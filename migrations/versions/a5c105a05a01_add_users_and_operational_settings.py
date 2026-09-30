'''add users and operational settings

Revision ID: a5c105a05a01
Revises: 9b2d04b04b01
'''

from alembic import op
import sqlalchemy as sa

import app.models.common


revision = 'a5c105a05a01'
down_revision = '9b2d04b04b01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('username', sa.String(length=150), nullable=False),
        sa.Column('normalized_username', sa.String(length=150), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=True),
        sa.Column('password_hash', sa.String(length=500), nullable=False),
        sa.Column('role', sa.Enum('ADMIN', 'OPERATOR', 'VIEWER', name='userrole', native_enum=False), nullable=False),
        sa.Column('last_login_at', app.models.common.UTCDateTime(timezone=True), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column('updated_at', app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_users_active', 'users', ['active'])
    op.create_index('ix_users_normalized_username', 'users', ['normalized_username'], unique=True)
    op.create_index('ix_users_role', 'users', ['role'])
    op.create_table(
        'operational_settings',
        sa.Column('singleton_key', sa.String(length=30), nullable=False),
        sa.Column('http_timeout_seconds', sa.Float(), nullable=False),
        sa.Column('max_response_bytes', sa.Integer(), nullable=False),
        sa.Column('user_agent', sa.String(length=500), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column('updated_at', app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('singleton_key'),
    )


def downgrade() -> None:
    op.drop_table('operational_settings')
    op.drop_index('ix_users_role', table_name='users')
    op.drop_index('ix_users_normalized_username', table_name='users')
    op.drop_index('ix_users_active', table_name='users')
    op.drop_table('users')
