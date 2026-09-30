"""Database-backed claims used by long-running and idempotent operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import OperationClaim
from app.models.common import utc_now


@dataclass(frozen=True)
class ClaimResult:
    acquired: bool
    claim: OperationClaim


class OperationClaimService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def acquire(self, resource_key: str, operation: str, *, commit: bool = False) -> ClaimResult:
        now = utc_now()
        claim = OperationClaim(
            resource_key=resource_key,
            operation=operation,
            owner_token=uuid.uuid4().hex,
            status='ACTIVE',
            claimed_at=now,
            heartbeat_at=now,
        )
        try:
            self.session.add(claim)
            self.session.flush()
            if commit:
                self.session.commit()
            return ClaimResult(True, claim)
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(OperationClaim).where(OperationClaim.resource_key == resource_key)
            )
            if existing is None:
                raise
            return ClaimResult(False, existing)

    def heartbeat(self, claim_id: uuid.UUID, owner_token: str) -> None:
        claim = self.session.get(OperationClaim, claim_id)
        if claim is not None and claim.owner_token == owner_token and claim.status == 'ACTIVE':
            claim.heartbeat_at = utc_now()
            self.session.commit()

    def complete(self, claim: OperationClaim, result: dict) -> None:
        claim.status = 'COMPLETED'
        claim.result_payload = result
        claim.completed_at = utc_now()
        claim.heartbeat_at = claim.completed_at

    def release(self, claim_id: uuid.UUID, owner_token: str) -> None:
        self.session.rollback()
        claim = self.session.get(OperationClaim, claim_id)
        if claim is not None and claim.owner_token == owner_token:
            self.session.delete(claim)
            self.session.commit()
