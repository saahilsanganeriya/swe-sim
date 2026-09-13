"""Trial rows carry the reason nothing was graded (`TrialRow.failure`): runner-error.json / launch.json `error`, projected
for runner-error, launch-failed and unresolved attempts so the console can say why instead of "unresolved"."""
from alembic import op
import sqlalchemy as sa

revision = '0006_trial_failure'
down_revision = '0005_funnel_versions'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('trials', sa.Column('failure', sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError('Forward-only migration: restore or rebuild the disposable index')
