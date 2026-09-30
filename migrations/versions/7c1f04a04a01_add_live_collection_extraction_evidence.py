"""add live collection extraction evidence

Revision ID: 7c1f04a04a01
Revises: 2dd871292d0a
Create Date: 2026-09-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models.common


revision: str = "7c1f04a04a01"
down_revision: Union[str, None] = "2dd871292d0a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.add_column(sa.Column("http_status", sa.Integer(), nullable=True))
    with op.batch_alter_table("observations") as batch_op:
        batch_op.add_column(sa.Column("content_type", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("declared_charset", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("response_bytes", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("final_url", sa.String(length=2048), nullable=True))

    op.create_table(
        "extraction_runs",
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("extractor_name", sa.String(length=100), nullable=False),
        sa.Column("extractor_version", sa.String(length=50), nullable=False),
        sa.Column("status", sa.Enum("PENDING", "RUNNING", "SUCCESS", "FAILED", name="extractionstatus", native_enum=False), nullable=False),
        sa.Column("started_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("finished_at", app.models.common.UTCDateTime(timezone=True), nullable=True),
        sa.Column("candidates_found", sa.Integer(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["observation_id"], ["observations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extraction_runs_observation_id", "extraction_runs", ["observation_id"])
    op.create_index("ix_extraction_runs_status", "extraction_runs", ["status"])
    op.create_index("ix_extraction_runs_identity", "extraction_runs", ["observation_id", "extractor_name", "extractor_version"])

    op.create_table(
        "extracted_contact_candidates",
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_type", sa.Enum("EMAIL", "PHONE", "FAX", name="candidatetype", native_enum=False), nullable=False),
        sa.Column("raw_value", sa.String(length=500), nullable=False),
        sa.Column("normalized_value", sa.String(length=500), nullable=False),
        sa.Column("context_text", sa.Text(), nullable=True),
        sa.Column("source_locator", sa.String(length=1000), nullable=True),
        sa.Column("detection_method", sa.Enum("TEXT_PATTERN", "MAILTO", "TEL_LINK", name="detectionmethod", native_enum=False), nullable=False),
        sa.Column("created_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"]),
        sa.ForeignKeyConstraint(["observation_id"], ["observations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extracted_contact_candidates_extraction_run_id", "extracted_contact_candidates", ["extraction_run_id"])
    op.create_index("ix_extracted_contact_candidates_observation_id", "extracted_contact_candidates", ["observation_id"])
    op.create_index("ix_extracted_contact_candidates_candidate_type", "extracted_contact_candidates", ["candidate_type"])
    op.create_index("ix_extracted_contact_candidates_normalized_value", "extracted_contact_candidates", ["normalized_value"])

    op.create_table(
        "extracted_directory_records",
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("record_type", sa.Enum("STAFF_DIRECTORY_ROW", name="directoryrecordtype", native_enum=False), nullable=False),
        sa.Column("org_unit_text", sa.Text(), nullable=True),
        sa.Column("duty_text", sa.Text(), nullable=True),
        sa.Column("position_text", sa.Text(), nullable=True),
        sa.Column("person_name_text", sa.Text(), nullable=True),
        sa.Column("phone_text", sa.Text(), nullable=True),
        sa.Column("email_text", sa.Text(), nullable=True),
        sa.Column("fax_text", sa.Text(), nullable=True),
        sa.Column("row_text", sa.Text(), nullable=False),
        sa.Column("source_locator", sa.String(length=1000), nullable=False),
        sa.Column("structured_payload", sa.JSON(), nullable=True),
        sa.Column("created_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"]),
        sa.ForeignKeyConstraint(["observation_id"], ["observations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extracted_directory_records_extraction_run_id", "extracted_directory_records", ["extraction_run_id"])
    op.create_index("ix_extracted_directory_records_observation_id", "extracted_directory_records", ["observation_id"])
    op.create_index("ix_extracted_directory_records_record_type", "extracted_directory_records", ["record_type"])


def downgrade() -> None:
    op.drop_index("ix_extracted_directory_records_record_type", table_name="extracted_directory_records")
    op.drop_index("ix_extracted_directory_records_observation_id", table_name="extracted_directory_records")
    op.drop_index("ix_extracted_directory_records_extraction_run_id", table_name="extracted_directory_records")
    op.drop_table("extracted_directory_records")
    op.drop_index("ix_extracted_contact_candidates_normalized_value", table_name="extracted_contact_candidates")
    op.drop_index("ix_extracted_contact_candidates_candidate_type", table_name="extracted_contact_candidates")
    op.drop_index("ix_extracted_contact_candidates_observation_id", table_name="extracted_contact_candidates")
    op.drop_index("ix_extracted_contact_candidates_extraction_run_id", table_name="extracted_contact_candidates")
    op.drop_table("extracted_contact_candidates")
    op.drop_index("ix_extraction_runs_identity", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_status", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_observation_id", table_name="extraction_runs")
    op.drop_table("extraction_runs")
    with op.batch_alter_table("observations") as batch_op:
        batch_op.drop_column("final_url")
        batch_op.drop_column("response_bytes")
        batch_op.drop_column("declared_charset")
        batch_op.drop_column("content_type")
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.drop_column("http_status")
