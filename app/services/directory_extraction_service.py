"""Versioned orchestration for structured staff-directory candidates."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.collectors.staff_directory_extractor import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    StaffDirectoryExtractor,
)
from app.core.config import PROJECT_ROOT
from app.core.discovery_quality import meaningful_directory_values
from app.models import (
    ExtractedDirectoryRecord,
    ExtractionRun,
    ExtractionStatus,
    Observation,
)
from app.models.common import utc_now
from app.services.artifact_validation import (
    HTMLArtifactValidationError,
    load_verified_html_artifact,
)


class DirectoryObservationNotFoundError(LookupError):
    """The requested evidence Observation does not exist."""


class DirectoryExtractionFinalizationError(RuntimeError):
    """Directory extraction could not be finalized truthfully in the DB."""


@dataclass(frozen=True)
class DirectoryExtractionResult:
    extraction_run: ExtractionRun
    records: list[ExtractedDirectoryRecord]
    reused: bool


def _error_summary(error: Exception) -> str:
    summary = str(error).strip() or error.__class__.__name__
    return summary[:1000]


class DirectoryExtractionService:
    """Verify one artifact and persist only structured directory candidates."""

    def __init__(
        self,
        session: Session,
        *,
        extractor: StaffDirectoryExtractor | None = None,
        project_root: Path = PROJECT_ROOT,
        raw_root: Path | None = None,
    ) -> None:
        self._session = session
        self._extractor = extractor or StaffDirectoryExtractor()
        self._project_root = project_root.resolve()
        self._raw_root = (
            raw_root.resolve()
            if raw_root is not None
            else (self._project_root / "data" / "raw").resolve()
        )
        if not self._raw_root.is_relative_to(self._project_root):
            raise ValueError("raw artifact root must be inside the project root")

    def extract(self, observation_id: uuid.UUID) -> DirectoryExtractionResult:
        observation = self._session.get(Observation, observation_id)
        if observation is None:
            raise DirectoryObservationNotFoundError(
                f"observation not found: {observation_id}"
            )

        previous = self._successful_run(observation_id)
        if previous is not None:
            return DirectoryExtractionResult(
                extraction_run=previous,
                records=self._records_for_run(previous.id),
                reused=True,
            )

        run = ExtractionRun(
            observation_id=observation_id,
            extractor_name=self._extractor.name,
            extractor_version=self._extractor.version,
            status=ExtractionStatus.RUNNING,
            started_at=utc_now(),
            candidates_found=0,
        )
        self._session.add(run)
        self._session.commit()
        run_id = run.id

        try:
            artifact = load_verified_html_artifact(
                self._session,
                observation_id,
                project_root=self._project_root,
                raw_root=self._raw_root,
            )
            extracted_values = self._extractor.extract(
                artifact.body,
                declared_charset=artifact.declared_charset,
            )
            extracted_values = [
                value for value in extracted_values
                if meaningful_directory_values(
                    row_text=value.row_text,
                    org_unit_text=value.org_unit_text,
                    duty_text=value.duty_text,
                    position_text=value.position_text,
                    person_name_text=value.person_name_text,
                    phone_text=value.phone_text,
                    email_text=value.email_text,
                    fax_text=value.fax_text,
                )
            ]
        except Exception as error:
            failed_run = self._mark_failed(run_id, error)
            return DirectoryExtractionResult(
                extraction_run=failed_run,
                records=[],
                reused=False,
            )

        records = [
            ExtractedDirectoryRecord(
                extraction_run_id=run_id,
                observation_id=observation_id,
                record_type=value.record_type,
                org_unit_text=value.org_unit_text,
                duty_text=value.duty_text,
                position_text=value.position_text,
                person_name_text=value.person_name_text,
                phone_text=value.phone_text,
                email_text=value.email_text,
                fax_text=value.fax_text,
                row_text=value.row_text,
                source_locator=value.source_locator,
                structured_payload=value.structured_payload,
            )
            for value in extracted_values
        ]

        try:
            persistent_run = self._session.get(ExtractionRun, run_id)
            if persistent_run is None:
                raise DirectoryExtractionFinalizationError(
                    "extraction run disappeared before finalization"
                )
            self._session.add_all(records)
            persistent_run.status = ExtractionStatus.SUCCESS
            persistent_run.finished_at = utc_now()
            persistent_run.candidates_found = len(records)
            persistent_run.error_summary = None
            self._session.commit()
            return DirectoryExtractionResult(
                extraction_run=persistent_run,
                records=records,
                reused=False,
            )
        except (SQLAlchemyError, DirectoryExtractionFinalizationError) as error:
            self._session.rollback()
            self._best_effort_failed_finalization(run_id, error)
            raise DirectoryExtractionFinalizationError(
                "directory candidate extraction could not be finalized"
            ) from error

    def _successful_run(self, observation_id: uuid.UUID) -> ExtractionRun | None:
        return self._session.scalar(
            select(ExtractionRun)
            .where(
                ExtractionRun.observation_id == observation_id,
                ExtractionRun.extractor_name == self._extractor.name,
                ExtractionRun.extractor_version == self._extractor.version,
                ExtractionRun.status == ExtractionStatus.SUCCESS,
            )
            .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
            .limit(1)
        )

    def _records_for_run(
        self,
        extraction_run_id: uuid.UUID,
    ) -> list[ExtractedDirectoryRecord]:
        return list(
            self._session.scalars(
                select(ExtractedDirectoryRecord)
                .where(
                    ExtractedDirectoryRecord.extraction_run_id
                    == extraction_run_id
                )
                .order_by(
                    ExtractedDirectoryRecord.created_at,
                    ExtractedDirectoryRecord.id,
                )
            )
        )

    def _mark_failed(
        self,
        run_id: uuid.UUID,
        error: Exception,
    ) -> ExtractionRun:
        self._session.rollback()
        run = self._session.get(ExtractionRun, run_id)
        if run is None:
            raise DirectoryExtractionFinalizationError(
                f"extraction run disappeared while recording failure: {run_id}"
            )
        run.status = ExtractionStatus.FAILED
        run.finished_at = utc_now()
        run.candidates_found = 0
        run.error_summary = _error_summary(error)
        self._session.commit()
        return run

    def _best_effort_failed_finalization(
        self,
        run_id: uuid.UUID,
        error: Exception,
    ) -> None:
        try:
            self._mark_failed(run_id, error)
        except (SQLAlchemyError, DirectoryExtractionFinalizationError):
            self._session.rollback()
