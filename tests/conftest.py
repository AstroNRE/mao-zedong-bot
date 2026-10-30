from datetime import datetime, timezone

import pytest
import pytest_asyncio

from bot.config import Settings
from bot.database import Base, Database
from bot.services import RatingService


@pytest.fixture
def settings(tmp_path):
    return Settings(
        discord_token="test-token", guild_id=123,
        moderator_role_ids={10}, admin_role_ids={20},
        daily_report_channel_id=456, moderation_log_channel_id=789,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'rating.db'}",
    )


@pytest_asyncio.fixture
async def service(settings):
    db = Database(settings.database_url)
    await db.initialize()
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    svc = RatingService(db, settings)
    yield svc
    await db.close()


@pytest.fixture
def instant():
    return datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch, instant):
    monkeypatch.setattr("bot.services.utcnow", lambda: instant)
