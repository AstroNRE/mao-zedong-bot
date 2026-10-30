from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.discord_app import RatingCommands
from bot.multi_discord import MultiGuildBot, GuildWorker
from bot.services import DomainError


def request(admin=True):
    guild = SimpleNamespace(id=123, me=object(), default_role=SimpleNamespace(id=123))
    return SimpleNamespace(
        guild=guild, guild_id=123, created_at=datetime.now(UTC),
        user=SimpleNamespace(id=1, roles=[SimpleNamespace(id=20)], guild_permissions=SimpleNamespace(administrator=admin)),
        command=SimpleNamespace(name="setup", qualified_name="rating setup"),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), client=SimpleNamespace(user=SimpleNamespace(id=999)),
    )


def channel(guild, channel_id=456, *, can_send=True):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.guild = guild
    channel.type = discord.ChannelType.text
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=can_send, embed_links=True)
    return channel


@pytest.mark.asyncio
async def test_setup_requires_actual_discord_administrator(settings):
    registry = SimpleNamespace(configure=AsyncMock())
    command = RatingCommands(None, settings, registry=registry)
    interaction = request(admin=False)
    await command.setup.callback(command, interaction, channel(interaction.guild))
    registry.configure.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["foreign_channel", "news", "missing_permissions", "foreign_role", "everyone", "same_log", "public_log"])
async def test_invalid_setup_cannot_modify_registry(settings, monkeypatch, bad):
    registry = SimpleNamespace(configure=AsyncMock())
    command = RatingCommands(None, settings, registry=registry)
    interaction = request()
    report = channel(interaction.guild)
    admin_role = None
    log = None
    if bad == "foreign_channel":
        report.guild = SimpleNamespace(id=999)
    elif bad == "news":
        report.type = discord.ChannelType.news
    elif bad == "missing_permissions":
        report.permissions_for.return_value.send_messages = False
    elif bad == "foreign_role":
        admin_role = SimpleNamespace(id=20, guild=SimpleNamespace(id=999))
    elif bad == "everyone":
        admin_role = SimpleNamespace(id=123, guild=interaction.guild)
    elif bad == "same_log":
        log = report
    elif bad == "public_log":
        log = channel(interaction.guild, 789)
        monkeypatch.setattr("bot.discord_app.private_log_channel", lambda *args: False)
    with pytest.raises(DomainError):
        await command.setup.callback(command, interaction, report, admin_role=admin_role, moderation_log_channel=log)
    registry.configure.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_defers_before_registry_and_uses_only_current_guild(settings):
    interaction = request()
    service = object()
    returned_settings = settings.model_copy(update={"guild_id": 123, "daily_report_channel_id": 456})

    async def configure(**kwargs):
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        return SimpleNamespace(service=service, settings=returned_settings)

    registry = SimpleNamespace(configure=AsyncMock(side_effect=configure))
    client = SimpleNamespace(refresh_guild=AsyncMock())
    command = RatingCommands(None, settings, registry=registry, client=client)
    await command.setup.callback(command, interaction, channel(interaction.guild))
    values = registry.configure.call_args.kwargs
    assert values["guild_id"] == 123
    assert values["report_channel_id"] == 456
    assert values["admin_role_ids"] == values["moderator_role_ids"] == set()
    assert values["moderation_log_channel_id"] is None
    assert command.service is service
    client.refresh_guild.assert_awaited_once_with(123)
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_unconfigured_server_blocks_nonsetup_without_registry_io(settings):
    registry = SimpleNamespace(get=AsyncMock())
    commands = RatingCommands(None, settings, registry=registry)
    interaction = request()
    assert await commands.interaction_check(interaction)
    interaction.command = SimpleNamespace(name="profile")
    assert not await commands.interaction_check(interaction)
    registry.get.assert_not_awaited()
    interaction.guild_id = 999
    assert not await commands.interaction_check(interaction)


@pytest.mark.asyncio
async def test_gateway_routes_messages_to_matching_worker_only(settings):
    registry = SimpleNamespace(get=AsyncMock())
    bot = MultiGuildBot(settings, registry)
    first = SimpleNamespace(active=True, is_ready=lambda: True, on_message=AsyncMock(), stop=AsyncMock())
    second = SimpleNamespace(active=True, is_ready=lambda: True, on_message=AsyncMock(), stop=AsyncMock())
    bot.workers = {123: first, 456: second}
    try:
        message = SimpleNamespace(guild=SimpleNamespace(id=123))
        await bot.on_message(message)
        first.on_message.assert_awaited_once_with(message)
        second.on_message.assert_not_awaited()
        await bot.on_message(SimpleNamespace(guild=SimpleNamespace(id=999)))
        await bot.on_message(SimpleNamespace(guild=None))
        assert first.on_message.await_count == 1
        registry.get.assert_not_awaited()
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_unconfigured_registration_does_not_start_worker(settings, monkeypatch):
    registry = SimpleNamespace(get=AsyncMock(return_value=None))
    bot = MultiGuildBot(settings, registry)
    guild = SimpleNamespace(id=123, unavailable=False)
    monkeypatch.setattr(bot, "get_guild", lambda gid: guild)
    monkeypatch.setattr(bot.tree, "sync", AsyncMock())
    try:
        await bot.register_guild(guild)
        await bot.register_guild(guild)
        assert bot.groups[123].service is None
        assert bot.workers == {}
        bot.tree.sync.assert_awaited_once()
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_guild_worker_rejects_cross_server_channel(settings):
    client = SimpleNamespace(fetch_channel=AsyncMock(return_value=SimpleNamespace(guild=SimpleNamespace(id=456))))
    worker = GuildWorker(client, SimpleNamespace(settings=settings, service=object()))
    with pytest.raises(ValueError):
        await worker.fetch_channel(999)


@pytest.mark.asyncio
async def test_removing_guild_stops_only_its_worker_and_keeps_registry(settings):
    registry = SimpleNamespace(get=AsyncMock())
    bot = MultiGuildBot(settings, registry)
    first = SimpleNamespace(stop=AsyncMock())
    second = SimpleNamespace(stop=AsyncMock())
    bot.workers = {123: first, 456: second}
    try:
        await bot.on_guild_remove(SimpleNamespace(id=123))
        first.stop.assert_awaited_once()
        second.stop.assert_not_awaited()
        assert set(bot.workers) == {456}
        registry.get.assert_not_awaited()
    finally:
        await bot.close()
