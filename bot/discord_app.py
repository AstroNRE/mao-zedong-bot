"""Discord adapter: guild-scoped commands, recovery, and durable notifications."""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands

from bot.config import Settings
from bot.periods import period_bounds
from bot.recovery import accepts_message, reconcile_history
from bot.services import DomainError, RatingService, MAX_MANUAL_GRANT

logger = logging.getLogger(__name__)
PAGE_SIZE = 8
LEADERBOARD_PAGE_SIZE = 10


async def defer_response(interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
    """Acknowledge before any DB work; measure transport separately from command work."""
    started = time.monotonic()
    age = (datetime.now(UTC) - interaction.created_at).total_seconds()
    command_name = getattr(getattr(interaction, "command", None), "qualified_name", "component")
    try:
        await interaction.response.defer(ephemeral=ephemeral)
    finally:
        logger.info("Interaction ACK command=%s age_at_start=%.3fs request_duration=%.3fs",
                    command_name, age, time.monotonic() - started)


def actor_roles(member: Any) -> set[int]:
    return {role.id for role in getattr(member, "roles", ())}


def has_staff_role(member: Any, settings: Settings, *, admin_only: bool = False) -> bool:
    allowed = settings.admin_role_ids if admin_only else settings.admin_role_ids | settings.moderator_role_ids
    fallback_admin = (not settings.admin_role_ids
                      and getattr(getattr(member, "guild_permissions", None), "administrator", False) is True)
    return bool(actor_roles(member) & allowed or fallback_admin)


def private_log_channel(channel: discord.TextChannel, settings: Settings, bot_id: int) -> bool:
    """Fail closed if ordinary roles or explicit nonstaff members can see logs.

    Guild owner/Administrator bypasses Discord channel restrictions by design.
    Only bot's own identity and configured staff roles may be explicitly allowed.
    """
    guild = channel.guild
    if channel.permissions_for(guild.default_role).view_channel:
        return False
    allowed = settings.admin_role_ids | settings.moderator_role_ids
    for role in guild.roles:
        if role.id == guild.default_role.id or role.id in allowed or role.permissions.administrator:
            continue
        if channel.permissions_for(role).view_channel:
            # The bot's own integration role is the only nonstaff exception.
            if not (role.is_bot_managed() and role.tags and role.tags.bot_id == bot_id):
                return False
    for target, overwrite in channel.overwrites.items():
        if overwrite.view_channel is not True or target.id == bot_id:
            continue
        if isinstance(target, discord.Member):
            if not has_staff_role(target, settings) and not target.guild_permissions.administrator:
                return False
        elif not isinstance(target, discord.Role):
            # Without members intent, uncached user overwrites are discord.Object.
            # Unknown identities cannot be assumed to be staff.
            return False
    return True


def display_time(value: str | datetime | None) -> str:
    if value is None:
        return "Нет"
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"<t:{int(moment.timestamp())}:f>"


def safe_text(value: Any, limit: int = 500) -> str:
    return discord.utils.escape_markdown(str(value))[:limit]


def social_class(balance: int) -> tuple[str, discord.Color]:
    if balance < 500:
        return 'Неблагонадёжный', discord.Color.red()
    if balance < 800:
        return 'Под наблюдением', discord.Color.orange()
    if balance < 1100:
        return 'Обычный гражданин', discord.Color.blue()
    if balance < 1500:
        return 'Благонадёжный', discord.Color.green()
    return 'Образцовый гражданин', discord.Color.gold()


def profile_embed(data: dict[str, Any], settings: Settings) -> discord.Embed:
    label, color = social_class(data['balance'])
    embed = discord.Embed(title="Социальный рейтинг", color=color)
    embed.description = f"<@{data['discord_id']}>"
    embed.add_field(name="Социальный рейтинг", value=str(data['balance']))
    embed.add_field(name="Социальный класс", value=label)
    embed.add_field(name="Кредиты", value=str(data["credit_balance"]))
    return embed


def history_embed(rows: list[dict[str, Any]], page: int) -> discord.Embed:
    embed = discord.Embed(title="История рейтинга и кредитов", color=discord.Color.blue())
    kinds = {"daily": "Ежедневное начисление", "punishment": "Взыскание", "undo": "Отмена взыскания", "manual_grant": "Выдача рейтинга", "opening": "Стартовый рейтинг", "credit_daily": "Ежедневное начисление кредитов"}
    for row in rows:
        reason = safe_text(row.get("reason") or "Активность за расчётный период")
        unit = "кредитов" if row["type"] == "credit_daily" else "рейтинга"
        balance = f"\nБаланс после: {row['balance_after']} {unit}" if "balance_after" in row else ""
        embed.add_field(
            name=f"{kinds.get(row['type'], row['type'])}: {row['amount']:+d} {unit}",
            value=f"{display_time(row['created_at'])}\n{reason}{balance}\nID: {row['id']}",
            inline=False,
        )
    if not rows:
        embed.description = "Операций пока нет."
    embed.set_footer(text=f"Страница {page + 1}")
    return embed


class Pager(discord.ui.View):
    def __init__(self, owner_id: int, load: Callable[[int], Awaitable[tuple[discord.Embed, bool]]]) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.load = load
        self.page = 0
        self._lock = asyncio.Lock()
        self.previous.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Эти кнопки доступны автору команды.", ephemeral=True)
            return False
        return True

    async def move(self, interaction: discord.Interaction, direction: int) -> None:
        await defer_response(interaction)
        async with self._lock:
            self.page = max(0, self.page + direction)
            embed, has_next = await self.load(self.page)
            self.previous.disabled = self.page == 0
            self.next.disabled = not has_next
            await interaction.edit_original_response(embed=embed, view=self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error("Pagination failed", exc_info=(type(error), error, error.__traceback__))
        await interaction.followup.send("Не удалось загрузить страницу. Попробуйте команду ещё раз.", ephemeral=True)

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.move(interaction, -1)

    @discord.ui.button(label="Далее", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.move(interaction, 1)


@app_commands.guild_only()
class RatingCommands(app_commands.Group):
    def __init__(self, service: RatingService | None, settings: Settings, *,
                 registry: Any = None, client: Any = None, guild_id: int | None = None) -> None:
        super().__init__(name="rating", description="Социальный рейтинг сообщества")
        self.service = service
        self.settings = settings
        self.registry = registry
        self.client = client
        self.guild_id = guild_id if guild_id is not None else settings.guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("Команды доступны только на соответствующем сервере.", ephemeral=True)
            return False
        is_setup = getattr(interaction.command, "name", None) == "setup"
        # Contexts are preloaded during guild registration and refreshed on setup.
        # Never wait for registry migration/IO before Discord's three-second ACK.
        if self.registry is not None and not is_setup and self.service is None:
            await interaction.response.send_message("Рейтинг ещё не включён. Администратор сервера должен выполнить /rating setup.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="setup", description="Включить или настроить рейтинг на этом сервере")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(report_channel="Канал ежедневных сводок", admin_role="Роль администраторов рейтинга (необязательно)",
                           moderator_role="Роль модераторов рейтинга (необязательно)",
                           moderation_log_channel="Закрытый канал взысканий (необязательно)")
    async def setup(self, interaction: discord.Interaction, report_channel: discord.TextChannel,
                    admin_role: discord.Role | None = None, moderator_role: discord.Role | None = None,
                    moderation_log_channel: discord.TextChannel | None = None) -> None:
        if getattr(getattr(interaction.user, "guild_permissions", None), "administrator", False) is not True:
            await interaction.response.send_message("Настройка доступна только обладателю разрешения Discord Administrator.", ephemeral=True)
            return
        if self.registry is None:
            await interaction.response.send_message("Для настройки через Discord запустите многосерверный режим бота.", ephemeral=True)
            return
        await defer_response(interaction, ephemeral=True)
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            raise DomainError("Настройка доступна только внутри соответствующего сервера.")
        for channel in (report_channel, moderation_log_channel):
            if channel is None:
                continue
            if channel.guild.id != guild.id or channel.type is not discord.ChannelType.text:
                raise DomainError("Выберите обычный текстовый канал этого сервера.")
            permissions = channel.permissions_for(guild.me)
            if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
                raise DomainError("Боту нужны View Channel, Send Messages и Embed Links в выбранных каналах.")
        for role in (admin_role, moderator_role):
            if role is not None and (role.guild.id != guild.id or role.id == guild.default_role.id):
                raise DomainError("Выберите отдельную роль этого сервера, не @everyone.")
        if moderation_log_channel is not None and report_channel.id == moderation_log_channel.id:
            raise DomainError("Канал сводок и закрытый журнал должны различаться.")
        admin_ids = {admin_role.id} if admin_role else set()
        moderator_ids = {moderator_role.id} if moderator_role else set()
        candidate = self.settings.model_copy(update={"guild_id": guild.id,
            "daily_report_channel_id": report_channel.id,
            "moderation_log_channel_id": moderation_log_channel.id if moderation_log_channel else None,
            "admin_role_ids": admin_ids, "moderator_role_ids": moderator_ids})
        if moderation_log_channel is not None and not private_log_channel(moderation_log_channel, candidate, interaction.client.user.id):
            raise DomainError("Журнал виден посторонним ролям или участникам. Сначала закройте доступ к каналу.")
        context = await self.registry.configure(guild_id=guild.id, report_channel_id=report_channel.id,
            admin_role_ids=admin_ids, moderator_role_ids=moderator_ids,
            moderation_log_channel_id=moderation_log_channel.id if moderation_log_channel else None,
            now=datetime.now(UTC))
        self.settings, self.service = context.settings, context.service
        if self.client is not None:
            await self.client.refresh_guild(guild.id)
        await interaction.followup.send(
            f"Рейтинг включён на этом сервере. Ежедневные сводки: <#{report_channel.id}>. "
            "Учёт начинается с момента первой настройки; предыдущая активность не начисляется. "
            "Повторная настройка сохраняет балансы и историю.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        cause = getattr(error, "original", error)
        if isinstance(cause, app_commands.CheckFailure) and interaction.response.is_done():
            # Our guild/opt-in check already sent the precise private explanation.
            return
        if isinstance(cause, discord.NotFound) and cause.code == 10062:
            logger.warning("Interaction expired before acknowledgment; command was not applied")
            return
        if isinstance(cause, DomainError):
            message = str(cause)
        else:
            logger.error("Application command failed", exc_info=(type(cause), cause, cause.__traceback__))
            message = "Операция не выполнена. Попробуйте позже или обратитесь к администратору."
        try:
            if interaction.response.is_done():
                command_name = getattr(getattr(interaction, "command", None), "qualified_name", "")
                if command_name in {"rating profile", "rating user", "rating top", "rating give", "rating punish", "rating undo"}:
                    # Complete the public defer with a generic notice first. Detailed
                    # errors are a subsequent private follow-up, never profile content.
                    await interaction.edit_original_response(content="Не удалось выполнить команду.", embed=None, view=None)
                await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.send_message(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as response_error:
            logger.warning("Could not deliver command error: HTTP %s Discord code %s",
                           response_error.status, response_error.code)

    async def require_staff(self, interaction: discord.Interaction, *, admin_only: bool = False) -> bool:
        if not has_staff_role(interaction.user, self.settings, admin_only=admin_only):
            await interaction.response.send_message("Недостаточно прав для этой команды.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="profile", description="Ваш профиль социального рейтинга")
    async def profile(self, interaction: discord.Interaction) -> None:
        await defer_response(interaction, ephemeral=False)
        data = await self.service.profile(interaction.user.id)
        await interaction.followup.send(embed=profile_embed(data, self.settings), ephemeral=False)

    @app_commands.command(name="user", description="Рейтинг выбранного участника")
    async def user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await defer_response(interaction, ephemeral=False)
        data = await self.service.profile(user.id)
        # Public profile shows rating, class and separate credits, never moderation details.
        await interaction.followup.send(embed=profile_embed(data, self.settings), ephemeral=False)

    @app_commands.command(name="top", description="Общий рейтинг или рейтинг социального класса")
    @app_commands.describe(social_class="Показать общий рейтинг или только выбранный класс")
    @app_commands.choices(social_class=[
        app_commands.Choice(name="Все классы", value="all"),
        app_commands.Choice(name="Неблагонадёжный", value="untrusted"),
        app_commands.Choice(name="Под наблюдением", value="watched"),
        app_commands.Choice(name="Обычный гражданин", value="citizen"),
        app_commands.Choice(name="Благонадёжный", value="trusted"),
        app_commands.Choice(name="Образцовый гражданин", value="exemplary"),
    ])
    async def top(self, interaction: discord.Interaction,
                  social_class: app_commands.Choice[str] | None = None) -> None:
        await defer_response(interaction, ephemeral=False)
        class_key = social_class.value if social_class and social_class.value != "all" else None
        label = social_class.name if class_key else "Все классы"

        async def load(page: int) -> tuple[discord.Embed, bool]:
            rows = await self.service.top(page=page, page_size=LEADERBOARD_PAGE_SIZE, class_key=class_key)
            following = await self.service.top(page=page + 1, page_size=LEADERBOARD_PAGE_SIZE, class_key=class_key)
            own = await self.service.my_rank(interaction.user.id, class_key=class_key)
            embed = discord.Embed(title=f"Рейтинг: {label}", color=discord.Color.gold())
            embed.description = "\n".join(f"{row['rank']}. <@{row['discord_id']}>: **{row['balance']}**" for row in rows) or "В этом рейтинге пока нет участников."
            if own:
                embed.add_field(name="Место автора команды", value=f"<@{interaction.user.id}>: {own['rank']} • {own['balance']} баллов", inline=False)
            embed.set_footer(text=f"Страница {page + 1} • При равном рейтинге место одинаковое; порядок по ID")
            return embed, bool(following)
        await self.send_pager(interaction, load, ephemeral=False)

    async def send_pager(self, interaction: discord.Interaction, load: Callable[[int], Awaitable[tuple[discord.Embed, bool]]], *, ephemeral: bool = True) -> None:
        embed, has_next = await load(0)
        view = Pager(interaction.user.id, load)
        view.next.disabled = not has_next
        await interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral)

    async def show_history(self, interaction: discord.Interaction, user_id: int, *, staff: bool = False) -> None:
        await defer_response(interaction, ephemeral=True)
        async def load(page: int) -> tuple[discord.Embed, bool]:
            # Recheck the invoking member's configured roles when pagination occurs.
            if staff:
                member = await interaction.guild.fetch_member(interaction.user.id)
                if not has_staff_role(member, self.settings):
                    raise DomainError("Недостаточно прав для просмотра журнала.")
            rows = await self.service.history(user_id, page=page, page_size=PAGE_SIZE)
            following = await self.service.history(user_id, page=page + 1, page_size=PAGE_SIZE)
            return history_embed(rows, page), bool(following)
        await self.send_pager(interaction, load)

    @app_commands.command(name="history", description="Ваша история изменений рейтинга и кредитов")
    async def history(self, interaction: discord.Interaction) -> None:
        await self.show_history(interaction, interaction.user.id)

    @app_commands.command(name="log", description="Журнал операций участника для модерации")
    async def log(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if await self.require_staff(interaction):
            await self.show_history(interaction, user.id, staff=True)

    async def validate_public_reason(self, interaction: discord.Interaction, reason: str) -> bool:
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
            await interaction.response.send_message("Причина должна содержать от 1 до 500 символов.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="punish", description="Снять 50 баллов за нарушение")
    async def punish(self, interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 1, 500]) -> None:
        if not await self.require_staff(interaction):
            return
        if not await self.validate_public_reason(interaction, reason):
            return
        await defer_response(interaction, ephemeral=False)
        result = await self.service.punish(user.id, interaction.user.id, reason, interaction.id, actor_roles(interaction.user),
                                           actor_is_admin=interaction.user.guild_permissions.administrator)
        await interaction.followup.send(embed=notification_embed("punishment", result), ephemeral=False)

    @app_commands.command(name="give", description="Выдать социальный рейтинг (администратор)")
    async def give(self, interaction: discord.Interaction, user: discord.Member,
                   amount: app_commands.Range[int, 1, 1_000_000],
                   reason: app_commands.Range[str, 1, 500]) -> None:
        if not await self.require_staff(interaction, admin_only=True):
            return
        if type(amount) is not int or not 1 <= amount <= MAX_MANUAL_GRANT:
            await interaction.response.send_message("Количество должно быть целым числом от 1 до 1 000 000.", ephemeral=True)
            return
        if not await self.validate_public_reason(interaction, reason):
            return
        await defer_response(interaction, ephemeral=False)
        result = await self.service.give(user.id, interaction.user.id, amount, reason, interaction.id,
                                         actor_roles(interaction.user),
                                         actor_is_admin=interaction.user.guild_permissions.administrator)
        await interaction.followup.send(embed=notification_embed("manual_grant", result), ephemeral=False)

    @app_commands.command(name="undo", description="Отменить взыскание (администратор)")
    async def undo(self, interaction: discord.Interaction, punishment_id: str, reason: app_commands.Range[str, 1, 500]) -> None:
        if not await self.require_staff(interaction, admin_only=True):
            return
        if not await self.validate_public_reason(interaction, reason):
            return
        await defer_response(interaction, ephemeral=False)
        result = await self.service.undo(punishment_id, interaction.user.id, reason, interaction.id, actor_roles(interaction.user),
                                           actor_is_admin=interaction.user.guild_permissions.administrator)
        await interaction.followup.send(embed=notification_embed("undo", result), ephemeral=False)

    @app_commands.command(name="status", description="Состояние начислений (администратор)")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self.require_staff(interaction, admin_only=True):
            return
        await defer_response(interaction, ephemeral=True)
        data = await self.service.status()
        embed = discord.Embed(title="Состояние системы рейтинга", color=discord.Color.blue())
        embed.add_field(name="Последнее начисление", value=display_time(data.get("last_successful_payout")), inline=False)
        embed.add_field(name="Следующее начисление", value=display_time(data.get("next_payout")), inline=False)
        embed.add_field(name="Активных участников сейчас", value=str(data.get("active_users", 0)))
        embed.add_field(name="Незавершённых периодов", value=str(data.get("pending_periods", 0)))
        embed.add_field(name="Текущий период", value=f"{display_time(data.get('current_period_start'))} → {display_time(data.get('current_period_end'))}", inline=False)
        embed.add_field(name="Состояние периода", value="Неполные данные" if data.get("current_period_incomplete") else "Учёт активности", inline=False)
        last_recovery = await self.service.get_state("last_reconciliation")
        embed.add_field(name="Последняя сверка истории", value=display_time(last_recovery), inline=False)
        incomplete = data.get("incomplete_periods", [])
        if incomplete:
            for issue in incomplete[:5]:
                embed.add_field(name=f"Проблемный период: {issue['period_start']}", value=safe_text(issue['reason'], 600), inline=False)
        else:
            embed.add_field(name="Полнота данных", value="Известных проблем нет", inline=False)
        embed.set_footer(text=f"Показано {min(len(incomplete), 5)} из {len(incomplete)} последних проблемных периодов. Удалённые за простой сообщения восстановить нельзя.")
        await interaction.followup.send(embed=embed, ephemeral=True)


def notification_embed(kind: str, data: dict[str, Any]) -> discord.Embed:
    if kind == "daily":
        embed = discord.Embed(title="Ежедневное начисление социального рейтинга", color=discord.Color.green())
        embed.add_field(name="Дата", value=display_time(data.get("period_end") or data.get("finished_at")), inline=False)
        embed.add_field(name="Активных участников", value=str(data["participants_count"]))
        reward = data.get("reward_per_user")
        if reward is None and data["participants_count"]:
            reward = data["total_amount"] // data["participants_count"]
        if reward is not None:
            embed.add_field(name="Начислено каждому", value=f"+{reward} рейтинга")
        embed.add_field(name="Всего начислено рейтинга", value=str(data["total_amount"]))
        # Older durable notifications contain rating only. Never fabricate credits
        # for a historical period which preceded the currency ledger.
        if "total_credits" in data:
            embed.add_field(name="Всего начислено кредитов", value=str(data["total_credits"]))
            class_counts = data.get("credit_class_counts")
            if class_counts:
                embed.add_field(
                    name="Получатели кредитов по классам",
                    value="\n".join(f"{safe_text(label, 100)}: {count}" for label, count in class_counts.items())[:1024],
                    inline=False,
                )
        embed.add_field(name="Минимум активности", value="5 сообщений")
        if data.get("incomplete"):
            embed.add_field(name="Внимание", value="Данные периода неполные. Учтена только подтверждённая активность.", inline=False)
        return embed
    titles = {"undo": "Отмена взыскания", "punishment": "Взыскание", "manual_grant": "Выдача социального рейтинга"}
    embed = discord.Embed(title=titles.get(kind, "Изменение рейтинга"), color=discord.Color.orange())
    embed.add_field(name="Участник", value=f"<@{data['discord_id']}>")
    embed.add_field(name="Сотрудник", value=f"<@{data['moderator_id']}>")
    embed.add_field(name="Изменение", value=f"{data['amount']:+d}")
    embed.add_field(name="Рейтинг после операции", value=str(data["balance_after"]))
    embed.add_field(name="Причина", value=safe_text(data.get("reason", "")), inline=False)
    embed.add_field(name="Дата", value=display_time(data["created_at"]), inline=False)
    embed.add_field(name="ID операции", value=str(data["id"]), inline=False)
    if data.get("reverses_id"):
        embed.add_field(name="Исходное взыскание", value=str(data["reverses_id"]), inline=False)
    return embed


class RatingBot(discord.Client):
    def __init__(self, settings: Settings, service: RatingService) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        connector = aiohttp.TCPConnector(keepalive_timeout=75) if settings.discord_proxy else None
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(),
                         proxy=settings.discord_proxy, connector=connector)
        self.settings = settings
        self.service = service
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(RatingCommands(service, settings), guild=discord.Object(id=settings.guild_id))
        self._worker: asyncio.Task[None] | None = None
        self._keepwarm: asyncio.Task[None] | None = None
        self._ingestion_lock = asyncio.Lock()
        self._recovery_epoch = 0
        self._recovered_epoch = -1
        self._last_period: datetime | None = None
        self._last_cleanup: datetime | None = None

    async def setup_hook(self) -> None:
        synced = await self.tree.sync(guild=discord.Object(id=self.settings.guild_id))
        logger.info("Guild command sync succeeded: %s", ", ".join(command.name for command in synced))
        self._worker = asyncio.create_task(self.background_worker(), name="rating-worker")
        if self.settings.discord_proxy:
            self._keepwarm = asyncio.create_task(self.keep_http_warm(), name="discord-http-keepwarm")

    async def on_ready(self) -> None:
        self._recovery_epoch += 1
        logger.info("Discord connected as user %s to guild %s", self.user.id if self.user else None, self.settings.guild_id)

    async def on_disconnect(self) -> None:
        self._recovery_epoch += 1
        logger.warning("Discord disconnected; reconciliation required before next payout")

    async def on_resumed(self) -> None:
        self._recovery_epoch += 1

    async def on_message(self, message: discord.Message) -> None:
        if not accepts_message(message, self.settings.guild_id):
            return
        try:
            async with self._ingestion_lock:
                await self.service.record_message(message.id, message.author.id, message.created_at)
        except Exception:
            logger.exception("Message accounting failed for message %s; scheduling history recovery", message.id)
            self._recovery_epoch += 1

    async def send_pending_notifications(self) -> None:
        for item in await self.service.pending_notifications():
            try:
                channel_id = self.settings.daily_report_channel_id if item["kind"] == "daily" else self.settings.moderation_log_channel_id
                if channel_id is None:
                    continue
                channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.settings.guild_id:
                    raise ValueError("Configured notification channel is not a guild text channel")
                if item["kind"] != "daily" and not private_log_channel(channel, self.settings, self.user.id):
                    raise ValueError("Moderation channel is visible to nonstaff; fix channel permissions")
                embed = notification_embed(item["kind"], item["payload"])
                # Stable marker makes an occasional send-before-ACK duplicate auditable.
                embed.set_footer(text=f"Уведомление {item['id']}")
                sent = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                await self.service.notification_sent(item["id"], sent.id)
            except Exception as error:
                logger.exception("Outbox notification %s failed", item["id"])
                await self.service.notification_failed(item["id"], type(error).__name__ + ": " + str(error)[:250])

    async def cycle(self) -> None:
        # Notifications concern already committed operations, so recovery failures
        # must never block retries of moderation logs or older daily summaries.
        try:
            await self.send_pending_notifications()
        except Exception:
            logger.exception("Outbox retry cycle failed; recovery will still run")
        guild = self.get_guild(self.settings.guild_id)
        if guild is None:
            logger.error("Configured guild not visible to bot: %s", self.settings.guild_id)
            return
        now = datetime.now(UTC)
        start, _ = period_bounds(now, self.settings.timezone, self.settings.payout_time)
        epoch = self._recovery_epoch
        if self._recovered_epoch != epoch or self._last_period != start:
            async with self._ingestion_lock:
                recovered = await reconcile_history(guild, self.service, self.settings, cutoff=now, after_disconnect=self._recovered_epoch != epoch)
                if not recovered:
                    return
                # A concurrent disconnect invalidates this scan; do not settle yet.
                if self._recovery_epoch != epoch or not self.is_ready():
                    return
                self._recovered_epoch = epoch
                self._last_period = start
                await self.service.settle_due(now)
        else:
            await self.service.settle_due(now)
        await self.send_pending_notifications()
        if self._last_cleanup is None or (now - self._last_cleanup).total_seconds() >= 3600:
            await self.service.cleanup(now)
            self._last_cleanup = now

    async def background_worker(self) -> None:
        while not self.is_closed():
            await self.wait_until_ready()
            try:
                await self.cycle()
            except Exception:
                logger.exception("Rating background cycle failed; retrying in 30 seconds")
            await asyncio.sleep(30)

    async def keep_http_warm(self) -> None:
        """Avoid a cold proxy tunnel consuming Discord's three-second ACK budget."""
        while not self.is_closed():
            await self.wait_until_ready()
            try:
                started = time.monotonic()
                await self.fetch_user(self.user.id)
                logger.debug("Discord proxy keepalive %.3fs", time.monotonic() - started)
            except Exception as error:
                logger.warning("Discord proxy keepalive failed: %s", type(error).__name__)
            await asyncio.sleep(20)

    async def close(self) -> None:
        if self._keepwarm is not None:
            self._keepwarm.cancel()
            try:
                await self._keepwarm
            except asyncio.CancelledError:
                logger.debug("Discord HTTP keepalive stopped")
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                logger.debug("Rating background worker stopped")
        await super().close()
