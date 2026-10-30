import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import discord
import pytest
from sqlalchemy import text

from bot.discord_app import RatingCommands, profile_embed
from bot.services import DomainError


@pytest.mark.parametrize("balance,label,color", [
    (-10000, "Неблагонадёжный", discord.Color.red()),
    (0, "Неблагонадёжный", discord.Color.red()),
    (499, "Неблагонадёжный", discord.Color.red()),
    (500, "Под наблюдением", discord.Color.orange()),
    (799, "Под наблюдением", discord.Color.orange()),
    (800, "Обычный гражданин", discord.Color.blue()),
    (1099, "Обычный гражданин", discord.Color.blue()),
    (1100, "Благонадёжный", discord.Color.green()),
    (1499, "Благонадёжный", discord.Color.green()),
    (1500, "Образцовый гражданин", discord.Color.gold()),
    (1000000, "Образцовый гражданин", discord.Color.gold()),
])
def test_simplified_profile_class_boundaries(settings, balance, label, color):
    embed = profile_embed({"discord_id": 123, "balance": balance, "credit_balance": 17}, settings)
    assert embed.description == "<@123>"
    assert [(field.name, field.value) for field in embed.fields] == [
        ("Социальный рейтинг", str(balance)), ("Социальный класс", label), ("Кредиты", "17")
    ]
    assert embed.color == color
    assert not embed.footer.text


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [True, False, 1.5, 1.0, 0, -1, 1000001])
async def test_invalid_grant_amount_rejected(service, amount):
    with pytest.raises(DomainError):
        await service.give(1, 2, amount, "Выдача", 901, {20})
    assert (await service.profile(1))["balance"] == 800
    assert [row["type"] for row in await service.history(1)] == ["opening"]


@pytest.mark.asyncio
@pytest.mark.parametrize("roles,admin", [(set(), False), ({10}, False), (set(), True)])
async def test_grant_requires_configured_admin_role(service, roles, admin):
    with pytest.raises(DomainError):
        await service.give(1, 2, 100, "Выдача", 901, roles, actor_is_admin=admin)
    assert (await service.profile(1))["balance"] == 800


@pytest.mark.asyncio
async def test_grant_administrator_fallback(service, settings):
    settings.admin_role_ids = set()
    settings.moderator_role_ids = set()
    with pytest.raises(DomainError):
        await service.give(1, 2, 100, "Выдача", 901, set())
    grant = await service.give(1, 2, 100, "Выдача", 902, set(), actor_is_admin=True)
    assert grant["type"] == "manual_grant"
    assert (await service.profile(1))["balance"] == 900


@pytest.mark.asyncio
async def test_grant_ledger_idempotency_and_optional_outbox(service, settings):
    settings.moderation_log_channel_id = None
    grant = await service.give(1, 2, 800, "Помощь проекту", 901, {20})
    retry = await service.give(1, 2, 800, "Помощь проекту", 901, {20})
    assert retry["id"] == grant["id"]
    assert grant["balance_before"] == 800
    assert grant["balance_after"] == 1600
    assert grant["type"] == "manual_grant"
    assert (await service.profile(1))["balance"] == 1600
    assert len(await service.history(1)) == 2
    assert await service.pending_notifications() == []
    with pytest.raises(DomainError):
        await service.give(1, 2, 801, "Помощь проекту", 901, {20})
    with pytest.raises(DomainError):
        await service.undo(grant["id"], 2, "Отмена выдачи как взыскания", 902, {20})
    assert (await service.profile(1))["balance"] == 1600


@pytest.mark.asyncio
async def test_grant_outbox_when_configured(service):
    grant = await service.give(1, 2, 1, "Выдача", 901, {20})
    rows = await service.pending_notifications()
    assert len(rows) == 1
    assert rows[0]["kind"] == "manual_grant"
    assert rows[0]["payload"]["id"] == grant["id"]


@pytest.mark.asyncio
async def test_concurrent_grants_and_punishments_preserve_balance(service):
    await asyncio.gather(
        *(service.give(1, 2, 300, "Выдача", 1000 + index, {20}) for index in range(10)),
        *(service.punish(1, 2, "Причина", 2000 + index, {10}) for index in range(10)),
    )
    assert (await service.profile(1))["balance"] == 3300
    rows = await service.history(1, page_size=25)
    assert len(rows) == 21
    assert sum(row["amount"] for row in rows) == 3300


@pytest.mark.asyncio
async def test_grant_balance_overflow_rejected(service):
    await service.profile(1)
    limit = (1 << 63) - 1
    async with service.db.write() as session:
        await session.execute(text("UPDATE users SET balance=:balance WHERE discord_id=1"), {"balance": limit})
    with pytest.raises(DomainError):
        await service.give(1, 2, 1, "Переполнение", 901, {20})
    assert (await service.profile(1))["balance"] == limit
    assert [row["type"] for row in await service.history(1)] == ["opening"]


@pytest.mark.asyncio
async def test_grant_maximum_accepted(service):
    await service.give(1, 2, 1000000, "Выдача", 901, {20})
    assert (await service.profile(1))["balance"] == 1000800


def test_ten_rating_commands_registered(settings):
    group = RatingCommands(SimpleNamespace(), settings)
    assert {command.name for command in group.commands} == {
        "profile", "user", "top", "history", "punish", "undo", "log", "status", "give", "setup"
    }


def test_upgrade_preserves_existing_ledger_and_allows_manual_grant(tmp_path):
    path = tmp_path / "old-ledger.db"
    env = os.environ | {"DATABASE_URL": f"sqlite+aiosqlite:///{path}"}
    root = Path(__file__).resolve().parents[1]

    def upgrade(revision):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision],
                                cwd=root, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr

    upgrade("c1b88909d385")
    timestamp = "2026-10-30T12:00:00+00:00"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO users VALUES (1, 0, ?)", (timestamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('p1',1,-100,'punishment','Причина',2,NULL,0,-100,NULL,?)", (timestamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('u1',1,100,'undo','Отмена',2,NULL,-100,0,'p1',?)", (timestamp,))
        conn.execute("INSERT INTO processed_interactions VALUES (901,'p1','fingerprint')")
        conn.execute("INSERT INTO processed_interactions VALUES (902,'u1','fingerprint2')")
    upgrade("head")
    upgrade("head")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT id,type,reverses_id FROM rating_transactions ORDER BY id").fetchall() == [
            ("p1", "punishment", None), ("u1", "undo", "p1")]
        assert conn.execute("SELECT COUNT(*) FROM processed_interactions").fetchone()[0] == 2
        assert conn.execute("SELECT balance FROM users WHERE discord_id=1").fetchone()[0] == 0
        conn.execute("INSERT INTO rating_transactions VALUES ('g1',1,500,'manual_grant','Выдача',2,NULL,0,500,NULL,?)", (timestamp,))
        conn.execute("UPDATE users SET balance=500 WHERE discord_id=1")
        conn.execute("INSERT INTO users (discord_id,balance,created_at) VALUES (2,800,?)", (timestamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('o1',2,800,'opening','Стартовый рейтинг',NULL,NULL,0,800,NULL,?)", (timestamp,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rating_transactions VALUES ('o2',2,800,'opening','Повтор',NULL,NULL,800,1600,NULL,?)", (timestamp,))
        assert conn.execute("SELECT COUNT(*) FROM rating_transactions WHERE type='opening' AND discord_id=2").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.asyncio
async def test_concurrent_new_profile_creation_has_one_opening(service):
    profiles = await asyncio.gather(*(service.profile(777) for _ in range(20)))
    assert {profile["balance"] for profile in profiles} == {800}
    assert {profile["awarded_days"] for profile in profiles} == {0}
    rows = await service.history(777)
    assert len(rows) == 1
    assert rows[0]["type"] == "opening"
    assert rows[0]["amount"] == 800
    assert rows[0]["balance_before"] == 0
    assert rows[0]["balance_after"] == 800
    assert await service.pending_notifications() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("balance", [0, -100])
async def test_legacy_balance_never_receives_new_opening(service, instant, balance):
    async with service.db.write() as session:
        await session.execute(text("INSERT INTO users (discord_id,balance,created_at) VALUES (777,:balance,:time)"),
                              {"balance": balance, "time": instant.isoformat()})
    assert (await service.profile(777))["balance"] == balance
    assert await service.history(777) == []
    await service.give(777, 2, 100, "Выдача", 999, {20})
    assert (await service.profile(777))["balance"] == balance + 100
    assert [row["type"] for row in await service.history(777)] == ["manual_grant"]
