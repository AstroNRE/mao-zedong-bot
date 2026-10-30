"""Only authorized successful mutations receive public receipts."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from discord import app_commands
import pytest

from bot.discord_app import RatingCommands
from bot.services import DomainError


def request(name, *, authorized=True):
    return SimpleNamespace(
        id=900, created_at=datetime.now(UTC),
        user=SimpleNamespace(id=2, roles=[SimpleNamespace(id=20)] if authorized else [],
                             guild_permissions=SimpleNamespace(administrator=False)),
        command=SimpleNamespace(qualified_name=f"rating {name}"),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=False)),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
    )


def arguments(name, reason="Проверка", amount=25):
    if name == "undo":
        return ("original-op", reason)
    if name == "give":
        return (SimpleNamespace(id=1), amount, reason)
    return (SimpleNamespace(id=1), reason)


def service_mock(name):
    result = dict(id="receipt-op", discord_id=1, moderator_id=2, amount=-50 if name == "punish" else 50,
                  balance_after=750 if name == "punish" else 850, reason="Проверка",
                  created_at=datetime.now(UTC), reverses_id="original-op" if name == "undo" else None)
    return SimpleNamespace(**{name: AsyncMock(return_value=result)})


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["give", "punish", "undo"])
async def test_success_receipt_public_after_ack(settings, name):
    svc = service_mock(name)
    commands = RatingCommands(svc, settings)
    event = request(name)

    async def verify_ack(*args, **kwargs):
        event.response.defer.assert_awaited_once_with(ephemeral=False)
        return getattr(svc, name).return_value

    getattr(svc, name).side_effect = verify_ack
    await getattr(commands, name).callback(commands, event, *arguments(name))
    event.response.send_message.assert_not_awaited()
    event.followup.send.assert_awaited_once()
    kwargs = event.followup.send.call_args.kwargs
    assert kwargs["ephemeral"] is False
    fields = {f.name: f.value for f in kwargs["embed"].fields}
    assert fields["Участник"] == "<@1>"
    assert fields["Сотрудник"] == "<@2>"
    assert fields["Причина"] == "Проверка"
    assert fields["ID операции"] == "receipt-op"
    if name == "undo":
        assert fields["Исходное взыскание"] == "original-op"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["give", "punish", "undo"])
async def test_unauthorized_mutation_never_publicly_acknowledged(settings, name):
    svc = service_mock(name)
    commands = RatingCommands(svc, settings)
    event = request(name, authorized=False)
    await getattr(commands, name).callback(commands, event, *arguments(name))
    event.response.defer.assert_not_awaited()
    event.followup.send.assert_not_awaited()
    getattr(svc, name).assert_not_awaited()
    assert event.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["give", "punish", "undo"])
@pytest.mark.parametrize("reason", ["   ", "x" * 501])
async def test_invalid_reason_rejected_privately_before_defer(settings, name, reason):
    svc = service_mock(name)
    commands = RatingCommands(svc, settings)
    event = request(name)
    await getattr(commands, name).callback(commands, event, *arguments(name, reason=reason))
    event.response.defer.assert_not_awaited()
    event.followup.send.assert_not_awaited()
    getattr(svc, name).assert_not_awaited()
    assert event.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0, -1, 1_000_001, True, 1.5])
async def test_invalid_grant_amount_private_before_defer(settings, amount):
    svc = service_mock("give")
    commands = RatingCommands(svc, settings)
    event = request("give")
    await commands.give.callback(commands, event, *arguments("give", amount=amount))
    event.response.defer.assert_not_awaited()
    event.followup.send.assert_not_awaited()
    svc.give.assert_not_awaited()
    assert event.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["give", "punish", "undo"])
async def test_post_ack_mutation_error_details_remain_private(settings, name):
    commands = RatingCommands(SimpleNamespace(), settings)
    event = request(name)
    event.response.is_done.return_value = True
    error = DomainError("Private ledger detail")
    await commands.on_error(event, app_commands.CommandInvokeError(getattr(commands, name), error))
    event.edit_original_response.assert_awaited_once()
    assert "Private ledger detail" not in str(event.edit_original_response.call_args)
    assert event.followup.send.call_args.args[0] == "Private ledger detail"
    assert event.followup.send.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["history", "status", "log"])
async def test_sensitive_read_commands_remain_private(settings, name):
    svc = SimpleNamespace(history=AsyncMock(return_value=[]), status=AsyncMock(return_value={}),
                          get_state=AsyncMock(return_value=None))
    commands = RatingCommands(svc, settings)
    event = request(name)
    event.guild = SimpleNamespace(fetch_member=AsyncMock(return_value=event.user))
    args = (SimpleNamespace(id=1),) if name == "log" else ()
    await getattr(commands, name).callback(commands, event, *args)
    event.response.defer.assert_awaited_once_with(ephemeral=True)
    assert event.followup.send.call_args.kwargs["ephemeral"] is True
