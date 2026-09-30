"""Reconcile collection work whose database claim no longer represents a live worker."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CrawlRun, OperationClaim, RunStatus, StageStatus
from app.models.common import utc_now


COLLECTION_CLAIM_PREFIX = 'source:'
COLLECTION_STALE_AFTER = timedelta(minutes=10)
INTERRUPTION_REASON = 'Collection interrupted before terminal finalization; recovered as failed.'


def source_claim_key(source_id) -> str:
    return f'{COLLECTION_CLAIM_PREFIX}{source_id}'


def reconcile_stale_collections(
    session: Session, *, stale_after: timedelta = COLLECTION_STALE_AFTER
) -> int:
    now = utc_now()
    cutoff = now - stale_after
    claims = {
        claim.resource_key: claim
        for claim in session.scalars(
            select(OperationClaim).where(
                OperationClaim.resource_key.like(f'{COLLECTION_CLAIM_PREFIX}%'),
                OperationClaim.status == 'ACTIVE',
            )
        )
    }
    recovered = 0
    for run in session.scalars(select(CrawlRun).where(CrawlRun.status == RunStatus.RUNNING)):
        claim = claims.get(source_claim_key(run.source_id))
        heartbeat = claim.heartbeat_at if claim is not None else (run.heartbeat_at or run.started_at)
        if heartbeat is not None and heartbeat >= cutoff:
            continue
        run.status = RunStatus.FAILED
        if run.connection_status is StageStatus.PENDING:
            run.connection_status = StageStatus.SKIPPED
        if run.raw_status is StageStatus.PENDING:
            run.raw_status = StageStatus.SKIPPED
        if run.extraction_status is StageStatus.PENDING:
            run.extraction_status = StageStatus.SKIPPED
        run.error_summary = INTERRUPTION_REASON
        run.finished_at = now
        run.heartbeat_at = now
        recovered += 1
        if claim is not None:
            session.delete(claim)
    stale_claims = [claim for claim in claims.values() if claim.heartbeat_at < cutoff]
    for claim in stale_claims:
        if claim in session.deleted:
            continue
        session.delete(claim)
    if recovered or stale_claims:
        session.commit()
    return recovered
