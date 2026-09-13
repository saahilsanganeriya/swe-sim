"""Preserve semantic evaluation configuration and the requested score estimator k."""
from alembic import op
import sqlalchemy as sa

revision = '0003_measurement_cohorts'
down_revision = '0002_discoveries'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('trials', sa.Column('evaluation_config', sa.JSON(), nullable=True))
    op.add_column('scores', sa.Column('requested_k', sa.Integer(), nullable=True))


def downgrade() -> None:
    raise RuntimeError('Forward-only migration: restore or rebuild the disposable index')
