"""Multi-guild Discord runtime with isolated configured contexts and workers."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import aiohttp
import discord
from discord import app_commands

from bot.config import Settings
from bot.discord_app import RatingBot, RatingCommands
from bot.guilds import GuildContext, GuildRegistry

logger = logging.getLogger(__name__)


class GuildWorker:
    """Per-guild state only; reuse tested single-guild accounting workflows."""

    send_pending_notifications = RatingBot.send_pending_notifications
    cycle = RatingBot.cycle
    on_message = RatingBot.on_message

    def __init__(self, client: MultiGuildBot, context: GuildContext) -> None:
        self.client = client
        self.settings = context.settings
        self.service = context.service
        self.guild_id = context.settings.guild_id
        self._ingestion_lock = asyncio.Lock()
        self._recovery_epoch = 0
        self._recovered_epoch = -1
        self._last_period: datetime | None = None
        self._last_cleanup: datetime | None = None
        self.active = True
        self.task: asyncio.Task[None] | None = None

    @property
    def user(self) -> discord.ClientUser | None:
        return self.client.user

    def get_guild(self, guild_id: int) -> discord.Guild | None:
        return self.client.get_guild(guild_id) if guild_id == self.guild_id and self.active else None

    def get_channel(self, channel_id: int) -> Any:
        channel = self.client.get_channel(channel_id)
        return channel if channel is not None and getattr(getattr(channel, "guild", None), "id", None) == self.guild_id else None

    async def fetch_channel(self, channel_id: int) -> Any:
        channel = await self.client.fetch_channel(channel_id)
        if getattr(getattr(channel, "guild", None), "id", None) != self.guild_id:
            raise ValueError("Channel does not belong to this guild")
        return channel

    def is_ready(self) -> bool:
        guild = self.get_guild(self.guild_id)
        return self.active and self.client.is_ready() and guild is not None and not guild.unavailable

    def refresh(self, context: GuildContext) -> None:
        if context.settings.guild_id != self.guild_id:
            raise ValueError("Cannot rebind a guild worker to a different guild")
        self.settings, self.service = context.settings, context.service

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name=f"rating-guild-{self.guild_id}")

    async def run(self) -> None:
        while self.active and not self.client.is_closed():
            await self.client.wait_until_ready()
            if self.is_ready():
                try:
                    await self.cycle()
                except Exception:
                    logger.exception("Guild %s background cycle failed; retrying", self.guild_id)
            await asyncio.sleep(30)

    async def stop(self) -> None:
        self.active = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                logger.debug("Guild %s worker stopped", self.guild_id)


class MultiGuildBot(discord.Client):
    def __init__(self, settings: Settings, registry: GuildRegistry) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        connector = aiohttp.TCPConnector(keepalive_timeout=75) if settings.discord_proxy else None
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(),
                         proxy=settings.discord_proxy, connector=connector)
        self.settings = settings
        self.registry = registry
        self.tree = app_commands.CommandTree(self)
        self.workers: dict[int, GuildWorker] = {}
        self.groups: dict[int, RatingCommands] = {}
        self._synced: set[int] = set()
        self._registration_locks: dict[int, asyncio.Lock] = {}
        self._worker_locks: dict[int, asyncio.Lock] = {}
        self._supervisor: asyncio.Task[None] | None = None
        self._keepwarm: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        self._supervisor = asyncio.create_task(self.supervise(), name="rating-guild-supervisor")
        if self.settings.discord_proxy:
            self._keepwarm = asyncio.create_task(self.keep_http_warm(), name="discord-http-keepwarm")

    keep_http_warm = RatingBot.keep_http_warm

    async def register_guild(self, guild: discord.Guild) -> None:
        lock = self._registration_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            if guild.id not in self.groups:
                context = await self.registry.get(guild.id)
                group_settings = context.settings if context else self.settings.model_copy(update={"guild_id": guild.id})
                group = RatingCommands(context.service if context else None, group_settings,
                                       registry=self.registry, client=self, guild_id=guild.id)
                self.tree.add_command(group, guild=discord.Object(id=guild.id), override=True)
                self.groups[guild.id] = group
            if guild.id not in self._synced:
                await self.tree.sync(guild=discord.Object(id=guild.id))
                self._synced.add(guild.id)
                logger.info("Guild %s commands synchronized", guild.id)
        await self.refresh_guild(guild.id)

    async def refresh_guild(self, guild_id: int) -> GuildWorker | None:
        lock = self._worker_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            if self.get_guild(guild_id) is None:
                return None
            context = await self.registry.get(guild_id)
            if context is None:
                return None
            group = self.groups.get(guild_id)
            if group is not None:
                group.settings, group.service = context.settings, context.service
            worker = self.workers.get(guild_id)
            if worker is None or not worker.active:
                worker = GuildWorker(self, context)
                self.workers[guild_id] = worker
            else:
                worker.refresh(context)
            worker.start()
            return worker

    async def supervise(self) -> None:
        while not self.is_closed():
            await self.wait_until_ready()
            # One unavailable guild never prevents configuration or activity in others.
            for guild in list(self.guilds):
                try:
                    await self.register_guild(guild)
                except Exception:
                    logger.exception("Unable to initialize guild %s; retrying", guild.id)
            await asyncio.sleep(30)

    async def on_ready(self) -> None:
        for worker in self.workers.values():
            worker._recovery_epoch += 1
        logger.info("Discord connected as %s; joined guilds=%s", self.user.id if self.user else None, len(self.guilds))
        for guild in list(self.guilds):
            try:
                await self.register_guild(guild)
            except Exception:
                logger.exception("Initial guild %s command registration failed; supervisor will retry", guild.id)

    async def on_disconnect(self) -> None:
        for worker in self.workers.values():
            worker._recovery_epoch += 1

    async def on_resumed(self) -> None:
        for worker in self.workers.values():
            worker._recovery_epoch += 1

    async def on_guild_join(self, guild: discord.Guild) -> None:
        try:
            await self.register_guild(guild)
        except Exception:
            logger.exception("Joined guild %s setup command registration failed; supervisor will retry", guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        lock = self._worker_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            worker = self.workers.pop(guild.id, None)
            if worker is not None:
                await worker.stop()
        self.groups.pop(guild.id, None)
        self._synced.discard(guild.id)
        self.tree.remove_command("rating", guild=discord.Object(id=guild.id))
        logger.info("Guild %s left: processing stopped; stored data retained", guild.id)

    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        worker = self.workers.get(guild.id)
        if worker is not None:
            worker._recovery_epoch += 1

    async def on_guild_available(self, guild: discord.Guild) -> None:
        worker = self.workers.get(guild.id)
        if worker is not None:
            worker._recovery_epoch += 1

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        # Joining a guild never opts it in. Workers are created only during
        # registration of a configured guild or after successful /rating setup.
        # Avoid registry IO for every message in unconfigured guilds.
        worker = self.workers.get(message.guild.id)
        if worker is not None and worker.active and worker.is_ready():
            await worker.on_message(message)

    async def close(self) -> None:
        for task in (self._supervisor, self._keepwarm):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug("Multi-guild shared worker stopped")
        await asyncio.gather(*(worker.stop() for worker in self.workers.values()))
        self.workers.clear()
        await super().close()
