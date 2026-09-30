"""Crash-safe filesystem storage for bounded raw response evidence."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import PROJECT_ROOT, runtime_paths


CONTENT_TYPE_EXTENSIONS = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/rss+xml": ".xml",
    "application/atom+xml": ".xml",
    "text/csv": ".csv",
    "text/plain": ".txt",
}
GENERIC_EXTENSION = ".bin"


@dataclass(frozen=True)
class StoredArtifact:
    relative_path: str
    sha256: str
    response_bytes: int


class RawArtifactStore:
    """Write response bytes atomically below a configured project raw root."""

    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        raw_root: Path | None = None,
    ) -> None:
        self._project_root = project_root.resolve()
        self._raw_root = (raw_root or runtime_paths(project_root).raw_root).resolve()
        if not self._raw_root.is_relative_to(self._project_root):
            raise ValueError("raw artifact root must be inside the project root")

    @property
    def raw_root(self) -> Path:
        return self._raw_root

    def store(
        self,
        *,
        source_id: uuid.UUID,
        crawl_run_id: uuid.UUID,
        observed_at: datetime,
        content: bytes,
        content_type: str,
        sequence: int | None = None,
    ) -> StoredArtifact:
        observed_utc = observed_at.astimezone(timezone.utc)
        destination_directory = (
            self._raw_root
            / f"{observed_utc.year:04d}"
            / f"{observed_utc.month:02d}"
            / f"{observed_utc.day:02d}"
            / str(source_id)
            / str(crawl_run_id)
        )
        destination_directory.mkdir(parents=True, exist_ok=True)
        extension = CONTENT_TYPE_EXTENSIONS.get(
            content_type.lower(), GENERIC_EXTENSION
        )
        filename = "response" if sequence is None else f"response_{sequence:04d}"
        final_path = destination_directory / f"{filename}{extension}"
        digest = hashlib.sha256()
        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination_directory,
                prefix=".response-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                digest.update(content)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())

            os.replace(temporary_path, final_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        return StoredArtifact(
            relative_path=final_path.relative_to(self._project_root).as_posix(),
            sha256=digest.hexdigest(),
            response_bytes=len(content),
        )

    def remove(self, artifact: StoredArtifact) -> None:
        """Best-effort removal when DB finalization cannot accept an artifact."""
        artifact_path = (self._project_root / artifact.relative_path).resolve()
        if artifact_path.is_relative_to(self._raw_root):
            artifact_path.unlink(missing_ok=True)
