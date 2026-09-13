"""Durable lifecycle history: candidate stage/decision receipts, the candidate's frozen source path, the workflow row's
lifecycle source (workflow hook | Temporal reconciler | Harbor job directory)."""
from alembic import op
import sqlalchemy as sa

revision = '0004_history'
down_revision = '0003_measurement_cohorts'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('candidate_events',
    sa.Column('event_id', sa.Text(), nullable=False),
    sa.Column('source_key', sa.Text(), nullable=False),
    sa.Column('instance_id', sa.Text(), nullable=False),
    sa.Column('candidates_path', sa.Text(), nullable=True),
    sa.Column('scenario_id', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=True),
    sa.Column('spec_hash', sa.Text(), nullable=True),
    sa.Column('phase', sa.Text(), nullable=False),
    sa.Column('result_status', sa.Text(), nullable=True),
    sa.Column('stage', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('gate', sa.Text(), nullable=True),
    sa.Column('reasons', sa.JSON(), nullable=False),
    sa.Column('terminal', sa.Boolean(), nullable=False),
    sa.Column('occurred_at', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('workflow_id', sa.Text(), nullable=False),
    sa.Column('run_id', sa.Text(), nullable=False),
    sa.Column('worker_image_digest', sa.Text(), nullable=True),
    sa.Column('origin', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('event_id')
    )
    op.create_index('ix_candidate_events_identity_time', 'candidate_events', ['source_key', 'instance_id', 'occurred_at'], unique=False)
    op.create_index('ix_candidate_events_workflow', 'candidate_events', ['workflow_id', 'run_id'], unique=False)
    op.add_column('candidates', sa.Column('candidates_path', sa.Text(), nullable=True))
    op.add_column('workflows', sa.Column('lifecycle_source', sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError('Forward-only migration: restore or rebuild the disposable index')
