"""Index Source collection method for faceted target resolution.

Revision ID: a0c406c40001
Revises: f9b207c06c03
"""

from alembic import op


revision = "a0c406c40001"
down_revision = "f9b207c06c03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_sources_collection_method", "sources", ["collection_method"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sources_collection_method", table_name="sources")
