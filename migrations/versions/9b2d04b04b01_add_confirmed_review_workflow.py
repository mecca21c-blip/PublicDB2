"""add confirmed contact review workflow

Revision ID: 9b2d04b04b01
Revises: 7c1f04a04a01
Create Date: 2026-09-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models.common


revision: str = "9b2d04b04b01"
down_revision: Union[str, None] = "7c1f04a04a01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


coverage_enum = sa.Enum(
    "UNKNOWN", "ADDITIVE_ONLY", "COMPLETE_SNAPSHOT",
    name="sourcecoveragemode", native_enum=False,
)


def upgrade() -> None:
    op.add_column("sources", sa.Column(
        "coverage_mode", coverage_enum, nullable=False, server_default="UNKNOWN"
    ))

    with op.batch_alter_table("change_detections") as batch_op:
        batch_op.add_column(sa.Column("extraction_run_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("agency_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("detector_name", sa.String(length=100), nullable=False, server_default="source_change"))
        batch_op.add_column(sa.Column("detector_version", sa.String(length=50), nullable=False, server_default="1.0"))
        batch_op.add_column(sa.Column("coverage_mode", coverage_enum, nullable=False, server_default="UNKNOWN"))
        batch_op.create_foreign_key("fk_change_detection_extraction", "extraction_runs", ["extraction_run_id"], ["id"])
        batch_op.create_foreign_key("fk_change_detection_agency", "agencies", ["agency_id"], ["id"])
        batch_op.create_index("ix_change_detections_extraction_run_id", ["extraction_run_id"])
        batch_op.create_index("ix_change_detections_agency_id", ["agency_id"])
        batch_op.create_index(
            "ix_change_detections_identity",
            ["extraction_run_id", "agency_id", "detector_name", "detector_version"],
        )

    op.create_table(
        "detected_change_candidates",
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("agency_id", sa.Uuid(), nullable=False),
        sa.Column("directory_record_id", sa.Uuid(), nullable=True),
        sa.Column("contact_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("entity_type", sa.Enum(
            "AGENCY", "ORG_UNIT", "DUTY", "PERSON", "PERSON_ASSIGNMENT",
            "CONTACT_POINT", "SOURCE", "SOURCE_BINDING",
            name="entitytype", native_enum=False,
        ), nullable=False),
        sa.Column("existing_entity_id", sa.Uuid(), nullable=True),
        sa.Column("proposed_event_type", sa.Enum(
            "ENTITY_ADDED", "ENTITY_CHANGED", "ENTITY_MISSING", "ENTITY_RESTORED",
            "CONTACT_CHANGED", "CONTACT_ADDED", "CONTACT_MISSING", "OTHER",
            name="changeeventtype", native_enum=False,
        ), nullable=False),
        sa.Column("old_value", sa.JSON(), nullable=True),
        sa.Column("new_value", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("review_status", sa.Enum(
            "PENDING_REVIEW", "APPROVED", "REJECTED", "IGNORED", "DEFERRED",
            name="reviewstatus", native_enum=False,
        ), nullable=False),
        sa.Column("candidate_key", sa.String(length=500), nullable=False),
        sa.Column("source_locator", sa.String(length=1000), nullable=True),
        sa.Column("actionable", sa.Boolean(), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("created_at", app.models.common.UTCDateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", app.models.common.UTCDateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["agency_id"], ["agencies.id"]),
        sa.ForeignKeyConstraint(["contact_candidate_id"], ["extracted_contact_candidates.id"]),
        sa.ForeignKeyConstraint(["detection_run_id"], ["change_detections.id"]),
        sa.ForeignKeyConstraint(["directory_record_id"], ["extracted_directory_records.id"]),
        sa.ForeignKeyConstraint(["observation_id"], ["observations.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("detection_run_id", "candidate_key", name="uq_detected_candidate_key"),
    )
    for column in (
        "detection_run_id", "source_id", "observation_id", "agency_id",
        "directory_record_id", "contact_candidate_id", "entity_type", "review_status",
    ):
        op.create_index(f"ix_detected_change_candidates_{column}", "detected_change_candidates", [column])
    op.create_index(
        "ix_detected_candidates_review", "detected_change_candidates",
        ["review_status", "actionable"],
    )


def downgrade() -> None:
    op.drop_index("ix_detected_candidates_review", table_name="detected_change_candidates")
    for column in reversed((
        "detection_run_id", "source_id", "observation_id", "agency_id",
        "directory_record_id", "contact_candidate_id", "entity_type", "review_status",
    )):
        op.drop_index(f"ix_detected_change_candidates_{column}", table_name="detected_change_candidates")
    op.drop_table("detected_change_candidates")
    with op.batch_alter_table("change_detections") as batch_op:
        batch_op.drop_index("ix_change_detections_identity")
        batch_op.drop_index("ix_change_detections_agency_id")
        batch_op.drop_index("ix_change_detections_extraction_run_id")
        batch_op.drop_constraint("fk_change_detection_agency", type_="foreignkey")
        batch_op.drop_constraint("fk_change_detection_extraction", type_="foreignkey")
        batch_op.drop_column("coverage_mode")
        batch_op.drop_column("detector_version")
        batch_op.drop_column("detector_name")
        batch_op.drop_column("agency_id")
        batch_op.drop_column("extraction_run_id")
    op.drop_column("sources", "coverage_mode")
