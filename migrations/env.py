import asyncio
import os
from pathlib import Path
from logging.config import fileConfig
from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.engine import make_url
from dotenv import load_dotenv
from bot.database.models import Base

load_dotenv()
config = context.config
url = os.getenv('DATABASE_URL', config.get_main_option('sqlalchemy.url'))
config.set_main_option('sqlalchemy.url', url.replace('%', '%%'))
database = make_url(url).database
if database and database != ':memory:':
    Path(database).parent.mkdir(parents=True, exist_ok=True)
if config.get_section('loggers'):
    fileConfig(config.config_file_name)


def run_migrations(connection):
    # Python's SQLite legacy transaction mode does not BEGIN for DDL. Start an
    # explicit write transaction so table rebuilds and version updates roll back
    # together on failure, instead of leaving partially applied migrations.
    connection.exec_driver_sql('BEGIN IMMEDIATE')
    try:
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


async def run_online():
    engine = async_engine_from_config(config.get_section(config.config_ini_section), prefix='sqlalchemy.', poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
