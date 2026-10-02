"""add crawl include and exclude path lists

Revision ID: b1d507c06c4b
Revises: a0c406c40001
"""

from alembic import op
import sqlalchemy as sa


revision = "b1d507c06c4b"
down_revision = "a0c406c40001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("source_crawl_configs") as batch_op:
        batch_op.add_column(sa.Column("allowed_paths", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("excluded_paths", sa.JSON(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE source_crawl_configs "
            "SET allowed_paths = json_array(allowed_path), excluded_paths = json('[]')"
        )
    )
    with op.batch_alter_table("source_crawl_configs") as batch_op:
        batch_op.alter_column("allowed_paths", existing_type=sa.JSON(), nullable=False)
        batch_op.alter_column("excluded_paths", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("source_crawl_configs") as batch_op:
        batch_op.drop_column("excluded_paths")
        batch_op.drop_column("allowed_paths")
