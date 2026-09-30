"""04B baseline, detection, coverage, and review action API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_operator, require_viewer
from app.api.schemas import ReviewDecision, SourceCoverageUpdate
from app.services.master_promotion_apply_service import MasterPromotionApplyService
from app.services.master_promotion_planner import PromotionError
from app.services.review_service import ReviewConflict, ReviewService
from app.services.source_change_detection_service import DetectionError, SourceChangeDetectionService
from app.services.source_coverage_service import SourceCoverageService
from app.models import ExtractionRun


router = APIRouter(prefix="/api", tags=["confirmed-review"], dependencies=[Depends(require_viewer)])


def _agency(value: uuid.UUID | None) -> uuid.UUID | None:
    return value


@router.get("/extractions/{extraction_run_id}/promotion-preview")
def promotion_preview(
    extraction_run_id: uuid.UUID, agency_id: uuid.UUID | None = None,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return MasterPromotionApplyService(session).preview(extraction_run_id, _agency(agency_id))
    except PromotionError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/extractions/{extraction_run_id}/workflow")
def workflow(
    extraction_run_id: uuid.UUID, agency_id: uuid.UUID | None = None,
    session: Session = Depends(get_session),
) -> dict:
    extraction = session.get(ExtractionRun, extraction_run_id)
    if extraction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extraction not found.")
    try:
        planner = MasterPromotionApplyService(session)
        resolved = planner.planner.resolve_agency(extraction.observation.source_id, agency_id)
        detector = SourceChangeDetectionService(session)
        if detector.baseline_exists(extraction.observation.source_id, resolved):
            return {"action": "detect", "agency_id": str(resolved)}
        return {"action": "promote", "agency_id": str(resolved), "preview": planner.preview(extraction_run_id, resolved)}
    except PromotionError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/extractions/{extraction_run_id}/promote", dependencies=[Depends(require_operator)])
def promote(
    extraction_run_id: uuid.UUID, agency_id: uuid.UUID | None = None,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return MasterPromotionApplyService(session).apply(extraction_run_id, _agency(agency_id))
    except PromotionError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/extractions/{extraction_run_id}/detect", dependencies=[Depends(require_operator)])
def detect(
    extraction_run_id: uuid.UUID, agency_id: uuid.UUID | None = None,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return SourceChangeDetectionService(session).generate(extraction_run_id, _agency(agency_id))
    except (PromotionError, DetectionError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/review/{candidate_id}/approve", dependencies=[Depends(require_operator)])
def approve(
    candidate_id: uuid.UUID, payload: ReviewDecision,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return ReviewService(session).approve(candidate_id, payload.note)
    except ReviewConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/review/{candidate_id}/keep", dependencies=[Depends(require_operator)])
def keep(
    candidate_id: uuid.UUID, payload: ReviewDecision,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return ReviewService(session).reject(candidate_id, payload.note)
    except ReviewConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/review/{candidate_id}/defer", dependencies=[Depends(require_operator)])
def defer(
    candidate_id: uuid.UUID, payload: ReviewDecision,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return ReviewService(session).defer(candidate_id, payload.note)
    except ReviewConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.patch("/sources/{source_id}/coverage", dependencies=[Depends(require_operator)])
def update_coverage(
    source_id: uuid.UUID, payload: SourceCoverageUpdate,
    session: Session = Depends(get_session),
) -> dict:
    try:
        source = SourceCoverageService(session).set_mode(source_id, payload.coverage_mode)
        return {"source_id": str(source.id), "coverage_mode": source.coverage_mode.value}
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
