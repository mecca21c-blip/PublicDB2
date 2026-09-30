"""add three way collection

Revision ID: c7f205b05b01
Revises: a5c105a05a01
"""

from alembic import op
import sqlalchemy as sa

import app.models.common


revision = "c7f205b05b01"
down_revision = "a5c105a05a01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_scrape_configs",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("extract_contacts", sa.Boolean(), nullable=False),
        sa.Column("extract_directory", sa.Boolean(), nullable=False),
        sa.Column("created_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id"),
    )
    op.create_index("ix_source_scrape_configs_source_id", "source_scrape_configs", ["source_id"], unique=True)
    op.create_table(
        "source_crawl_configs",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.Enum("PATH_PREFIX", "SAME_DOMAIN", name="crawlscope", native_enum=False), nullable=False),
        sa.Column("allowed_path", sa.String(length=2048), nullable=False),
        sa.Column("max_depth", sa.Integer(), nullable=False),
        sa.Column("max_pages", sa.Integer(), nullable=False),
        sa.Column("request_delay_ms", sa.Integer(), nullable=False),
        sa.Column("extract_contacts", sa.Boolean(), nullable=False),
        sa.Column("extract_directory", sa.Boolean(), nullable=False),
        sa.Column("created_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id"),
    )
    op.create_index("ix_source_crawl_configs_source_id", "source_crawl_configs", ["source_id"], unique=True)
    op.create_table(
        "source_api_configs",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Enum("OPEN_API", "RSS", "ATOM", name="apisourcekind", native_enum=False), nullable=False),
        sa.Column("response_format", sa.Enum("HTML", "JSON", "XML", "CSV", "XLSX", "PDF", "TEXT", "UNKNOWN", name="dataformat", native_enum=False), nullable=False),
        sa.Column("record_path", sa.String(length=1000), nullable=True),
        sa.Column("field_mapping", sa.JSON(), nullable=False),
        sa.Column("static_params", sa.JSON(), nullable=False),
        sa.Column("discovery_only", sa.Boolean(), nullable=False),
        sa.Column("pagination_mode", sa.Enum("NONE", "PAGE_NUMBER", name="apipaginationmode", native_enum=False), nullable=False),
        sa.Column("page_parameter", sa.String(length=100), nullable=True),
        sa.Column("page_size_parameter", sa.String(length=100), nullable=True),
        sa.Column("page_size", sa.Integer(), nullable=False),
        sa.Column("start_page", sa.Integer(), nullable=False),
        sa.Column("max_pages", sa.Integer(), nullable=False),
        sa.Column("request_delay_ms", sa.Integer(), nullable=False),
        sa.Column("auth_mode", sa.Enum("NONE", "QUERY_API_KEY", "HEADER_API_KEY", name="apiauthmode", native_enum=False), nullable=False),
        sa.Column("credential_ref", sa.String(length=200), nullable=True),
        sa.Column("credential_name", sa.String(length=200), nullable=True),
        sa.Column("credential_expires_on", sa.Date(), nullable=True),
        sa.Column("catalog_id", sa.String(length=200), nullable=True),
        sa.Column("catalog_version", sa.String(length=100), nullable=True),
        sa.Column("created_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id"),
    )
    op.create_index("ix_source_api_configs_source_id", "source_api_configs", ["source_id"], unique=True)
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.add_column(sa.Column("collection_method_snapshot", sa.Enum("API", "WEB_PAGE", "WEB_CRAWL", "FILE", "DOCUMENT", name="collectionmethod", native_enum=False), nullable=True))
        batch_op.add_column(sa.Column("collection_kind_snapshot", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("collection_config_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("collection_statistics", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("collector_version", sa.String(length=50), nullable=True))
    op.create_table(
        "extracted_feed_items",
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("item_identity", sa.String(length=2048), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("link", sa.String(length=2048), nullable=True),
        sa.Column("published_at", sa.String(length=200), nullable=True),
        sa.Column("updated_at_text", sa.String(length=200), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("source_locator", sa.String(length=1000), nullable=False),
        sa.Column("structured_payload", sa.JSON(), nullable=True),
        sa.Column("created_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["observation_id"], ["observations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extracted_feed_items_extraction_run_id", "extracted_feed_items", ["extraction_run_id"])
    op.create_index("ix_extracted_feed_items_observation_id", "extracted_feed_items", ["observation_id"])
    source_ids = [row[0] for row in op.get_bind().execute(sa.text("SELECT id FROM sources WHERE collection_method = 'WEB_PAGE'"))]
    table = sa.table(
        "source_scrape_configs",
        sa.column("id", sa.Uuid()), sa.column("source_id", sa.Uuid()),
        sa.column("extract_contacts", sa.Boolean()), sa.column("extract_directory", sa.Boolean()),
        sa.column("created_at", app.models.common.UTCDateTime()), sa.column("updated_at", app.models.common.UTCDateTime()),
    )
    if source_ids:
        import uuid
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        op.bulk_insert(table, [
            {
                "id": uuid.uuid4(),
                "source_id": source_id if isinstance(source_id, uuid.UUID) else uuid.UUID(str(source_id)),
                "extract_contacts": True, "extract_directory": True,
                "created_at": now, "updated_at": now,
            }
            for source_id in source_ids
        ])


def downgrade() -> None:
    op.drop_index("ix_extracted_feed_items_observation_id", table_name="extracted_feed_items")
    op.drop_index("ix_extracted_feed_items_extraction_run_id", table_name="extracted_feed_items")
    op.drop_table("extracted_feed_items")
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.drop_column("collector_version")
        batch_op.drop_column("collection_statistics")
        batch_op.drop_column("collection_config_snapshot")
        batch_op.drop_column("collection_kind_snapshot")
        batch_op.drop_column("collection_method_snapshot")
    op.drop_index("ix_source_api_configs_source_id", table_name="source_api_configs")
    op.drop_table("source_api_configs")
    op.drop_index("ix_source_crawl_configs_source_id", table_name="source_crawl_configs")
    op.drop_table("source_crawl_configs")
    op.drop_index("ix_source_scrape_configs_source_id", table_name="source_scrape_configs")
    op.drop_table("source_scrape_configs")
