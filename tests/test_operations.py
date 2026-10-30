import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from bot.discord_app import has_staff_role, private_log_channel
from bot.main import instance_lock


def test_migrations_create_and_upgrade_idempotently(tmp_path):
    database = tmp_path / "migrated.db"
    env = os.environ | {"DATABASE_URL": f"sqlite+aiosqlite:///{database}"}
    root = Path(__file__).resolve().parents[1]
    for _ in range(2):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                                cwd=root, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(database) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"users", "rating_transactions", "processed_messages", "daily_payouts", "outbox", "alembic_version"} <= tables
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_instance_lock_rejects_second_process_and_releases(settings):
    with instance_lock(settings.database_url):
        with pytest.raises(RuntimeError, match="уже запущен"):
            with instance_lock(settings.database_url):
                pytest.fail("Duplicate bot acquired lock")
    with instance_lock(settings.database_url):
        assert True


def role(role_id, admin=False):
    result = MagicMock(spec=discord.Role)
    result.id = role_id
    result.permissions = SimpleNamespace(administrator=admin)
    result.is_bot_managed.return_value = False
    result.tags = None
    return result


def private_channel():
    default = role(123)
    staff = role(10)
    channel = MagicMock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(default_role=default, roles=[default, staff])
    channel.permissions_for.side_effect = lambda target: SimpleNamespace(view_channel=target.id == 10)
    channel.overwrites = {}
    return channel


def test_private_log_channel(settings):
    assert private_log_channel(private_channel(), settings, bot_id=999)


def test_public_log_channel_rejected(settings):
    channel = private_channel()
    channel.permissions_for.side_effect = lambda _: SimpleNamespace(view_channel=True)
    assert not private_log_channel(channel, settings, bot_id=999)


def test_uncached_member_overwrite_rejected(settings):
    channel = private_channel()
    channel.overwrites = {discord.Object(id=888): discord.PermissionOverwrite(view_channel=True)}
    assert not private_log_channel(channel, settings, bot_id=999)


def test_staff_role_ids_not_names(settings):
    member = SimpleNamespace(roles=[SimpleNamespace(id=999, name="Admin")])
    assert not has_staff_role(member, settings)
    member.roles = [SimpleNamespace(id=10, name="Anything")]
    assert has_staff_role(member, settings)
    assert not has_staff_role(member, settings, admin_only=True)
    member.roles = [SimpleNamespace(id=20)]
    assert has_staff_role(member, settings, admin_only=True)


def test_invalid_configuration_does_not_print_input_secrets(monkeypatch):
    import bot.main as entrypoint
    from pydantic import ValidationError

    token = "sensitive-token-that-must-not-appear"
    failure = ValidationError.from_exception_data("Settings", [
        {"type": "int_parsing", "loc": ("guild_id",), "input": token}
    ])

    def reject_settings():
        raise failure

    monkeypatch.setattr(entrypoint, "Settings", reject_settings)
    with pytest.raises(SystemExit) as result:
        entrypoint.main()
    assert token not in str(result.value)
    assert "guild_id" in str(result.value)
