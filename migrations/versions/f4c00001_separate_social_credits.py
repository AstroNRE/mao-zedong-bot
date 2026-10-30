"""Independent social-credit currency; no retroactive rewards for closed periods."""
from alembic import op
import sqlalchemy as sa

revision = 'f4c00001'
down_revision = 'f3a25050'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('credit_balance', sa.BigInteger(), nullable=False, server_default=sa.text('0')))
    op.create_table('credit_transactions',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('discord_id', sa.BigInteger(), sa.ForeignKey('users.discord_id'), nullable=False),
        sa.Column('period_start', sa.String(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('balance_before', sa.BigInteger(), nullable=False),
        sa.Column('balance_after', sa.BigInteger(), nullable=False),
        sa.Column('basis_rating', sa.BigInteger(), nullable=False),
        sa.Column('basis_class', sa.String(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.UniqueConstraint('discord_id', 'period_start'),
        sa.CheckConstraint('balance_before >= 0 AND balance_after = balance_before + amount'),
        sa.CheckConstraint("(basis_rating<=499 AND basis_class='Неблагонадёжный' AND amount=0) OR (basis_rating BETWEEN 500 AND 799 AND basis_class='Под наблюдением' AND amount=10) OR (basis_rating BETWEEN 800 AND 1099 AND basis_class='Обычный гражданин' AND amount=20) OR (basis_rating BETWEEN 1100 AND 1499 AND basis_class='Благонадёжный' AND amount=30) OR (basis_rating>=1500 AND basis_class='Образцовый гражданин' AND amount=50)"))
    op.create_index('ix_credit_transactions_discord_id', 'credit_transactions', ['discord_id'])
    op.create_index('ix_credit_transactions_created_at', 'credit_transactions', ['created_at'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM credit_transactions')).scalar_one():
        raise RuntimeError('Cannot discard audited currency events')
    op.drop_table('credit_transactions')
    op.drop_column('users', 'credit_balance')
