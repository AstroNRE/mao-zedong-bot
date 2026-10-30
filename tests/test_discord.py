from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from bot.recovery import accepts_message, reconcile_history


def channel_factory(instant, channel_id=10):
    channel = MagicMock(spec=discord.TextChannel)
    channel.type = discord.ChannelType.text
    channel.id = channel_id
    channel.created_at = instant - timedelta(days=100)
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, read_message_history=True)
    return channel


def message_factory(channel, instant, message_id=100):
    return SimpleNamespace(
        id=message_id, guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=1, bot=False), webhook_id=None,
        channel=channel, is_system=lambda: False, created_at=instant,
    )


def test_ordinary_message_accepted(instant):
    assert accepts_message(message_factory(channel_factory(instant), instant), 123)


@pytest.mark.parametrize("case", ["dm", "other_guild", "bot", "webhook", "system", "thread", "forum", "voice", "news"])
def test_excluded_message_sources(instant, case):
    message = message_factory(channel_factory(instant), instant)
    if case == "dm":
        message.guild = None
    elif case == "other_guild":
        message.guild.id = 999
    elif case == "bot":
        message.author.bot = True
    elif case == "webhook":
        message.webhook_id = 999
    elif case == "system":
        message.is_system = lambda: True
    elif case == "thread":
        message.channel = MagicMock(spec=discord.Thread)
    elif case == "forum":
        message.channel = MagicMock(spec=discord.ForumChannel)
    elif case == "voice":
        message.channel = MagicMock(spec=discord.VoiceChannel)
    elif case == "news":
        message.channel.type = discord.ChannelType.news
    assert not accepts_message(message, 123)


@pytest.mark.asyncio
async def test_recovery_deduplicates_and_preserves_deleted_live_activity(service, settings, instant):
    channel = channel_factory(instant)
    start = instant - timedelta(minutes=10)
    await service.set_state("tracking_started", start.isoformat())
    # Message 99 was seen live then deleted and therefore absent from history.
    await service.record_message(99, 1, instant - timedelta(minutes=2))
    await service.record_message(100, 1, instant - timedelta(minutes=1))

    async def history(**kwargs):
        for i in range(100, 104):
            yield message_factory(channel, instant - timedelta(minutes=1), i)
    channel.history = history
    guild = SimpleNamespace(id=123, text_channels=[channel], me=object())
    assert await reconcile_history(guild, service, settings, cutoff=instant, after_disconnect=True)
    assert (await service.profile(1, now=instant))["message_count"] == 5
    assert (await service.status(now=instant))["current_period_incomplete"]
    assert await reconcile_history(guild, service, settings, cutoff=instant + timedelta(seconds=1), after_disconnect=False)
    assert (await service.profile(1, now=instant))["message_count"] == 5


@pytest.mark.asyncio
async def test_recovery_missing_permissions_marks_incomplete(service, settings, instant):
    await service.set_state("tracking_started", (instant - timedelta(minutes=10)).isoformat())
    channel = channel_factory(instant)
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, read_message_history=False)
    guild = SimpleNamespace(id=123, text_channels=[channel], me=object())
    assert await reconcile_history(guild, service, settings, cutoff=instant, after_disconnect=False)
    assert await service.get_state("channel_scan:10") is None
    assert (await service.status(now=instant))["current_period_incomplete"]
    channel.history.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_temporary_failure_does_not_advance_watermark(service, settings, instant):
    start = instant - timedelta(minutes=10)
    await service.set_state("tracking_started", start.isoformat())
    channel = channel_factory(instant)

    async def history(**kwargs):
        raise OSError("Simulated temporary network failure")
        yield  # async iterator shape, no event can be yielded on failure
    channel.history = history
    guild = SimpleNamespace(id=123, text_channels=[channel], me=object())
    assert not await reconcile_history(guild, service, settings, cutoff=instant, after_disconnect=True)
    assert await service.get_state("channel_scan:10") is None
    assert (await service.status(now=instant))["current_period_incomplete"]


@pytest.mark.asyncio
async def test_recovery_failure_does_not_block_outbox(service, settings, monkeypatch):
    from unittest.mock import AsyncMock
    import bot.discord_app as adapter

    client = adapter.RatingBot(settings, service)
    sender = AsyncMock()
    settle = AsyncMock()
    monkeypatch.setattr(client, "send_pending_notifications", sender)
    monkeypatch.setattr(client, "get_guild", lambda _: object())
    monkeypatch.setattr(service, "settle_due", settle)
    monkeypatch.setattr(adapter, "reconcile_history", AsyncMock(return_value=False))
    try:
        await client.cycle()
        sender.assert_awaited_once()
        settle.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,retained,result", [("deleted", False, True), ("hidden", True, True), ("temporary", True, False)])
async def test_missing_channel_classified_without_repeated_deletion_warning(service, settings, instant, kind, retained, result):
    from unittest.mock import AsyncMock

    await service.set_state("tracking_started", (instant - timedelta(minutes=10)).isoformat())
    await service.set_state("known_text_channels", "10")
    response = SimpleNamespace(status=404 if kind == "deleted" else 403, reason=kind)
    error = discord.NotFound(response, "deleted") if kind == "deleted" else discord.Forbidden(response, "hidden") if kind == "hidden" else OSError("temporary")
    guild = SimpleNamespace(id=123, text_channels=[], me=object(), fetch_channel=AsyncMock(side_effect=error))
    assert await reconcile_history(guild, service, settings, cutoff=instant, after_disconnect=False) is result
    assert (await service.get_state("known_text_channels") == "10") is retained
    assert (await service.status(now=instant))["current_period_incomplete"]
