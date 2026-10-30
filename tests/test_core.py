"""Acceptance tests: actual isolated SQLite, never a live Discord server."""
import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from bot.backup import backup_database
from bot.database import Database
from bot.periods import period_bounds
from bot.services import DomainError, RatingService

pytestmark = pytest.mark.asyncio


async def activity(svc, instant, count=5, user=1, first_id=1000):
    for i in range(count):
        await svc.record_message(first_id + i, user, instant)


async def settle(svc, instant):
    _, end = period_bounds(instant)
    return await svc.settle_due(end + timedelta(seconds=1))


async def test_new_user_starts_eight_hundred(service, instant):
    profile = await service.profile(1, now=instant)
    assert profile["balance"] == 800
    assert profile["awarded_days"] == 0


@pytest.mark.parametrize("count,expected", [(4, 0), (5, 25), (10, 25)])
async def test_activity_threshold(service, instant, count, expected):
    await activity(service, instant, count)
    await settle(service, instant)
    profile = await service.profile(1, now=instant)
    assert profile["balance"] == 800 + expected
    assert profile["awarded_days"] == int(expected > 0)


async def test_duplicate_message_not_counted(service, instant):
    assert await service.record_message(100, 1, instant)
    assert not await service.record_message(100, 1, instant)
    assert (await service.profile(1, now=instant))["message_count"] == 1


async def test_payout_retry_no_double_award(service, instant):
    await activity(service, instant)
    await settle(service, instant)
    await settle(service, instant)
    assert (await service.profile(1, now=instant))["balance"] == 825


async def test_balance_persists_after_restart(service, settings, instant):
    await activity(service, instant)
    await settle(service, instant)
    second_db = Database(settings.database_url)
    await second_db.initialize()
    try:
        second = RatingService(second_db, settings)
        assert (await second.profile(1, now=instant))["balance"] == 825
        await settle(second, instant)
        assert (await second.profile(1, now=instant))["balance"] == 825
    finally:
        await second_db.close()


async def test_punishment_negative_and_multiple(service, instant):
    for interaction in range(200, 221):
        await service.punish(1, 2, "Нарушение правил", interaction, {10})
    p = await service.profile(1, now=instant)
    assert p["balance"] == 800 - 1050
    assert p["active_punishments"] == 21


@pytest.mark.parametrize("reason", ["", " ", "\t\n", "x" * 501])
async def test_invalid_reason_rejected(service, reason):
    with pytest.raises(DomainError):
        await service.punish(1, 2, reason, 201, {10})
    assert (await service.profile(1))["balance"] == 800


async def test_unauthorized_punishment_rejected(service):
    with pytest.raises(DomainError):
        await service.punish(1, 2, "Причина", 201, {999})
    assert (await service.profile(1))["balance"] == 800


async def test_undo_keeps_original_and_compensates(service):
    tx = await service.punish(1, 2, "Ошибка", 201, {10})
    await service.undo(tx["id"], 3, "Исправление", 202, {20})
    p = await service.profile(1)
    assert p["balance"] == 800
    assert p["active_punishments"] == 0
    assert len(await service.history(1)) == 3
    with pytest.raises(DomainError):
        await service.undo(tx["id"], 3, "Повтор", 203, {20})
    assert (await service.profile(1))["balance"] == 800


async def test_moderator_cannot_undo(service):
    tx = await service.punish(1, 2, "Ошибка", 201, {10})
    with pytest.raises(DomainError):
        await service.undo(tx["id"], 2, "Отмена", 202, {10})
    assert (await service.profile(1))["balance"] == 750


async def test_duplicate_interaction(service):
    await service.punish(1, 2, "Причина", 201, {10})
    await service.punish(1, 2, "Причина", 201, {10})
    assert (await service.profile(1))["balance"] == 750


async def test_concurrent_payout_and_punishments(service, instant):
    await activity(service, instant)
    await asyncio.gather(
        settle(service, instant), settle(service, instant),
        *(service.punish(1, 2, "Причина", 300 + i, {10}) for i in range(15)),
    )
    p = await service.profile(1)
    assert p["balance"] == 800 + 25 - 750
    assert p["awarded_days"] == 1
    assert p["active_punishments"] == 15


async def test_concurrent_duplicate_messages(service, instant):
    await asyncio.gather(*(service.record_message(100, 1, instant) for _ in range(15)))
    assert (await service.profile(1, now=instant))["message_count"] == 1


async def test_concurrent_database_instances(settings, service):
    db = Database(settings.database_url)
    await db.initialize()
    try:
        other = RatingService(db, settings)
        await asyncio.gather(
            *(svc.punish(1, 2, "Причина", 500 + i, {10})
              for i, svc in enumerate([service, other] * 5))
        )
        assert (await service.profile(1))["balance"] == 300
    finally:
        await db.close()


async def test_notification_failure_preserves_balance(service):
    await service.punish(1, 2, "Причина", 201, {10})
    rows = await service.pending_notifications()
    assert rows
    await service.notification_failed(rows[0]["id"], "Discord forbidden")
    assert (await service.profile(1))["balance"] == 750
    assert len(await service.history(1)) == 2


async def test_notification_delivery_ack(service):
    await service.punish(1, 2, "Причина", 201, {10})
    row = (await service.pending_notifications())[0]
    await service.notification_sent(row["id"], 888)
    assert not await service.pending_notifications()


async def test_backup_integrity(service, settings, tmp_path):
    await service.punish(1, 2, "Причина", 201, {10})
    source = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    destination = tmp_path / "backup.db"
    await backup_database(source, destination)
    with sqlite3.connect(destination) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT balance FROM users WHERE discord_id=1").fetchone()[0] == 750
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_daily_award_cannot_be_undone(service, instant):
    await activity(service, instant)
    await settle(service, instant)
    tx = next(row for row in await service.history(1) if row["type"] == "daily")
    with pytest.raises(DomainError):
        await service.undo(tx["id"], 3, "Отмена начисления", 800, {20})
    assert (await service.profile(1))["balance"] == 825


async def test_top_tie_order_stable(service):
    for user in (9, 2, 5):
        await service.profile(user)
    first = await service.top(page=0, page_size=2)
    second = await service.top(page=1, page_size=2)
    assert [r["discord_id"] for r in first + second] == [2, 5, 9]


async def test_interrupted_payout_rolls_back_and_retry_completes(service, instant, monkeypatch):
    await activity(service, instant, user=1)
    await activity(service, instant, user=2, first_id=2000)
    original = service._transaction
    calls = 0

    async def fail_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = await original(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("Simulated process failure before commit")
        return result

    monkeypatch.setattr(service, "_transaction", fail_after_first)
    with pytest.raises(RuntimeError):
        await settle(service, instant)
    assert (await service.profile(1))["balance"] == 800
    assert (await service.profile(2))["balance"] == 800
    assert [row["type"] for row in await service.history(1)] == ["opening"]
    assert await service.pending_notifications() == []
    monkeypatch.setattr(service, "_transaction", original)
    await settle(service, instant)
    await settle(service, instant)
    assert (await service.profile(1))["balance"] == 825
    assert (await service.profile(2))["balance"] == 825


async def test_stale_event_cannot_reopen_retention_window(service, instant):
    assert not await service.record_message(123, 1, instant - timedelta(days=36))
    assert (await service.profile(1))["balance"] == 800
    assert (await service.status(now=instant))["incomplete_periods"]


async def test_late_event_does_not_reopen_completed_period(service, instant):
    await activity(service, instant, count=4)
    await settle(service, instant)
    assert not await service.record_message(9999, 1, instant)
    await settle(service, instant)
    assert (await service.profile(1))["balance"] == 800
    assert (await service.status(now=instant))["incomplete_periods"]


async def test_cleanup_does_not_permit_duplicate_daily_award(service, instant):
    await activity(service, instant)
    await settle(service, instant)
    await service.cleanup(now=instant + timedelta(days=40))
    assert not await service.record_message(1000, 1, instant)
    assert (await service.profile(1))["balance"] == 825


async def test_notification_failure_keeps_daily_award(service, instant):
    await activity(service, instant)
    await settle(service, instant)
    row = (await service.pending_notifications())[0]
    await service.notification_failed(row["id"], "Forbidden")
    await settle(service, instant)
    assert (await service.profile(1))["balance"] == 825
    assert len(await service.history(1)) == 3


async def test_undo_interaction_retry(service):
    tx = await service.punish(1, 2, "Причина", 201, {10})
    result = await service.undo(tx["id"], 3, "Отмена", 202, {20})
    retry = await service.undo(tx["id"], 3, "Отмена", 202, {20})
    assert retry["id"] == result["id"]
    assert (await service.profile(1))["balance"] == 800


async def test_database_pragmas(service):
    from sqlalchemy import text
    async with service.db.engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() > 0
