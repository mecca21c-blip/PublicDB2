"""Versioned orchestration for deterministic Observation candidate extraction."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.collectors.html_contact_extractor import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    HTMLContactExtractor,
)
from app.core.config import PROJECT_ROOT
from app.core.discovery_quality import deduplicate_contacts
from app.models import (
    ExtractedContactCandidate,
    ExtractionRun,
    ExtractionStatus,
    Observation,
)
from app.models.common import utc_now
from app.services.artifact_validation import (
    HTMLArtifactValidationError,
    load_verified_html_artifact,
)


class ObservationNotFoundError(LookupError):
    """The requested evidence Observation does not exist."""


class ExtractionInputError(ValueError):
    """The Observation cannot safely be parsed by the HTML extractor."""


class ExtractionFinalizationError(RuntimeError):
    """Extraction output could not be finalized truthfully in the DB."""


@dataclass(frozen=True)
class ExtractionResult:
    extraction_run: ExtractionRun
    candidates: list[ExtractedContactCandidate]
    reused: bool


def _error_summary(error: Exception) -> str:
    summary = str(error).strip() or error.__class__.__name__
    return summary[:1000]


class ContactExtractionService:
    """Verify one raw artifact and persist only evidence-derived candidates."""

    def __init__(
        self,
        session: Session,
        *,
        extractor: HTMLContactExtractor | None = None,
        project_root: Path = PROJECT_ROOT,
        raw_root: Path | None = None,
    ) -> None:
        self._session = session
        self._extractor = extractor or HTMLContactExtractor()
        self._project_root = project_root.resolve()
        self._raw_root = (
            raw_root.resolve()
            if raw_root is not None
            else (self._project_root / "data" / "raw").resolve()
        )
        if not self._raw_root.is_relative_to(self._project_root):
            raise ValueError("raw artifact root must be inside the project root")

    def extract(self, observation_id: uuid.UUID) -> ExtractionResult:
        observation = self._session.get(Observation, observation_id)
        if observation is None:
            raise ObservationNotFoundError(
                f"observation not found: {observation_id}"
            )

        previous = self._successful_run(observation_id)
        if previous is not None:
            return ExtractionResult(
                extraction_run=previous,
                candidates=self._candidates_for_run(previous.id),
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
            html, charset = self._verified_html(observation_id)
            extracted_values = deduplicate_contacts(
                self._extractor.extract(
                    html,
                    declared_charset=charset,
                )
            )
        except Exception as error:
            failed_run = self._mark_failed(run_id, error)
            return ExtractionResult(
                extraction_run=failed_run,
                candidates=[],
                reused=False,
            )

        candidates = [
            ExtractedContactCandidate(
                extraction_run_id=run_id,
                observation_id=observation_id,
                candidate_type=value.candidate_type,
                raw_value=value.raw_value,
                normalized_value=value.normalized_value,
                context_text=value.context_text,
                source_locator=value.source_locator,
                detection_method=value.detection_method,
            )
            for value in extracted_values
        ]

        try:
            persistent_run = self._session.get(ExtractionRun, run_id)
            if persistent_run is None:
                raise ExtractionFinalizationError(
                    "extraction run disappeared before finalization"
                )
            self._session.add_all(candidates)
            persistent_run.status = ExtractionStatus.SUCCESS
            persistent_run.finished_at = utc_now()
            persistent_run.candidates_found = len(candidates)
            persistent_run.error_summary = None
            self._session.commit()
            return ExtractionResult(
                extraction_run=persistent_run,
                candidates=candidates,
                reused=False,
            )
        except (SQLAlchemyError, ExtractionFinalizationError) as error:
            self._session.rollback()
            self._best_effort_failed_finalization(run_id, error)
            raise ExtractionFinalizationError(
                "contact candidate extraction could not be finalized"
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

    def _candidates_for_run(
        self,
        extraction_run_id: uuid.UUID,
    ) -> list[ExtractedContactCandidate]:
        return list(
            self._session.scalars(
                select(ExtractedContactCandidate)
                .where(
                    ExtractedContactCandidate.extraction_run_id
                    == extraction_run_id
                )
                .order_by(
                    ExtractedContactCandidate.created_at,
                    ExtractedContactCandidate.id,
                )
            )
        )

    def _verified_html(
        self,
        observation_id: uuid.UUID,
    ) -> tuple[bytes, str | None]:
        try:
            artifact = load_verified_html_artifact(
                self._session,
                observation_id,
                project_root=self._project_root,
                raw_root=self._raw_root,
            )
        except HTMLArtifactValidationError as error:
            raise ExtractionInputError(str(error)) from error
        return artifact.body, artifact.declared_charset

    def _mark_failed(
        self,
        run_id: uuid.UUID,
        error: Exception,
    ) -> ExtractionRun:
        self._session.rollback()
        run = self._session.get(ExtractionRun, run_id)
        if run is None:
            raise ExtractionFinalizationError(
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
        except (SQLAlchemyError, ExtractionFinalizationError):
            self._session.rollback()
