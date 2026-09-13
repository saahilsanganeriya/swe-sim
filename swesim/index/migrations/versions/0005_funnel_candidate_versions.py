"""Creation funnel reconciliation: a candidate row carries the archived (superseded) versions of its scenario so the
funnel can count every version once and show a replaced version as superseded rather than losing it."""
from alembic import op
import sqlalchemy as sa

revision = '0005_funnel_versions'
down_revision = '0004_history'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('candidates', sa.Column('earlier_versions', sa.JSON(), nullable=True))


def downgrade() -> None:
    raise RuntimeError('Forward-only migration: restore or rebuild the disposable index')
