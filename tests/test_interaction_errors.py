"""An expired Discord acknowledgment must not apply a command or retry a dead token."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord import app_commands
import pytest

from bot.discord_app import RatingCommands
from bot.services import DomainError


def interaction():
    return SimpleNamespace(
        created_at=datetime.now(UTC), user=SimpleNamespace(id=1),
        command=SimpleNamespace(qualified_name="rating profile"),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=False)),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
    )


def expired_response():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"),
                            {"code": 10062, "message": "Unknown interaction"})


@pytest.mark.asyncio
async def test_expired_profile_ack_never_calls_database_or_retries_reply(settings):
    service = SimpleNamespace(profile=AsyncMock())
    commands = RatingCommands(service, settings)
    request = interaction()
    request.response.defer.side_effect = expired_response()
    with pytest.raises(discord.NotFound) as error:
        await commands.profile.callback(commands, request)
    service.profile.assert_not_awaited()
    request.followup.send.assert_not_awaited()
    await commands.on_error(request, app_commands.CommandInvokeError(commands.profile, error.value))
    request.response.send_message.assert_not_awaited()
    request.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_ack_precedes_database(settings):
    calls = []
    request = interaction()

    async def defer(**kwargs):
        calls.append("ack")

    async def profile(user_id):
        calls.append("database")
        assert calls == ["ack", "database"]
        return {"discord_id": user_id, "balance": 0, "awarded_days": 0,
                "active_punishments": 0, "message_count": 0, "credit_balance": 0}

    request.response.defer.side_effect = defer
    service = SimpleNamespace(profile=AsyncMock(side_effect=profile))
    commands = RatingCommands(service, settings)
    await commands.profile.callback(commands, request)
    assert calls == ["ack", "database"]
    request.response.defer.assert_awaited_once_with(ephemeral=False)
    request.followup.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_acked", [False, True])
async def test_command_error_reply_http_failure_is_contained(settings, already_acked):
    commands = RatingCommands(SimpleNamespace(), settings)
    request = interaction()
    request.response.is_done.return_value = already_acked
    response_error = discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "Temporary failure")
    sender = request.followup.send if already_acked else request.response.send_message
    sender.side_effect = response_error
    wrapped = app_commands.CommandInvokeError(commands.profile, DomainError("Причина обязательна"))
    await commands.on_error(request, wrapped)
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_moderation_confirmations_are_public_embeds(service, settings):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    from bot.discord_app import RatingCommands
    commands = RatingCommands(service, settings)
    caller = SimpleNamespace(id=2, roles=[SimpleNamespace(id=20)],
                             guild_permissions=SimpleNamespace(administrator=False))
    interaction = SimpleNamespace(
        id=9001, user=caller, created_at=datetime.now(timezone.utc),
        command=SimpleNamespace(qualified_name='rating punish'),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    target = SimpleNamespace(id=1)
    await commands.punish.callback(commands, interaction, target, 'Проверка')
    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs['ephemeral'] is False
    embed = kwargs['embed']
    fields = {field.name: field.value for field in embed.fields}
    assert fields['Участник'] == '<@1>'
    assert fields['Причина'] == 'Проверка'
    assert fields['Изменение'] == '-50'
    assert fields['Рейтинг после операции'] == '750'
    punishment_id = fields['ID операции']
    interaction.id = 9002
    interaction.followup.send.reset_mock()
    await commands.undo.callback(commands, interaction, punishment_id, 'Отмена проверки')
    kwargs = interaction.followup.send.call_args.kwargs
    fields = {field.name: field.value for field in kwargs['embed'].fields}
    assert kwargs['ephemeral'] is False
    assert fields['Изменение'] == '+50'
    assert fields['Рейтинг после операции'] == '800'
    assert fields['Исходное взыскание'] == punishment_id


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['profile', 'user'])
async def test_profiles_are_public(settings, name):
    svc = SimpleNamespace(profile=AsyncMock(return_value={'discord_id': 1, 'balance': 800, 'credit_balance': 0}))
    commands = RatingCommands(svc, settings)
    request = interaction()
    command = getattr(commands, name)
    args = (SimpleNamespace(id=1),) if name == 'user' else ()
    await command.callback(commands, request, *args)
    request.response.defer.assert_awaited_once_with(ephemeral=False)
    assert request.followup.send.call_args.kwargs['ephemeral'] is False
    assert len(request.followup.send.call_args.kwargs['embed'].fields) == 3


@pytest.mark.asyncio
async def test_public_profile_error_has_only_generic_public_notice(settings):
    commands = RatingCommands(SimpleNamespace(), settings)
    request = interaction()
    request.response.is_done.return_value = True
    await commands.on_error(request, app_commands.CommandInvokeError(commands.profile, DomainError('Private detail')))
    request.edit_original_response.assert_awaited_once_with(content='Не удалось выполнить команду.', embed=None, view=None)
    assert request.followup.send.call_args.kwargs['ephemeral'] is True
    assert request.followup.send.call_args.args[0] == 'Private detail'


@pytest.mark.asyncio
async def test_answered_check_failure_does_not_send_duplicate_error(settings):
    from discord import app_commands
    request = interaction()
    request.response.is_done.return_value = True
    commands = RatingCommands(SimpleNamespace(), settings)
    await commands.on_error(request, app_commands.CheckFailure('Setup required'))
    request.followup.send.assert_not_awaited()
    request.response.send_message.assert_not_awaited()
