"""Use +25 daily rating and -50 punishments while preserving historical amounts."""
from alembic import op
import sqlalchemy as sa
from migrations.versions.f2a80001_manual_grants_and_opening import NEW_KINDS as OLD_KINDS, frozen_table

revision = 'f3a25050'
down_revision = 'f2a80001'
branch_labels = None
depends_on = None

NEW_KINDS = OLD_KINDS.replace("type='daily' AND amount=50", "type='daily' AND amount IN (25,50)").replace(
    "type='punishment' AND amount=-100", "type='punishment' AND amount IN (-50,-100)").replace(
    "type='undo' AND amount=100", "type='undo' AND amount IN (50,100)")


def table_with_opening_index(kinds):
    table = frozen_table(kinds)
    sa.Index('uq_rating_opening_per_user', table.c.discord_id, unique=True,
             sqlite_where=sa.text("type = 'opening'"), postgresql_where=sa.text("type = 'opening'"))
    return table


def upgrade():
    with op.batch_alter_table('rating_transactions', recreate='always', copy_from=table_with_opening_index(OLD_KINDS)) as batch:
        batch.drop_constraint('ck_rating_operation', type_='check')
        batch.create_check_constraint('ck_rating_operation', NEW_KINDS)
    if op.get_bind().execute(sa.text('PRAGMA foreign_key_check')).fetchall():
        raise RuntimeError('Foreign-key integrity check failed after rating rule migration')


def downgrade():
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM rating_transactions WHERE (type='daily' AND amount=25) OR (type='punishment' AND amount=-50) OR (type='undo' AND amount=50)")).scalar_one()
    if count:
        raise RuntimeError('Cannot downgrade new rating operations without losing audit compatibility')
    with op.batch_alter_table('rating_transactions', recreate='always', copy_from=table_with_opening_index(NEW_KINDS)) as batch:
        batch.drop_constraint('ck_rating_operation', type_='check')
        batch.create_check_constraint('ck_rating_operation', OLD_KINDS)
