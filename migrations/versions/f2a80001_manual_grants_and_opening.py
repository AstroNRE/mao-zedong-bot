"""Allow audited manual grants and one opening credit for newly created users.

Existing users and balances are intentionally unchanged. Frozen table declarations
make the SQLite CHECK replacement independent of future ORM model changes.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f2a80001'
down_revision = 'c1b88909d385'
branch_labels = None
depends_on = None

OLD_KINDS = "(type='daily' AND amount=50 AND period_start IS NOT NULL AND reverses_id IS NULL) OR (type='punishment' AND amount=-100 AND reason IS NOT NULL AND reverses_id IS NULL) OR (type='undo' AND amount=100 AND reason IS NOT NULL AND reverses_id IS NOT NULL)"
NEW_KINDS = OLD_KINDS + " OR (type='manual_grant' AND amount>0 AND amount<=1000000 AND reason IS NOT NULL AND moderator_id IS NOT NULL AND period_start IS NULL AND reverses_id IS NULL) OR (type='opening' AND amount=800 AND moderator_id IS NULL AND period_start IS NULL AND reverses_id IS NULL)"


def frozen_table(kinds):
    metadata = sa.MetaData()
    sa.Table('users', metadata, sa.Column('discord_id', sa.BigInteger, primary_key=True))
    table = sa.Table(
        'rating_transactions', metadata,
        sa.Column('id', sa.String, primary_key=True),
        sa.Column('discord_id', sa.BigInteger, sa.ForeignKey('users.discord_id'), nullable=False),
        sa.Column('amount', sa.Integer, nullable=False),
        sa.Column('type', sa.String, nullable=False),
        sa.Column('reason', sa.Text, nullable=True),
        sa.Column('moderator_id', sa.BigInteger, nullable=True),
        sa.Column('period_start', sa.String, nullable=True),
        sa.Column('balance_before', sa.BigInteger, nullable=False),
        sa.Column('balance_after', sa.BigInteger, nullable=False),
        sa.Column('reverses_id', sa.String, sa.ForeignKey('rating_transactions.id'), nullable=True),
        sa.Column('created_at', sa.String, nullable=False),
        sa.UniqueConstraint('discord_id', 'period_start', 'type'),
        sa.UniqueConstraint('reverses_id'),
        sa.CheckConstraint('balance_after = balance_before + amount', name='ck_rating_balances'),
        sa.CheckConstraint(kinds, name='ck_rating_operation'),
    )
    sa.Index('ix_rating_transactions_created_at', table.c.created_at)
    sa.Index('ix_rating_transactions_discord_id', table.c.discord_id)
    return table


def upgrade():
    # SQLite rebuilds a table to replace its CHECK; migration runs with FK checks
    # disabled on this dedicated connection, followed by an explicit integrity check.
    with op.batch_alter_table('rating_transactions', recreate='always', copy_from=frozen_table(OLD_KINDS)) as batch:
        batch.drop_constraint('ck_rating_operation', type_='check')
        batch.create_check_constraint('ck_rating_operation', NEW_KINDS)
    op.create_index('uq_rating_opening_per_user', 'rating_transactions', ['discord_id'], unique=True,
                    sqlite_where=sa.text("type = 'opening'"), postgresql_where=sa.text("type = 'opening'"))
    if op.get_bind().execute(sa.text('PRAGMA foreign_key_check')).fetchall():
        raise RuntimeError('Foreign-key integrity check failed after rating schema migration')


def downgrade():
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM rating_transactions WHERE type IN ('manual_grant','opening')")).scalar_one()
    if count:
        raise RuntimeError('Cannot downgrade while opening/manual grant records exist; preserve audit history')
    op.drop_index('uq_rating_opening_per_user', table_name='rating_transactions')
    with op.batch_alter_table('rating_transactions', recreate='always', copy_from=frozen_table(NEW_KINDS)) as batch:
        batch.drop_constraint('ck_rating_operation', type_='check')
        batch.create_check_constraint('ck_rating_operation', OLD_KINDS)
