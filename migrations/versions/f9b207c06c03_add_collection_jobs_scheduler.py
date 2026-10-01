"""add persistent collection jobs, scopes, and scheduler settings

Revision ID: f9b207c06c03
Revises: e8a106a06a01
"""

from alembic import op
import sqlalchemy as sa

import app.models.common


revision = 'f9b207c06c03'
down_revision = 'e8a106a06a01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agencies', sa.Column('region_code', sa.String(length=40), nullable=True))
    op.create_index('ix_agencies_region_code', 'agencies', ['region_code'])
    op.add_column('sources', sa.Column('scheduled_refresh_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_index('ix_sources_scheduled_refresh_enabled', 'sources', ['scheduled_refresh_enabled'])
    op.add_column('operational_settings', sa.Column('automatic_refresh_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('operational_settings', sa.Column('refresh_recurrence', sa.String(length=7), nullable=False, server_default='WEEKLY'))
    op.add_column('operational_settings', sa.Column('refresh_weekday', sa.Integer(), nullable=True, server_default='5'))
    op.add_column('operational_settings', sa.Column('refresh_day_of_month', sa.Integer(), nullable=True))
    op.add_column('operational_settings', sa.Column('refresh_time_of_day', sa.String(length=5), nullable=False, server_default='02:00'))
    op.add_column('operational_settings', sa.Column('retry_failed_next_day', sa.Boolean(), nullable=False, server_default=sa.true()))

    op.create_table(
        'collection_jobs',
        sa.Column('trigger_type', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=21), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('trigger_context', sa.JSON(), nullable=False),
        sa.Column('requested_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('parent_job_id', sa.Uuid(), nullable=True),
        sa.Column('schedule_slot_key', sa.String(length=120), nullable=True),
        sa.Column('total_items', sa.Integer(), nullable=False),
        sa.Column('succeeded_items', sa.Integer(), nullable=False),
        sa.Column('failed_items', sa.Integer(), nullable=False),
        sa.Column('skipped_items', sa.Integer(), nullable=False),
        sa.Column('error_summary', sa.Text(), nullable=True),
        sa.Column('created_at', app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column('started_at', app.models.common.UTCDateTime(timezone=True), nullable=True),
        sa.Column('finished_at', app.models.common.UTCDateTime(timezone=True), nullable=True),
        sa.Column('updated_at', app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['parent_job_id'], ['collection_jobs.id']),
        sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('schedule_slot_key', name='uq_collection_jobs_schedule_slot'),
    )
    op.create_index('ix_collection_jobs_dispatch', 'collection_jobs', ['status', 'priority', 'created_at'])
    op.create_index('ix_collection_jobs_parent_job_id', 'collection_jobs', ['parent_job_id'])
    op.create_index('ix_collection_jobs_requested_by_user_id', 'collection_jobs', ['requested_by_user_id'])
    op.create_index('ix_collection_jobs_status', 'collection_jobs', ['status'])
    op.create_index('ix_collection_jobs_trigger_type', 'collection_jobs', ['trigger_type'])
    op.create_table(
        'collection_job_items',
        sa.Column('job_id', sa.Uuid(), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=7), nullable=False),
        sa.Column('started_at', app.models.common.UTCDateTime(timezone=True), nullable=True),
        sa.Column('finished_at', app.models.common.UTCDateTime(timezone=True), nullable=True),
        sa.Column('crawl_run_id', sa.Uuid(), nullable=True),
        sa.Column('error_code', sa.String(length=120), nullable=True),
        sa.Column('error_summary', sa.Text(), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('created_at', app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column('updated_at', app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['crawl_run_id'], ['crawl_runs.id']),
        sa.ForeignKeyConstraint(['job_id'], ['collection_jobs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_id'], ['sources.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id', 'source_id', name='uq_collection_job_item_source'),
    )
    op.create_index('ix_collection_job_items_crawl_run_id', 'collection_job_items', ['crawl_run_id'])
    op.create_index('ix_collection_job_items_dispatch', 'collection_job_items', ['status', 'job_id', 'sequence'])
    op.create_index('ix_collection_job_items_job_id', 'collection_job_items', ['job_id'])
    op.create_index('ix_collection_job_items_source_id', 'collection_job_items', ['source_id'])
    op.create_index('ix_collection_job_items_status', 'collection_job_items', ['status'])


def downgrade() -> None:
    op.drop_table('collection_job_items')
    op.drop_table('collection_jobs')
    op.drop_column('operational_settings', 'retry_failed_next_day')
    op.drop_column('operational_settings', 'refresh_time_of_day')
    op.drop_column('operational_settings', 'refresh_day_of_month')
    op.drop_column('operational_settings', 'refresh_weekday')
    op.drop_column('operational_settings', 'refresh_recurrence')
    op.drop_column('operational_settings', 'automatic_refresh_enabled')
    op.drop_index('ix_sources_scheduled_refresh_enabled', table_name='sources')
    op.drop_column('sources', 'scheduled_refresh_enabled')
    op.drop_index('ix_agencies_region_code', table_name='agencies')
    op.drop_column('agencies', 'region_code')
