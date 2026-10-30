"""Guild opt-in and all accounting data are physically isolated per server."""
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from bot.guilds import GuildRegistry
from bot.periods import period_bounds


@pytest_asyncio.fixture
async def registry(settings):
    configured = settings.model_copy(update={"guild_id": None, "daily_report_channel_id": None})
    registry = GuildRegistry(configured)
    await registry.initialize()
    yield registry
    await registry.close()


async def configure(registry, guild_id, report_channel_id, now):
    return await registry.configure(guild_id, report_channel_id, admin_role_ids={20},
                                    moderator_role_ids={10}, moderation_log_channel_id=None, now=now)


@pytest.mark.asyncio
async def test_unconfigured_guild_has_no_context_or_database(registry, tmp_path):
    before = set(tmp_path.rglob("*"))
    assert await registry.get(999) is None
    assert 999 not in await registry.guild_ids()
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.asyncio
async def test_two_guilds_same_ids_independent_all_accounting(registry, instant):
    a = await configure(registry, 101, 1001, instant - timedelta(minutes=1))
    b = await configure(registry, 202, 2001, instant - timedelta(minutes=1))
    assert a.db.engine.url.database != b.db.engine.url.database
    # Discord IDs are normally global; identical fixture IDs verify actual isolation.
    for index in range(5):
        assert await a.service.record_message(5000 + index, 1, instant)
    for index in range(4):
        assert await b.service.record_message(5000 + index, 1, instant)
    await a.service.punish(1, 2, "Только сервер A", 7000, {20})
    await b.service.give(1, 2, 500, "Только сервер B", 7000, {20})
    _, end = period_bounds(instant)
    await a.service.settle_due(end + timedelta(seconds=1))
    await b.service.settle_due(end + timedelta(seconds=1))
    pa = await a.service.profile(1, now=instant)
    pb = await b.service.profile(1, now=instant)
    assert (pa["balance"], pa["credit_balance"], pa["message_count"]) == (775, 10, 5)
    assert (pb["balance"], pb["credit_balance"], pb["message_count"]) == (1300, 0, 4)
    assert (await a.service.top())[0]["balance"] == 775
    assert (await b.service.top())[0]["balance"] == 1300
    assert all(row.get("reason") != "Только сервер B" for row in await a.service.history(1))
    assert all(row.get("reason") != "Только сервер A" for row in await b.service.history(1))
    await a.service.set_state("channel_scan:42", instant.isoformat())
    assert await b.service.get_state("channel_scan:42") is None
    pending_a = await a.service.pending_notifications()
    pending_b = await b.service.pending_notifications()
    assert len(pending_a) == len(pending_b) == 1
    await a.service.notification_sent(pending_a[0]["id"], 12345)
    assert await a.service.pending_notifications() == []
    assert len(await b.service.pending_notifications()) == 1
    async with a.db.sessions() as session:
        assert (await session.execute(text("SELECT COUNT(*) FROM credit_transactions"))).scalar() == 1
    async with b.db.sessions() as session:
        assert (await session.execute(text("SELECT COUNT(*) FROM credit_transactions"))).scalar() == 0


@pytest.mark.asyncio
async def test_setup_records_exact_opt_in_and_reconfiguration_preserves_data(registry, instant):
    ctx = await configure(registry, 101, 1001, instant)
    assert await ctx.service.get_state("tracking_started") == instant.isoformat()
    assert await ctx.service.get_state("opt_in_at") == instant.isoformat()
    await ctx.service.give(1, 2, 100, "Проверка", 7000, {20})
    updated = await registry.configure(101, 1002, admin_role_ids={30}, moderator_role_ids=set(),
                                      moderation_log_channel_id=None, now=instant + timedelta(hours=1))
    assert (await updated.service.profile(1))["balance"] == 900
    assert updated.settings.daily_report_channel_id == 1002
    assert updated.settings.admin_role_ids == {30}
    assert updated.settings.moderator_role_ids == set()
    assert await updated.service.get_state("tracking_started") == instant.isoformat()
    assert await updated.service.get_state("opt_in_at") == instant.isoformat()


@pytest.mark.asyncio
async def test_registry_restart_preserves_settings_and_balances(registry, settings, instant):
    ctx = await configure(registry, 101, 1001, instant)
    await ctx.service.give(1, 2, 100, "Проверка", 7000, {20})
    await registry.close()
    restarted = GuildRegistry(settings.model_copy(update={"guild_id": None, "daily_report_channel_id": None}))
    await restarted.initialize()
    try:
        ctx = await restarted.get(101)
        assert ctx is not None
        assert ctx.settings.daily_report_channel_id == 1001
        assert ctx.settings.admin_role_ids == {20}
        assert (await ctx.service.profile(1))["balance"] == 900
        assert await restarted.get(202) is None
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_messages_before_opt_in_are_ignored(registry, instant):
    ctx = await configure(registry, 101, 1001, instant)
    assert not await ctx.service.record_message(5000, 1, instant - timedelta(microseconds=1))
    assert await ctx.service.top() == []
    assert await ctx.service.record_message(5001, 1, instant)
    assert (await ctx.service.profile(1, now=instant))["message_count"] == 1
    assert await ctx.service.history(1)


@pytest.mark.asyncio
async def test_legacy_import_bound_only_to_original_server_and_keeps_cursor(registry, instant):
    from bot.database import Database
    from bot.services import RatingService

    with pytest.raises(ValueError):
        await registry.import_legacy(202, 2001)
    registry.settings = registry.settings.model_copy(update={"legacy_guild_id": 101})
    with pytest.raises(ValueError):
        await registry.import_legacy(202, 2001)
    await registry._migrate(registry.legacy_path)
    db = Database('sqlite+aiosqlite:///' + str(registry.legacy_path))
    await db.initialize()
    old_settings = registry.settings.model_copy(update={"guild_id": 101, "daily_report_channel_id": 1001})
    old = RatingService(db, old_settings)
    await old.punish(1, 2, "Старое взыскание", 7000, {20})
    await old.set_state("channel_scan:42", instant.isoformat())
    await old.set_state("tracking_started", (instant - timedelta(days=2)).isoformat())
    await db.close()
    imported = await registry.import_legacy(101, 1001)
    assert imported is not None
    assert (await imported.service.profile(1))["balance"] == 750
    assert len(await imported.service.history(1)) == 2
    assert await imported.service.get_state("channel_scan:42") == instant.isoformat()
    assert await imported.service.get_state("tracking_started") == (instant - timedelta(days=2)).isoformat()
    assert await registry.import_legacy(101, 9999) is None
    assert imported.settings.daily_report_channel_id == 1001
    new_guild = await configure(registry, 202, 2001, instant)
    assert await new_guild.service.top() == []
    assert await new_guild.service.get_state("channel_scan:42") is None
    assert (await new_guild.service.profile(1))["balance"] == 800
    assert (await new_guild.service.profile(1))["credit_balance"] == 0


@pytest.mark.asyncio
async def test_legacy_import_disabled_without_explicit_original_guild(registry):
    await registry._migrate(registry.legacy_path)
    with pytest.raises(ValueError, match='explicit LEGACY_GUILD_ID'):
        await registry.import_legacy(101, 1001)
    assert await registry.guild_ids() == []
    assert registry.legacy_path.is_file()
