"""Atomic rating ledger, idempotent activity, payouts and durable outbox."""
import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4
from typing import Any
from .config import Settings
from .database import Database
from sqlalchemy import select, func, delete, union_all, literal
from .database.models import User, Activity, Transaction, Payout, ProcessedMessage, Interaction, Outbox, State, Incomplete, CreditTransaction
from .periods import utcnow, period_bounds, iso


MAX_MANUAL_GRANT = 1_000_000
SQLITE_MIN_INT = -(2 ** 63)
SQLITE_MAX_INT = 2 ** 63 - 1


class DomainError(ValueError):
    """Safe to show to the command caller."""


def row_dict(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class RatingService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db, self.settings = db, settings

    async def _user(self, session, user_id):
        user = await session.get(User, user_id)
        if user is None:
            user = User(discord_id=user_id, balance=0, created_at=iso(utcnow()))
            session.add(user)
            await session.flush()
            await self._transaction(session, user, self.settings.start_rating, 'opening',
                                    reason='Стартовый рейтинг')
        return user

    async def _incomplete(self, session, start, reason):
        row = await session.get(Incomplete, start)
        if row is None:
            session.add(Incomplete(period_start=start, reason=reason[:4000], detected_at=iso(utcnow())))
        elif reason not in row.reason:
            row.reason = (row.reason + '; ' + reason)[:4000]

    async def get_state(self, key: str) -> str | None:
        async with self.db.sessions() as session:
            row = await session.get(State, key)
            return row.value if row else None

    async def set_state(self, key: str, value: str) -> None:
        async with self.db.write() as session:
            row = await session.get(State, key)
            if row:
                row.value = value
            else:
                session.add(State(key=key, value=value))

    async def mark_incomplete(self, start: datetime, end: datetime, reason: str) -> None:
        cursor, _ = period_bounds(start)
        async with self.db.write() as session:
            while cursor < end:
                await self._incomplete(session, iso(cursor), reason)
                _, cursor = period_bounds(cursor)

    async def record_message(self, message_id: int, user_id: int, created_at: datetime) -> bool:
        start, end = period_bounds(created_at)
        key = iso(start)
        async with self.db.write() as session:
            opt_in = await session.get(State, 'opt_in_at')
            if opt_in is not None and created_at < datetime.fromisoformat(opt_in.value):
                return False
            if await session.get(ProcessedMessage, message_id):
                return False
            payout = await session.get(Payout, key)
            if payout and payout.status == 'completed':
                await self._incomplete(session, key, 'Сообщение доставлено после завершения начисления')
                return False
            # Retention never reopens old periods or accepts stale replay events.
            if created_at < utcnow() - timedelta(days=self.settings.message_retention_days):
                await self._incomplete(session, key, 'Событие вне окна хранения дедупликации')
                return False
            await self._user(session, user_id)
            session.add(ProcessedMessage(message_id=message_id, discord_id=user_id, period_start=key))
            activity = await session.get(Activity, (user_id, key))
            if activity is None:
                session.add(Activity(discord_id=user_id, period_start=key, message_count=1))
            else:
                activity.message_count += 1
            tracking = await session.get(State, 'tracking_started')
            if tracking is None:
                session.add(State(key='tracking_started', value=iso(created_at)))
                if created_at > start:
                    await self._incomplete(session, key, 'Первый запуск: до начала наблюдения активность неизвестна')
            return True

    async def _transaction(self, session, user, amount, kind, reason=None, moderator_id=None,
                           period_start=None, reverses_id=None):
        if not SQLITE_MIN_INT <= user.balance + amount <= SQLITE_MAX_INT:
            raise DomainError('Операция выходит за допустимый диапазон хранения рейтинга.')
        transaction = Transaction(id=uuid4().hex, discord_id=user.discord_id, amount=amount,
                                  type=kind, reason=reason, moderator_id=moderator_id,
                                  period_start=period_start, balance_before=user.balance,
                                  balance_after=user.balance+amount, reverses_id=reverses_id,
                                  created_at=iso(utcnow()))
        user.balance += amount
        session.add(transaction)
        await session.flush()
        return transaction

    def _reason(self, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
            raise DomainError('Причина должна содержать от 1 до 500 символов.')
        return reason.strip()

    def _authorize(self, roles, admin=False, *, actor_is_admin: bool = False):
        allowed = self.settings.admin_role_ids if admin else self.settings.admin_role_ids | self.settings.moderator_role_ids
        fallback_admin = not self.settings.admin_role_ids and actor_is_admin is True
        if not (set(roles) & allowed or fallback_admin):
            raise DomainError('Недостаточно прав для этой команды.')

    async def _interaction(self, session, interaction_id, fingerprint):
        row = await session.get(Interaction, interaction_id)
        if row:
            if row.fingerprint != fingerprint:
                raise DomainError('Этот запрос уже обработан с другими параметрами.')
            return row_dict(await session.get(Transaction, row.transaction_id))
        return None

    def _fingerprint(self, *args):
        return hashlib.sha256(json.dumps(args, ensure_ascii=False).encode()).hexdigest()

    def _outbox(self, session, kind, payload, notification_id=None):
        session.add(Outbox(id=notification_id or uuid4().hex, kind=kind,
                          payload=json.dumps(payload, ensure_ascii=False), attempts=0,
                          next_attempt=iso(utcnow())))

    async def punish(self, user_id: int, moderator_id: int, reason: str, interaction_id: int, actor_role_ids: set[int], *, actor_is_admin: bool = False) -> dict[str, Any]:
        self._authorize(actor_role_ids, actor_is_admin=actor_is_admin)
        reason = self._reason(reason)
        fingerprint = self._fingerprint('punishment', user_id, moderator_id, reason)
        async with self.db.write() as session:
            previous = await self._interaction(session, interaction_id, fingerprint)
            if previous:
                return previous
            user = await self._user(session, user_id)
            transaction = await self._transaction(session, user, -self.settings.punishment_amount, 'punishment', reason, moderator_id)
            session.add(Interaction(interaction_id=interaction_id, transaction_id=transaction.id, fingerprint=fingerprint))
            data = row_dict(transaction)
            if self.settings.moderation_log_channel_id is not None:
                self._outbox(session, 'punishment', data)
            return data

    async def give(self, user_id: int, moderator_id: int, amount: int, reason: str,
                   interaction_id: int, actor_role_ids: set[int], *,
                   actor_is_admin: bool = False) -> dict[str, Any]:
        self._authorize(actor_role_ids, admin=True, actor_is_admin=actor_is_admin)
        if type(amount) is not int or not 1 <= amount <= MAX_MANUAL_GRANT:
            raise DomainError('Количество должно быть целым числом от 1 до 1 000 000.')
        reason = self._reason(reason)
        fingerprint = self._fingerprint('manual_grant', user_id, moderator_id, amount, reason)
        async with self.db.write() as session:
            previous = await self._interaction(session, interaction_id, fingerprint)
            if previous:
                return previous
            user = await self._user(session, user_id)
            transaction = await self._transaction(session, user, amount, 'manual_grant', reason, moderator_id)
            session.add(Interaction(interaction_id=interaction_id, transaction_id=transaction.id,
                                    fingerprint=fingerprint))
            data = row_dict(transaction)
            if self.settings.moderation_log_channel_id is not None:
                self._outbox(session, 'manual_grant', data)
            return data

    async def undo(self, punishment_id: str, moderator_id: int, reason: str, interaction_id: int, actor_role_ids: set[int], *, actor_is_admin: bool = False) -> dict[str, Any]:
        self._authorize(actor_role_ids, admin=True, actor_is_admin=actor_is_admin)
        reason = self._reason(reason)
        fingerprint = self._fingerprint('undo', punishment_id, moderator_id, reason)
        async with self.db.write() as session:
            previous = await self._interaction(session, interaction_id, fingerprint)
            if previous:
                return previous
            original = await session.get(Transaction, punishment_id)
            if original is None or original.type != 'punishment':
                raise DomainError('Взыскание с таким ID не найдено.')
            reversal = await session.scalar(select(Transaction.id).where(Transaction.reverses_id == punishment_id))
            if reversal:
                raise DomainError('Это взыскание уже отменено.')
            user = await self._user(session, original.discord_id)
            transaction = await self._transaction(session, user, -original.amount, 'undo', reason, moderator_id,
                                                  reverses_id=punishment_id)
            session.add(Interaction(interaction_id=interaction_id, transaction_id=transaction.id, fingerprint=fingerprint))
            data = row_dict(transaction)
            if self.settings.moderation_log_channel_id is not None:
                self._outbox(session, 'undo', data)
            return data

    async def profile(self, user_id: int, now: datetime | None = None) -> dict[str, Any]:
        start, _ = period_bounds(now or utcnow())
        async with self.db.write() as session:
            user = await self._user(session, user_id)
            counts = dict((await session.execute(select(Transaction.type, func.count()).where(
                Transaction.discord_id == user_id).group_by(Transaction.type))).all())
            activity = await session.get(Activity, (user_id, iso(start)))
            return {'discord_id': user_id, 'balance': user.balance, 'credit_balance': user.credit_balance,
                    'awarded_days': counts.get('daily', 0),
                    'active_punishments': counts.get('punishment', 0)-counts.get('undo', 0),
                    'message_count': activity.message_count if activity else 0}

    def _ranked_users(self, class_key: str | None = None):
        bounds = {
            'untrusted': (None, 499), 'watched': (500, 799), 'citizen': (800, 1099),
            'trusted': (1100, 1499), 'exemplary': (1500, None),
        }
        query = select(User.discord_id, User.balance,
                       func.rank().over(order_by=User.balance.desc()).label('rank'))
        if class_key is not None:
            if class_key not in bounds:
                raise DomainError('Неизвестный социальный класс.')
            lower, upper = bounds[class_key]
            if lower is not None:
                query = query.where(User.balance >= lower)
            if upper is not None:
                query = query.where(User.balance <= upper)
        return query.subquery()

    async def top(self, page: int = 0, page_size: int = 10,
                  class_key: str | None = None) -> list[dict[str, Any]]:
        if page < 0 or not 1 <= page_size <= 25:
            raise DomainError('Некорректная страница.')
        ranked = self._ranked_users(class_key)
        async with self.db.sessions() as session:
            rows = await session.execute(select(ranked).order_by(ranked.c.balance.desc(), ranked.c.discord_id)
                                         .offset(page * page_size).limit(page_size))
            return [dict(row) for row in rows.mappings()]

    async def my_rank(self, user_id: int, class_key: str | None = None) -> dict[str, Any] | None:
        ranked = self._ranked_users(class_key)
        async with self.db.sessions() as session:
            row = (await session.execute(select(ranked).where(ranked.c.discord_id == user_id))).mappings().first()
            return dict(row) if row else None

    async def history(self, user_id: int, page: int = 0, page_size: int = 10) -> list[dict[str, Any]]:
        if page < 0 or not 1 <= page_size <= 25:
            raise DomainError('Некорректная страница.')
        async with self.db.sessions() as session:
            rating = select(Transaction.id, Transaction.discord_id, Transaction.amount, Transaction.type,
                            Transaction.reason, Transaction.moderator_id, Transaction.period_start,
                            Transaction.balance_before, Transaction.balance_after, Transaction.reverses_id,
                            Transaction.created_at).where(Transaction.discord_id == user_id)
            credits = select(CreditTransaction.id, CreditTransaction.discord_id, CreditTransaction.amount,
                             literal('credit_daily').label('type'),
                             (literal('Класс до начисления: ') + CreditTransaction.basis_class).label('reason'),
                             literal(None).label('moderator_id'), CreditTransaction.period_start,
                             CreditTransaction.balance_before, CreditTransaction.balance_after,
                             literal(None).label('reverses_id'), CreditTransaction.created_at
                             ).where(CreditTransaction.discord_id == user_id)
            ledger = union_all(rating, credits).subquery()
            rows = await session.execute(select(ledger).order_by(ledger.c.created_at.desc(), ledger.c.id.desc())
                                         .offset(page*page_size).limit(page_size))
            return [dict(row) for row in rows.mappings()]

    async def _credit_reward(self, session, user, period_start):
        # Processing-time basis, captured BEFORE this period's rating increase.
        basis = user.balance
        if basis <= 499:
            label, amount = 'Неблагонадёжный', 0
        elif basis <= 799:
            label, amount = 'Под наблюдением', 10
        elif basis <= 1099:
            label, amount = 'Обычный гражданин', 20
        elif basis <= 1499:
            label, amount = 'Благонадёжный', 30
        else:
            label, amount = 'Образцовый гражданин', 50
        after = user.credit_balance + amount
        if after > SQLITE_MAX_INT:
            raise DomainError('Превышен предел баланса кредитов.')
        session.add(CreditTransaction(id=uuid4().hex, discord_id=user.discord_id,
                    period_start=period_start, amount=amount, balance_before=user.credit_balance,
                    balance_after=after, basis_rating=basis, basis_class=label, created_at=iso(utcnow())))
        user.credit_balance = after

    async def settle_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utcnow()
        tracking = await self.get_state('tracking_started')
        if tracking is None:
            return []
        cursor, _ = period_bounds(datetime.fromisoformat(tracking))
        # Start with first pending period. Each period commits atomically, so a crash can
        # only leave whole completed periods and an uncommitted next one.
        async with self.db.sessions() as session:
            latest = await session.scalar(select(func.max(Payout.period_start)).where(Payout.status == 'completed'))
        if latest:
            _, cursor = period_bounds(datetime.fromisoformat(latest))
        completed = []
        while True:
            start, end = period_bounds(cursor)
            if end > now:
                break
            key = iso(start)
            async with self.db.write() as session:
                existing = await session.get(Payout, key)
                if existing and existing.status == 'completed':
                    cursor = end
                    continue
                payout = existing or Payout(period_start=key, period_end=iso(end), status='processing',
                                             started_at=iso(utcnow()), participants_count=0, total_amount=0)
                if existing is None:
                    session.add(payout)
                await session.flush()
                eligible = (await session.scalars(select(Activity).where(Activity.period_start == key,
                                                    Activity.message_count >= 5))).all()
                for activity in eligible:
                    prior = await session.scalar(select(Transaction.id).where(
                        Transaction.discord_id == activity.discord_id, Transaction.period_start == key,
                        Transaction.type == 'daily'))
                    if prior is None:
                        user = await self._user(session, activity.discord_id)
                        await self._credit_reward(session, user, key)
                        await self._transaction(session, user, self.settings.daily_reward, 'daily', period_start=key)
                count = await session.scalar(select(func.count()).select_from(Transaction).where(
                    Transaction.period_start == key, Transaction.type == 'daily'))
                payout.status, payout.finished_at = 'completed', iso(utcnow())
                payout.participants_count = count
                payout.total_amount = await session.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.period_start == key, Transaction.type == 'daily'))
                await session.flush()
                credit_rows = (await session.execute(select(CreditTransaction.basis_class, func.count(), func.sum(CreditTransaction.amount))
                    .where(CreditTransaction.period_start == key).group_by(CreditTransaction.basis_class))).all()
                incomplete = await session.get(Incomplete, key)
                data = row_dict(payout) | {'reward_per_user': self.settings.daily_reward,
                                           'total_credits': sum(row[2] for row in credit_rows),
                                           'credit_class_counts': {row[0]: row[1] for row in credit_rows},
                                           'incomplete': incomplete is not None,
                                           'incomplete_reason': incomplete.reason if incomplete else None}
                if await session.get(Outbox, 'daily:' + key) is None:
                    self._outbox(session, 'daily', data, 'daily:' + key)
                completed.append(data)
            cursor = end
        return completed

    async def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        start, end = period_bounds(now)
        async with self.db.sessions() as session:
            last = await session.scalar(select(func.max(Payout.finished_at)).where(Payout.status == 'completed'))
            active = await session.scalar(select(func.count()).select_from(Activity).where(
                Activity.period_start == iso(start), Activity.message_count >= 5))
            incompletes = (await session.scalars(select(Incomplete).order_by(Incomplete.period_start.desc()).limit(20))).all()
            processing = await session.scalar(select(func.count()).select_from(Payout).where(Payout.status != 'completed'))
            tracking = await session.get(State, 'tracking_started')
            latest_end = await session.scalar(select(func.max(Payout.period_end)).where(Payout.status == 'completed'))
            pending = processing
            if tracking:
                cursor = datetime.fromisoformat(latest_end) if latest_end else period_bounds(datetime.fromisoformat(tracking.value))[0]
                while period_bounds(cursor)[1] <= now:
                    pending += 1
                    cursor = period_bounds(cursor)[1]
            return {'last_successful_payout': last, 'next_payout': iso(end),
                    'current_period_start': iso(start), 'current_period_end': iso(end),
                    'active_users': active, 'incomplete_periods': [row_dict(r) for r in incompletes],
                    'pending_periods': pending, 'current_period_incomplete': await session.get(Incomplete, iso(start)) is not None}

    async def pending_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self.db.sessions() as session:
            query = select(Outbox).where(Outbox.sent_at.is_(None), Outbox.next_attempt <= iso(utcnow()))
            if self.settings.moderation_log_channel_id is None:
                query = query.where(Outbox.kind == 'daily')
            rows = (await session.scalars(query.order_by(Outbox.next_attempt, Outbox.id).limit(limit))).all()
            return [row_dict(r) | {'payload': json.loads(r.payload)} for r in rows]

    async def notification_sent(self, notification_id: str, discord_message_id: int) -> None:
        async with self.db.write() as session:
            row = await session.get(Outbox, notification_id)
            if row:
                row.sent_at, row.discord_message_id, row.last_error = iso(utcnow()), discord_message_id, None

    async def notification_failed(self, notification_id: str, error: str) -> None:
        async with self.db.write() as session:
            row = await session.get(Outbox, notification_id)
            if row and row.sent_at is None:
                row.attempts += 1
                row.last_error = str(error)[:1000]
                row.next_attempt = iso(utcnow() + timedelta(seconds=min(3600, 5*2**min(row.attempts,10))))

    async def cleanup(self, now: datetime | None = None) -> None:
        cutoff = iso((now or utcnow()) - timedelta(days=self.settings.message_retention_days))
        async with self.db.write() as session:
            # Only clean messages from completed periods; pending recovery stays deduplicated.
            completed = select(Payout.period_start).where(Payout.status == 'completed', Payout.period_end < cutoff)
            await session.execute(delete(ProcessedMessage).where(ProcessedMessage.period_start.in_(completed)))
