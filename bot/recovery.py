"""Bounded Discord history reconciliation; never claims deleted messages recovered."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from bot.config import Settings
    from bot.services import RatingService

logger = logging.getLogger(__name__)


def accepts_message(message: Any, guild_id: int) -> bool:
    """Count normal human messages only; content is neither inspected nor stored."""
    return bool(
        message.guild is not None
        and message.guild.id == guild_id
        and not message.author.bot
        and message.webhook_id is None
        and isinstance(message.channel, discord.TextChannel)
        and message.channel.type is discord.ChannelType.text
        and not message.is_system()
    )


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def reconcile_history(
    guild: discord.Guild,
    service: RatingService,
    settings: Settings,
    *,
    cutoff: datetime,
    after_disconnect: bool,
) -> bool:
    """Scan from durable watermarks. Failure never advances a channel watermark.

    Discord's history iterator handles API pagination and rate-limit responses.
    IDs are deduplicated in SQLite. There is no message-content intent requirement.
    Watermarks represent a completed scan, not the most recent live event.
    """
    tracking = await service.get_state("tracking_started")
    if tracking is None:
        tracking = cutoff.isoformat()
        await service.set_state("tracking_started", tracking)
        await service.mark_incomplete(cutoff, cutoff, "Первый запуск: активность до установки не отслеживалась.")
    started = parse_utc(tracking)
    oldest = cutoff - timedelta(days=settings.recovery_lookback_days)
    prior_ids = set(filter(None, (await service.get_state("known_text_channels") or "").split(",")))
    channels = [c for c in guild.text_channels if c.type is discord.ChannelType.text]
    current_ids = {str(c.id) for c in channels}
    complete_scan = True
    unresolved_ids: set[str] = set()
    for channel_id in prior_ids - current_ids:
        value = await service.get_state(f"channel_scan:{channel_id}")
        begin = parse_utc(value) if value else started
        try:
            missing = await guild.fetch_channel(int(channel_id))
        except discord.NotFound:
            await service.mark_incomplete(begin, cutoff, f"Канал {channel_id} удалён; история не восстановлена.")
            await service.set_state(f"channel_scan:{channel_id}", cutoff.isoformat())
        except discord.Forbidden:
            unresolved_ids.add(channel_id)
            await service.mark_incomplete(begin, cutoff, f"Канал {channel_id} недоступен; история не восстановлена.")
        except (discord.HTTPException, OSError):
            unresolved_ids.add(channel_id)
            complete_scan = False
            await service.mark_incomplete(begin, cutoff, f"Канал {channel_id}: временная ошибка проверки доступности.")
        else:
            if isinstance(missing, discord.TextChannel) and missing.type is discord.ChannelType.text:
                channels.append(missing)
                current_ids.add(channel_id)
            else:
                await service.mark_incomplete(begin, cutoff, f"Канал {channel_id} больше не является обычным текстовым каналом.")
                await service.set_state(f"channel_scan:{channel_id}", cutoff.isoformat())
    for channel in channels:
        value = await service.get_state(f"channel_scan:{channel.id}")
        begin = parse_utc(value) if value else started
        begin = max(begin, started, channel.created_at)
        if begin >= cutoff:
            continue
        if after_disconnect:
            await service.mark_incomplete(begin, cutoff, f"Возможны удалённые за простой сообщения канала {channel.id}; восстановлена только доступная история.")
        if begin < oldest:
            await service.mark_incomplete(begin, oldest, f"Канал {channel.id}: превышено окно восстановления {settings.recovery_lookback_days} дней.")
            begin = oldest
        member = guild.me
        permissions = channel.permissions_for(member) if member else None
        if not permissions or not permissions.view_channel or not permissions.read_message_history:
            await service.mark_incomplete(begin, cutoff, f"Канал {channel.id}: нет View Channel / Read Message History.")
            logger.warning("History recovery unavailable in channel %s: permissions", channel.id)
            continue
        try:
            # A 1 ms overlap also covers a message exactly at the durable watermark.
            async for message in channel.history(limit=None, after=begin - timedelta(milliseconds=1), before=cutoff, oldest_first=True):
                if message.created_at < started or message.created_at < begin:
                    continue
                if accepts_message(message, settings.guild_id):
                    await service.record_message(message.id, message.author.id, message.created_at)
            await service.set_state(f"channel_scan:{channel.id}", cutoff.isoformat())
        except (discord.HTTPException, OSError):
            complete_scan = False
            logger.exception("History recovery failed in channel %s", channel.id)
            await service.mark_incomplete(begin, cutoff, f"Канал {channel.id}: ошибка чтения истории; будет повторено.")
    # The persisted incomplete period retains the deletion warning; do not flag
    # every future period for a channel that no longer exists.
    await service.set_state("known_text_channels", ",".join(sorted(current_ids | unresolved_ids)))
    await service.set_state("last_reconciliation", cutoff.isoformat())
    return complete_scan
