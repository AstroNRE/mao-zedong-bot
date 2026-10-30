"""Entrypoint: python -m bot.main. Apply Alembic migrations before starting."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import logging
from pathlib import Path
import sys
from typing import Iterator

from pydantic import ValidationError
from sqlalchemy.engine import make_url

from bot.config import Settings
from bot.guilds import GuildRegistry
from bot.multi_discord import MultiGuildBot


@contextmanager
def instance_lock(database_url: str) -> Iterator[None]:
    """Keep one bot/outbox sender per local database; OS releases lock on crash."""
    database_path = Path(make_url(database_url).database or "rating.db").resolve()
    lock_path = database_path.with_suffix(database_path.suffix + ".bot.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if sys.platform == "win32":
            import msvcrt
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Бот уже запущен для этой базы данных.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Бот уже запущен для этой базы данных.") from exc
        try:
            yield
        finally:
            if sys.platform == "win32":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


async def run() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with instance_lock(settings.database_url):
        registry = GuildRegistry(settings)
        try:
            await registry.initialize()
            # Existing data has one known origin; never bind it to a newly invited guild.
            if settings.legacy_guild_id is not None and settings.guild_id == settings.legacy_guild_id and settings.daily_report_channel_id:
                await registry.import_legacy(settings.guild_id, settings.daily_report_channel_id)
            async with MultiGuildBot(settings, registry) as client:
                await client.start(settings.discord_token)
        finally:
            await registry.close()



def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Stopped by operator")
    except ValidationError as error:
        # Never echo Pydantic's input values: a bad configuration may contain secrets.
        details = "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors(include_input=False))
        raise SystemExit("Некорректная конфигурация: " + details) from None


if __name__ == "__main__":
    main()
