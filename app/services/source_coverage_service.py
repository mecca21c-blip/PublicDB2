"""Canonical Source coverage policy."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models import Source, SourceCoverageMode


class SourceCoverageService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def set_mode(self, source_id: uuid.UUID, mode: SourceCoverageMode) -> Source:
        source = self.session.get(Source, source_id)
        if source is None:
            raise ValueError("Source not found.")
        source.coverage_mode = mode
        self.session.commit()
        return source
