"""Durable opt-in configuration and physically isolated per-guild databases."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys

from sqlalchemy.engine import make_url

from .config import Settings
from .database import Database
from .services import RatingService
from .periods import utcnow, iso

@dataclass
class GuildContext:
    settings: Settings
    service: RatingService
    db: Database


class GuildRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        source = Path(make_url(settings.database_url).database or 'rating.db').resolve()
        self.legacy_path = source
        self.root = source.parent / (source.stem + '.guilds')
        self.registry_path = self.root / 'registry.db'
        self._contexts: dict[int, GuildContext] = {}
        self._lock = asyncio.Lock()

    async def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.registry_path) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS guild_config (guild_id INTEGER PRIMARY KEY, config TEXT NOT NULL, database_path TEXT NOT NULL, configured_at TEXT NOT NULL)')
        self.registry_path.chmod(0o600)

    def _row(self, guild_id):
        with sqlite3.connect(self.registry_path) as connection:
            return connection.execute('SELECT config,database_path,configured_at FROM guild_config WHERE guild_id=?', (guild_id,)).fetchone()

    async def guild_ids(self):
        with sqlite3.connect(self.registry_path) as connection:
            return [row[0] for row in connection.execute('SELECT guild_id FROM guild_config ORDER BY guild_id')]

    def _settings(self, guild_id, config, path):
        values = self.settings.model_dump()
        values.update(config, guild_id=guild_id, database_url='sqlite+aiosqlite:///' + str(path))
        return Settings(**values)

    async def _open(self, guild_id, config, path):
        settings = self._settings(guild_id, config, path)
        database = Database(settings.database_url)
        try:
            await database.initialize()
            await database.verify_schema()
        except BaseException:
            await database.close()
            raise
        return GuildContext(settings, RatingService(database, settings), database)

    async def get(self, guild_id: int):
        if guild_id in self._contexts:
            return self._contexts[guild_id]
        async with self._lock:
            if guild_id in self._contexts:
                return self._contexts[guild_id]
            row = self._row(guild_id)
            if row is None:
                return None
            context = await self._open(guild_id, json.loads(row[0]), Path(row[1]))
            self._contexts[guild_id] = context
            return context

    async def _migrate(self, path):
        environment = os.environ.copy()
        environment.pop('DISCORD_TOKEN', None)
        environment['DATABASE_URL'] = 'sqlite+aiosqlite:///' + str(path)
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'alembic', 'upgrade', 'head',
            cwd=str(Path(__file__).resolve().parents[1]), env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.communicate()
        if process.returncode:
            # Do not echo subprocess environment/configuration in public errors.
            raise RuntimeError('Guild database migration failed; inspect schema deployment')
        path.chmod(0o600)

    async def configure(self, guild_id: int, report_channel_id: int, *,
                        admin_role_ids: set[int] | None = None,
                        moderator_role_ids: set[int] | None = None,
                        moderation_log_channel_id: int | None = None,
                        now: datetime | None = None):
        if guild_id <= 0 or report_channel_id <= 0:
            raise ValueError('Positive guild/channel identifiers required')
        config = {'daily_report_channel_id': report_channel_id,
                  'admin_role_ids': sorted(admin_role_ids or []),
                  'moderator_role_ids': sorted(moderator_role_ids or []),
                  'moderation_log_channel_id': moderation_log_channel_id}
        now = now or utcnow()
        async with self._lock:
            old = self._row(guild_id)
            path = Path(old[1]) if old else self.root / f'{guild_id}.db'
            settings = self._settings(guild_id, config, path)  # validate before writes
            context = self._contexts.get(guild_id)
            if old is None:
                await self._migrate(path)
            if context is None:
                context = await self._open(guild_id, config, path)
            if old is None:
                await context.service.set_state('opt_in_at', iso(now))
                await context.service.set_state('tracking_started', iso(now))
                await context.service.mark_incomplete(now, now, 'Настройка сервера: учитываются сообщения только после согласия администратора.')
            with sqlite3.connect(self.registry_path) as connection:
                connection.execute('INSERT INTO guild_config VALUES (?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET config=excluded.config',
                                   (guild_id, json.dumps(config), str(path), old[2] if old else iso(now)))
            context.settings = settings
            context.service.settings = settings
            self._contexts[guild_id] = context
            return context

    async def import_legacy(self, guild_id: int, report_channel_id: int):
        """Bind existing single-guild data only to its known original server."""
        if self.settings.legacy_guild_id is None or guild_id != self.settings.legacy_guild_id:
            raise ValueError('Legacy import requires explicit LEGACY_GUILD_ID matching its original guild')
        if not self.legacy_path.is_file():
            return None
        async with self._lock:
            if self._row(guild_id) is not None:
                return None
            await self._migrate(self.legacy_path)
            config = {'daily_report_channel_id': report_channel_id,
                      'admin_role_ids': sorted(self.settings.admin_role_ids),
                      'moderator_role_ids': sorted(self.settings.moderator_role_ids),
                      'moderation_log_channel_id': self.settings.moderation_log_channel_id}
            self._settings(guild_id, config, self.legacy_path)
            with sqlite3.connect(self.registry_path) as connection:
                connection.execute('INSERT INTO guild_config VALUES (?,?,?,?)',
                                   (guild_id, json.dumps(config), str(self.legacy_path), iso(utcnow())))
        return await self.get(guild_id)

    async def close(self):
        for context in self._contexts.values():
            await context.db.close()
        self._contexts.clear()
