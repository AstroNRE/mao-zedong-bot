from sqlalchemy import BigInteger, Integer, String, Text, ForeignKey, UniqueConstraint, CheckConstraint, Index, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared SQLAlchemy metadata for the versioned application schema."""


class User(Base):
    __tablename__ = 'users'
    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_balance: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(String)


class Activity(Base):
    __tablename__ = 'daily_activity'
    discord_id: Mapped[int] = mapped_column(ForeignKey('users.discord_id'), primary_key=True)
    period_start: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint('message_count >= 0'),)


class Transaction(Base):
    __tablename__ = 'rating_transactions'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    discord_id: Mapped[int] = mapped_column(ForeignKey('users.discord_id'), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(Text)
    moderator_id: Mapped[int | None] = mapped_column(BigInteger)
    period_start: Mapped[str | None] = mapped_column(String)
    balance_before: Mapped[int] = mapped_column(BigInteger)
    balance_after: Mapped[int] = mapped_column(BigInteger)
    reverses_id: Mapped[str | None] = mapped_column(ForeignKey('rating_transactions.id'), unique=True)
    created_at: Mapped[str] = mapped_column(String, index=True)
    __table_args__ = (UniqueConstraint('discord_id','period_start','type'),
                     CheckConstraint('balance_after = balance_before + amount'),
                     CheckConstraint("(type='daily' AND amount IN (25,50) AND period_start IS NOT NULL AND reverses_id IS NULL) OR (type='punishment' AND amount IN (-50,-100) AND reason IS NOT NULL AND reverses_id IS NULL) OR (type='undo' AND amount IN (50,100) AND reason IS NOT NULL AND reverses_id IS NOT NULL) OR (type='manual_grant' AND amount>0 AND amount<=1000000 AND reason IS NOT NULL AND moderator_id IS NOT NULL AND period_start IS NULL AND reverses_id IS NULL) OR (type='opening' AND amount=800 AND moderator_id IS NULL AND period_start IS NULL AND reverses_id IS NULL)"),
                     Index('uq_rating_opening_per_user', 'discord_id', unique=True,
                           sqlite_where=text("type = 'opening'"),
                           postgresql_where=text("type = 'opening'")))


class Payout(Base):
    __tablename__ = 'daily_payouts'
    period_start: Mapped[str] = mapped_column(String, primary_key=True)
    period_end: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String)
    finished_at: Mapped[str | None] = mapped_column(String)
    participants_count: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[int] = mapped_column(Integer, default=0)


class ProcessedMessage(Base):
    __tablename__ = 'processed_messages'
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    discord_id: Mapped[int] = mapped_column(ForeignKey('users.discord_id'))
    period_start: Mapped[str] = mapped_column(String, index=True)


class Interaction(Base):
    __tablename__ = 'processed_interactions'
    interaction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey('rating_transactions.id'))
    fingerprint: Mapped[str] = mapped_column(String)


class Outbox(Base):
    __tablename__ = 'outbox'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[str] = mapped_column(String, index=True)
    sent_at: Mapped[str | None] = mapped_column(String)
    discord_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(Text)


class State(Base):
    __tablename__ = 'service_state'
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Incomplete(Base):
    __tablename__ = 'incomplete_periods'
    period_start: Mapped[str] = mapped_column(String, primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    detected_at: Mapped[str] = mapped_column(String)


class CreditTransaction(Base):
    __tablename__ = 'credit_transactions'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    discord_id: Mapped[int] = mapped_column(ForeignKey('users.discord_id'), index=True)
    period_start: Mapped[str] = mapped_column(String)
    amount: Mapped[int] = mapped_column(Integer)
    balance_before: Mapped[int] = mapped_column(BigInteger)
    balance_after: Mapped[int] = mapped_column(BigInteger)
    basis_rating: Mapped[int] = mapped_column(BigInteger)
    basis_class: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, index=True)
    __table_args__ = (
        UniqueConstraint('discord_id', 'period_start'),
        CheckConstraint('balance_before >= 0 AND balance_after = balance_before + amount'),
        CheckConstraint("(basis_rating<=499 AND basis_class='Неблагонадёжный' AND amount=0) OR (basis_rating BETWEEN 500 AND 799 AND basis_class='Под наблюдением' AND amount=10) OR (basis_rating BETWEEN 800 AND 1099 AND basis_class='Обычный гражданин' AND amount=20) OR (basis_rating BETWEEN 1100 AND 1499 AND basis_class='Благонадёжный' AND amount=30) OR (basis_rating>=1500 AND basis_class='Образцовый гражданин' AND amount=50)"),
    )
