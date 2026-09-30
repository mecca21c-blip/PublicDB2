"""add operational claims and collection recovery invariant

Revision ID: e8a106a06a01
Revises: c7f205b05b01
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

import app.models.common


revision = 'e8a106a06a01'
down_revision = 'c7f205b05b01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('crawl_runs') as batch_op:
        batch_op.add_column(sa.Column('heartbeat_at', app.models.common.UTCDateTime(timezone=True), nullable=True))
    now = datetime.now(timezone.utc)
    op.execute(
        sa.text(
            "UPDATE crawl_runs SET status='FAILED', "
            "connection_status=CASE WHEN connection_status='PENDING' THEN 'SKIPPED' ELSE connection_status END, "
            "raw_status=CASE WHEN raw_status='PENDING' THEN 'SKIPPED' ELSE raw_status END, "
            "extraction_status=CASE WHEN extraction_status='PENDING' THEN 'SKIPPED' ELSE extraction_status END, "
            "error_summary=:reason, finished_at=:now, heartbeat_at=:now WHERE status='RUNNING'"
        ).bindparams(reason='Collection interrupted before 06A migration; recovered as failed.', now=now)
    )
    op.create_index(
        'uq_crawl_runs_active_source', 'crawl_runs', ['source_id'], unique=True,
        sqlite_where=sa.text("status = 'RUNNING'"),
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_table(
        'operation_claims',
        sa.Column('resource_key', sa.String(length=500), nullable=False),
        sa.Column('operation', sa.String(length=80), nullable=False),
        sa.Column('owner_token', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('result_payload', sa.JSON(), nullable=True),
        sa.Column('error_summary', sa.Text(), nullable=True),
        sa.Column('claimed_at', app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column('heartbeat_at', app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column('completed_at', app.models.common.UTCDateTime(timezone=True), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('resource_key', name='uq_operation_claim_resource'),
    )
    op.create_index('ix_operation_claims_resource_key', 'operation_claims', ['resource_key'])
    op.create_index('ix_operation_claims_status', 'operation_claims', ['status'])


def downgrade() -> None:
    op.drop_index('ix_operation_claims_status', table_name='operation_claims')
    op.drop_index('ix_operation_claims_resource_key', table_name='operation_claims')
    op.drop_table('operation_claims')
    op.drop_index('uq_crawl_runs_active_source', table_name='crawl_runs')
    with op.batch_alter_table('crawl_runs') as batch_op:
        batch_op.drop_column('heartbeat_at')
