"""One session per transaction; BEGIN IMMEDIATE serializes SQLite writers across processes."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.exc import OperationalError


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, connect_args={'timeout': 30})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._writer = asyncio.Lock()
        @event.listens_for(self.engine.sync_engine, 'connect')
        def configure(connection, record):
            cursor = connection.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.execute('PRAGMA busy_timeout=30000')
            cursor.close()

    async def initialize(self):
        database = self.engine.url.database
        if database and database != ':memory:':
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.connect() as connection:
            await connection.execute(text('PRAGMA journal_mode=WAL'))
            await connection.commit()

    @asynccontextmanager
    async def write(self):
        async with self._writer:
            async with self.sessions() as session:
                for attempt in range(5):
                    try:
                        await session.execute(text('BEGIN IMMEDIATE'))
                        break
                    except OperationalError as exc:
                        await session.rollback()
                        if 'locked' not in str(exc).lower() or attempt == 4:
                            raise
                        await asyncio.sleep(0.1 * 2 ** attempt)
                try:
                    yield session
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise

    async def verify_schema(self):
        """Fail before connecting to Discord when deployment skipped migrations."""
        from sqlalchemy import inspect
        from .models import Base
        async with self.engine.connect() as connection:
            names = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
            if not set(Base.metadata.tables).issubset(names) or "alembic_version" not in names:
                raise RuntimeError("Database schema missing. Run: python -m alembic upgrade head")
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != "f4c00001":
                raise RuntimeError("Database migration version differs. Run: python -m alembic upgrade head")

    async def close(self):
        await self.engine.dispose()
