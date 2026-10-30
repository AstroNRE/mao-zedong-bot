"""Current 25/50 rules, historical-ledger compatibility and public class ranking."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from bot.discord_app import RatingCommands
from bot.services import DomainError


async def seed(service, instant, pairs):
    async with service.db.write() as session:
        for user_id, balance in pairs:
            await session.execute(text("INSERT INTO users (discord_id,balance,created_at) VALUES (:id,:balance,:time)"),
                                  {"id": user_id, "balance": balance, "time": instant.isoformat()})


@pytest.mark.asyncio
@pytest.mark.parametrize("key,expected", [
    ("untrusted", [-100, 0, 499]),
    ("watched", [500, 799]),
    ("citizen", [800, 1099]),
    ("trusted", [1100, 1499]),
    ("exemplary", [1500, 9999]),
])
async def test_top_filter_exact_class_ranges(service, instant, key, expected):
    values = [-100, 0, 499, 500, 799, 800, 1099, 1100, 1499, 1500, 9999]
    await seed(service, instant, enumerate(values, start=1))
    result = await service.top(class_key=key)
    assert [row["balance"] for row in result] == list(reversed(expected))
    assert [row["rank"] for row in result] == list(range(1, len(expected) + 1))


@pytest.mark.asyncio
async def test_competition_rank_and_deterministic_ties(service, instant):
    await seed(service, instant, [(9, 1500), (2, 1500), (7, 1400), (4, 1400), (8, 900)])
    result = await service.top()
    assert [row["discord_id"] for row in result] == [2, 9, 4, 7, 8]
    assert [row["rank"] for row in result] == [1, 1, 3, 3, 5]
    assert (await service.my_rank(7))["rank"] == 3
    assert (await service.my_rank(7, class_key="trusted"))["rank"] == 1


@pytest.mark.asyncio
async def test_top_default_page_has_ten_and_ranks_span_pages(service, instant):
    await seed(service, instant, [(index, 1000) for index in range(1, 13)] + [(13, 900)])
    first = await service.top()
    second = await service.top(page=1)
    assert len(first) == 10
    assert [row["discord_id"] for row in first] == list(range(1, 11))
    assert [row["discord_id"] for row in second] == [11, 12, 13]
    assert [row["rank"] for row in first] == [1] * 10
    assert [row["rank"] for row in second] == [1, 1, 13]
    assert (await service.my_rank(13))["rank"] == 13


@pytest.mark.asyncio
async def test_my_rank_does_not_create_user_or_opening(service):
    assert await service.my_rank(777) is None
    assert await service.top() == []
    assert await service.history(777) == []


@pytest.mark.asyncio
async def test_my_rank_outside_selected_class_returns_none(service, instant):
    await seed(service, instant, [(1, 800)])
    assert await service.my_rank(1, class_key="exemplary") is None
    assert (await service.my_rank(1, class_key="citizen"))["rank"] == 1


@pytest.mark.asyncio
async def test_invalid_class_key_rejected(service):
    with pytest.raises(DomainError):
        await service.top(class_key="unknown")
    with pytest.raises(DomainError):
        await service.my_rank(1, class_key="unknown")


@pytest.mark.asyncio
async def test_undo_preserves_old_100_and_returns_new_50(service, instant):
    await seed(service, instant, [(777, -100)])
    async with service.db.write() as session:
        await session.execute(text("INSERT INTO rating_transactions (id,discord_id,amount,type,reason,moderator_id,period_start,balance_before,balance_after,reverses_id,created_at) VALUES ('legacy-p',777,-100,'punishment','Старое взыскание',2,NULL,0,-100,NULL,:time)"), {"time": instant.isoformat()})
    old_undo = await service.undo("legacy-p", 2, "Отмена старого", 700, {20})
    assert old_undo["amount"] == 100
    assert (await service.profile(777))["balance"] == 0
    punishment = await service.punish(777, 2, "Новое взыскание", 701, {20})
    assert punishment["amount"] == -50
    assert (await service.profile(777))["balance"] == -50
    new_undo = await service.undo(punishment["id"], 2, "Отмена нового", 702, {20})
    assert new_undo["amount"] == 50
    assert (await service.profile(777))["balance"] == 0
    rows = await service.history(777)
    assert sorted(row["amount"] for row in rows) == [-100, -50, 50, 100]


def test_top_has_all_five_class_choices(settings):
    commands = RatingCommands(SimpleNamespace(), settings)
    choices = {choice.value for param in commands.top.parameters for choice in param.choices}
    assert choices == {"all", "untrusted", "watched", "citizen", "trusted", "exemplary"}


def test_rules_migration_keeps_old_amounts_and_accepts_new(tmp_path):
    import os
    from pathlib import Path
    import sqlite3
    import subprocess
    import sys

    database = tmp_path / "before-rule-change.db"
    env = os.environ | {"DATABASE_URL": f"sqlite+aiosqlite:///{database}"}
    root = Path(__file__).resolve().parents[1]

    def migrate(revision):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision],
                                cwd=root, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("f2a80001")
    timestamp = "2026-10-30T12:00:00+00:00"
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO users VALUES (1,750,?)", (timestamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('opening',1,800,'opening','Старт',NULL,NULL,0,800,NULL,?)", (timestamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('daily-old',1,50,'daily',NULL,NULL,'2026-10-28T16:00:00+00:00',800,850,NULL,?)", (timestamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('punish-old',1,-100,'punishment','Причина',2,NULL,850,750,NULL,?)", (timestamp,))
        conn.execute("INSERT INTO processed_interactions VALUES (1,'punish-old','legacy-fingerprint')")
    migrate("head")
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT balance FROM users WHERE discord_id=1").fetchone()[0] == 750
        assert conn.execute("SELECT amount FROM rating_transactions WHERE id='daily-old'").fetchone()[0] == 50
        assert conn.execute("SELECT amount FROM rating_transactions WHERE id='punish-old'").fetchone()[0] == -100
        conn.execute("INSERT INTO rating_transactions VALUES ('daily-new',1,25,'daily',NULL,NULL,'2026-10-29T16:00:00+00:00',750,775,NULL,?)", (timestamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('punish-new',1,-50,'punishment','Причина',2,NULL,775,725,NULL,?)", (timestamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('undo-new',1,50,'undo','Отмена',2,NULL,725,775,'punish-new',?)", (timestamp,))
        conn.execute("INSERT INTO rating_transactions VALUES ('undo-old',1,100,'undo','Отмена старого',2,NULL,775,875,'punish-old',?)", (timestamp,))
        conn.execute("UPDATE users SET balance=875 WHERE discord_id=1")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.asyncio
async def test_top_public_and_history_private(settings):
    from datetime import UTC, datetime
    from discord import app_commands

    service = SimpleNamespace(
        top=AsyncMock(side_effect=[[
            {"discord_id": 1, "balance": 900, "rank": 1},
            {"discord_id": 2, "balance": 900, "rank": 1},
        ], []]),
        my_rank=AsyncMock(return_value={"discord_id": 1, "balance": 900, "rank": 1}),
        history=AsyncMock(return_value=[]),
    )
    commands = RatingCommands(service, settings)
    request = SimpleNamespace(created_at=datetime.now(UTC), user=SimpleNamespace(id=1),
                              command=SimpleNamespace(qualified_name="rating top"),
                              response=SimpleNamespace(defer=AsyncMock()),
                              followup=SimpleNamespace(send=AsyncMock()))
    await commands.top.callback(commands, request, app_commands.Choice(name="Обычный гражданин", value="citizen"))
    request.response.defer.assert_awaited_once_with(ephemeral=False)
    service.top.assert_any_await(page=0, page_size=10, class_key="citizen")
    service.top.assert_any_await(page=1, page_size=10, class_key="citizen")
    service.my_rank.assert_awaited_once_with(1, class_key="citizen")
    top_kwargs = request.followup.send.call_args.kwargs
    assert top_kwargs["ephemeral"] is False
    assert top_kwargs["embed"].description.startswith("1. <@1>")
    assert "\n1. <@2>" in top_kwargs["embed"].description
    assert top_kwargs["embed"].fields[0].name == "Место автора команды"
    # Public leaderboard buttons still belong only to the author of the command.
    stranger = SimpleNamespace(user=SimpleNamespace(id=999), response=SimpleNamespace(send_message=AsyncMock()))
    assert not await top_kwargs["view"].interaction_check(stranger)
    assert stranger.response.send_message.call_args.kwargs["ephemeral"] is True
    top_kwargs["view"].stop()
    request.response.defer.reset_mock()
    request.followup.send.reset_mock()
    await commands.history.callback(commands, request)
    request.response.defer.assert_awaited_once_with(ephemeral=True)
    history_kwargs = request.followup.send.call_args.kwargs
    assert history_kwargs["ephemeral"] is True
    history_kwargs["view"].stop()
