"""Separate credits use the class before the same period's rating increase."""
import asyncio
from datetime import timedelta
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import text

from bot.database import Database
from bot.periods import period_bounds
from bot.services import RatingService


async def credit_rows(service):
    async with service.db.sessions() as session:
        result = await session.execute(text("SELECT * FROM credit_transactions ORDER BY period_start,discord_id"))
        return [dict(row) for row in result.mappings()]


async def messages(service, when, *, user_id=1, first_id=1000, count=5):
    for index in range(count):
        await service.record_message(first_id + index, user_id, when)


async def settle(service, when):
    _, end = period_bounds(when)
    return await service.settle_due(end + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_new_user_credit_balance_zero(service):
    profile = await service.profile(1)
    assert profile["balance"] == 800
    assert profile["credit_balance"] == 0
    assert await credit_rows(service) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rating,class_key,reward", [
    (-100, "untrusted", 0), (0, "untrusted", 0), (499, "untrusted", 0),
    (500, "watched", 10), (799, "watched", 10),
    (800, "citizen", 20), (1099, "citizen", 20),
    (1100, "trusted", 30), (1499, "trusted", 30),
    (1500, "exemplary", 50), (9999, "exemplary", 50),
])
async def test_credit_reward_class_boundaries_and_zero_audit(service, instant, rating, class_key, reward):
    await service.profile(1)
    async with service.db.write() as session:
        await session.execute(text("UPDATE users SET balance=:rating WHERE discord_id=1"), {"rating": rating})
    await messages(service, instant)
    await settle(service, instant)
    profile = await service.profile(1)
    assert profile["balance"] == rating + 25
    assert profile["credit_balance"] == reward
    rows = await credit_rows(service)
    assert len(rows) == 1
    assert rows[0]["amount"] == reward
    assert rows[0]["balance_before"] == 0
    assert rows[0]["balance_after"] == reward
    assert rows[0]["basis_rating"] == rating
    assert rows[0]["basis_class"] == {"untrusted": "Неблагонадёжный", "watched": "Под наблюдением", "citizen": "Обычный гражданин", "trusted": "Благонадёжный", "exemplary": "Образцовый гражданин"}[class_key]


@pytest.mark.asyncio
async def test_four_messages_give_no_credits(service, instant):
    await messages(service, instant, count=4)
    await settle(service, instant)
    assert (await service.profile(1))["credit_balance"] == 0
    assert await credit_rows(service) == []


@pytest.mark.asyncio
async def test_class_change_changes_next_period_only(service, instant):
    await service.profile(1)
    async with service.db.write() as session:
        await session.execute(text("UPDATE users SET balance=799 WHERE discord_id=1"))
    await messages(service, instant)
    await settle(service, instant)
    assert (await service.profile(1))["balance"] == 824
    assert (await service.profile(1))["credit_balance"] == 10
    _, end = period_bounds(instant)
    tomorrow = end + timedelta(hours=1)
    await messages(service, tomorrow, first_id=2000)
    await settle(service, tomorrow)
    assert (await service.profile(1))["balance"] == 849
    assert (await service.profile(1))["credit_balance"] == 30
    assert [(row["basis_rating"], row["amount"]) for row in await credit_rows(service)] == [(799, 10), (824, 20)]


@pytest.mark.asyncio
async def test_recovered_periods_use_processing_time_rating_chronologically(service, instant):
    await service.profile(1)
    async with service.db.write() as session:
        await session.execute(text("UPDATE users SET balance=799 WHERE discord_id=1"))
    _, end = period_bounds(instant)
    tomorrow = end + timedelta(hours=1)
    await messages(service, instant)
    await messages(service, tomorrow, first_id=2000)
    # Neither period settled until recovery of both completes.
    await settle(service, tomorrow)
    assert [(row["basis_rating"], row["amount"]) for row in await credit_rows(service)] == [(799, 10), (824, 20)]
    assert (await service.profile(1))["credit_balance"] == 30


@pytest.mark.asyncio
async def test_credits_idempotent_concurrent_payout_and_restart(service, settings, instant):
    await messages(service, instant, count=10)
    await asyncio.gather(*(settle(service, instant) for _ in range(5)))
    second_db = Database(settings.database_url)
    await second_db.initialize()
    try:
        restarted = RatingService(second_db, settings)
        await settle(restarted, instant)
        assert (await restarted.profile(1))["credit_balance"] == 20
        assert len(await credit_rows(restarted)) == 1
        assert (await restarted.profile(1))["balance"] == 825
    finally:
        await second_db.close()


@pytest.mark.asyncio
async def test_rating_operations_never_change_credit_balance(service, instant):
    await messages(service, instant)
    await settle(service, instant)
    punishment = await service.punish(1, 2, "Взыскание", 2000, {20})
    assert (await service.profile(1))["credit_balance"] == 20
    await service.give(1, 2, 500, "Выдача рейтинга", 2001, {20})
    assert (await service.profile(1))["credit_balance"] == 20
    await service.undo(punishment["id"], 2, "Отмена", 2002, {20})
    assert (await service.profile(1))["credit_balance"] == 20
    assert len(await credit_rows(service)) == 1


@pytest.mark.asyncio
async def test_period_failure_rolls_back_rating_and_credits_together(service, instant, monkeypatch):
    await messages(service, instant, user_id=1)
    await messages(service, instant, user_id=2, first_id=2000)
    original = service._transaction
    daily_count = 0

    async def fail_second_daily(*args, **kwargs):
        nonlocal daily_count
        result = await original(*args, **kwargs)
        if result.type == "daily":
            daily_count += 1
            if daily_count == 2:
                raise RuntimeError("Simulated crash in period transaction")
        return result

    monkeypatch.setattr(service, "_transaction", fail_second_daily)
    with pytest.raises(RuntimeError):
        await settle(service, instant)
    for user_id in (1, 2):
        profile = await service.profile(user_id)
        assert profile["balance"] == 800
        assert profile["credit_balance"] == 0
    assert await credit_rows(service) == []
    assert await service.pending_notifications() == []
    monkeypatch.setattr(service, "_transaction", original)
    await settle(service, instant)
    assert len(await credit_rows(service)) == 2
    assert (await service.profile(1))["credit_balance"] == 20
    assert (await service.profile(2))["credit_balance"] == 20


@pytest.mark.asyncio
async def test_currency_migration_preserves_completed_period_without_retro_grant(tmp_path, settings, instant):
    database = tmp_path / "before-credits.db"
    url = f"sqlite+aiosqlite:///{database}"
    env = os.environ | {"DATABASE_URL": url}
    root = Path(__file__).resolve().parents[1]

    def migrate(revision):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision],
                                cwd=root, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("f3a25050")
    start, end = period_bounds(instant)
    stamp = instant.isoformat()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO users (discord_id,balance,created_at) VALUES (1,825,?)", (stamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('opening',1,800,'opening','Старт',NULL,NULL,0,800,NULL,?)", (stamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('daily',1,25,'daily',NULL,NULL,?,800,825,NULL,?)", (start.isoformat(), stamp))
        conn.execute("INSERT INTO daily_activity VALUES (1,?,5)", (start.isoformat(),))
        conn.execute("INSERT INTO service_state VALUES ('tracking_started',?)", (stamp,))
        conn.execute("INSERT INTO daily_payouts VALUES (?,?,'completed',?,?,1,25)",
                     (start.isoformat(), end.isoformat(), stamp, stamp))
    migrate("head")
    db = Database(url)
    await db.initialize()
    try:
        svc = RatingService(db, settings)
        await svc.settle_due(end + timedelta(seconds=1))
        profile = await svc.profile(1)
        assert profile["balance"] == 825
        assert profile["credit_balance"] == 0
        assert await credit_rows(svc) == []
        assert len(await svc.history(1)) == 2
        assert await svc.pending_notifications() == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_and_report_distinguish_rating_from_credits(service, instant):
    from bot.discord_app import history_embed, notification_embed

    await messages(service, instant)
    payouts = await settle(service, instant)
    rows = await service.history(1)
    assert {row["type"] for row in rows} == {"opening", "daily", "credit_daily"}
    history = history_embed(rows, 0)
    assert any("кредитов" in field.name and "+20" in field.name for field in history.fields)
    assert any("рейтинга" in field.name and "+25" in field.name for field in history.fields)
    assert payouts[0]["total_credits"] == 20
    assert payouts[0]["credit_class_counts"] == {"Обычный гражданин": 1}
    report = notification_embed("daily", payouts[0])
    fields = {field.name: field.value for field in report.fields}
    assert fields["Всего начислено рейтинга"] == "25"
    assert fields["Всего начислено кредитов"] == "20"
    assert "Обычный гражданин: 1" in fields["Получатели кредитов по классам"]


def test_legacy_report_does_not_invent_credits():
    from bot.discord_app import notification_embed

    report = notification_embed("daily", {"participants_count": 2, "total_amount": 100,
                                           "period_end": "2026-10-30T16:00:00+00:00"})
    fields = {field.name: field.value for field in report.fields}
    assert fields["Начислено каждому"] == "+50 рейтинга"
    assert not any("кредит" in name.lower() for name in fields)
