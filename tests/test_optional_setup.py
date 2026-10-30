"""Optional role/log setup must not turn moderation into public access."""
from types import SimpleNamespace

import pytest
from bot.config import Settings
from bot.discord_app import has_staff_role
from bot.services import DomainError


def test_minimal_settings_without_role_or_log_channel():
    settings = Settings(_env_file=None, discord_token="test", guild_id=123,
                        daily_report_channel_id=456,
                        admin_role_ids="", moderator_role_ids="",
                        moderation_log_channel_id="")
    assert settings.admin_role_ids == set()
    assert settings.moderator_role_ids == set()
    assert settings.moderation_log_channel_id is None


def test_guild_and_report_channel_optional_until_setup():
    settings = Settings(_env_file=None, discord_token="test", guild_id="",
                        daily_report_channel_id="", admin_role_ids="",
                        moderation_log_channel_id="")
    assert settings.guild_id is None
    assert settings.daily_report_channel_id is None


def member(*, administrator=False, role_ids=()):
    return SimpleNamespace(
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        guild_permissions=SimpleNamespace(administrator=administrator),
    )


def test_empty_roles_only_allow_discord_administrators(settings):
    settings.admin_role_ids = set()
    settings.moderator_role_ids = set()
    assert not has_staff_role(member(), settings)
    assert not has_staff_role(member(role_ids=[999]), settings)
    assert not has_staff_role(SimpleNamespace(roles=[]), settings)
    assert has_staff_role(member(administrator=True), settings)
    assert has_staff_role(member(administrator=True), settings, admin_only=True)


def test_configured_admin_roles_remain_authoritative(settings):
    assert not has_staff_role(member(administrator=True), settings, admin_only=True)
    assert not has_staff_role(member(administrator=True), settings)
    assert has_staff_role(member(role_ids=[20]), settings, admin_only=True)
    assert has_staff_role(member(role_ids=[10]), settings)
    assert not has_staff_role(member(role_ids=[10]), settings, admin_only=True)


@pytest.mark.asyncio
async def test_no_roles_does_not_allow_everyone(service, settings):
    settings.admin_role_ids = set()
    settings.moderator_role_ids = set()
    with pytest.raises(DomainError):
        await service.punish(1, 2, "Причина", 700, set())
    with pytest.raises(DomainError):
        await service.punish(1, 2, "Причина", 701, {999}, actor_is_admin=False)
    assert (await service.profile(1))["balance"] == 800


@pytest.mark.asyncio
async def test_admin_fallback_can_punish_and_undo_without_log(service, settings):
    settings.admin_role_ids = set()
    settings.moderator_role_ids = set()
    settings.moderation_log_channel_id = None
    original = await service.punish(1, 2, "Причина", 700, set(), actor_is_admin=True)
    assert (await service.profile(1))["balance"] == 750
    assert await service.pending_notifications() == []
    with pytest.raises(DomainError):
        await service.undo(original["id"], 2, "Отмена", 701, set())
    await service.undo(original["id"], 2, "Отмена", 702, set(), actor_is_admin=True)
    assert (await service.profile(1))["balance"] == 800
    assert len(await service.history(1)) == 3
    assert await service.pending_notifications() == []


@pytest.mark.asyncio
async def test_configured_roles_disable_core_administrator_fallback(service):
    with pytest.raises(DomainError):
        await service.punish(1, 2, "Причина", 700, set(), actor_is_admin=True)
    original = await service.punish(1, 2, "Причина", 701, {10})
    with pytest.raises(DomainError):
        await service.undo(original["id"], 2, "Отмена", 702, set(), actor_is_admin=True)
    assert (await service.profile(1))["balance"] == 750


@pytest.mark.asyncio
async def test_disabled_log_backlog_cannot_starve_daily_report(service, settings, instant):
    from datetime import timedelta
    from bot.periods import period_bounds

    for index in range(25):
        await service.punish(1, 2, "Причина", 800 + index, {10})
    settings.moderation_log_channel_id = None
    for index in range(5):
        await service.record_message(900 + index, 1, instant)
    _, end = period_bounds(instant)
    await service.settle_due(end + timedelta(seconds=1))
    rows = await service.pending_notifications()
    assert len(rows) == 1
    assert rows[0]["kind"] == "daily"
    assert (await service.profile(1))["balance"] == -425
    settings.moderation_log_channel_id = 789
    assert len(await service.pending_notifications(limit=50)) == 26


def test_optional_proxy_blank_and_valid(settings):
    assert settings.discord_proxy is None
    blank = type(settings)(**(settings.model_dump() | {'discord_proxy': ''}))
    assert blank.discord_proxy is None
    configured = type(settings)(**(settings.model_dump() | {'discord_proxy': 'http://localhost:8080'}))
    assert configured.discord_proxy == 'http://localhost:8080'


def test_proxy_rejects_unsupported_protocol_without_leaking(settings):
    from pydantic import ValidationError
    import pytest
    with pytest.raises(ValidationError) as error:
        type(settings)(**(settings.model_dump() | {'discord_proxy': 'ftp://private-proxy.invalid'}))
    assert 'private-proxy' not in str(error.value)
