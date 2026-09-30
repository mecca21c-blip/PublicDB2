"""Shared verification for immutable HTML Observation artifacts."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import MAX_RESPONSE_BYTES
from app.models import Observation, StageStatus


HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
HTML_EXTENSIONS = {".html", ".htm", ".xhtml"}


class HTMLArtifactValidationError(ValueError):
    """An Observation cannot safely be parsed as trusted HTML evidence."""


@dataclass(frozen=True)
class VerifiedHTMLArtifact:
    body: bytes
    declared_charset: str | None


def load_verified_html_artifact(
    session: Session,
    observation_id: uuid.UUID,
    *,
    project_root: Path,
    raw_root: Path,
) -> VerifiedHTMLArtifact:
    observation = session.get(Observation, observation_id)
    if observation is None:
        raise HTMLArtifactValidationError(
            "observation disappeared before extraction"
        )
    if observation.crawl_run.raw_status is not StageStatus.SUCCESS:
        raise HTMLArtifactValidationError(
            "observation is not from a successful crawl run"
        )
    if not observation.artifact_path:
        raise HTMLArtifactValidationError(
            "observation has no raw artifact path"
        )
    if not observation.artifact_sha256:
        raise HTMLArtifactValidationError(
            "observation has no artifact SHA-256"
        )

    relative_path = Path(observation.artifact_path)
    if relative_path.is_absolute():
        raise HTMLArtifactValidationError(
            "artifact path must be project-relative"
        )
    artifact_path = (project_root / relative_path).resolve()
    if not artifact_path.is_relative_to(raw_root):
        raise HTMLArtifactValidationError(
            "artifact path escapes the configured raw root"
        )

    content_type = (observation.content_type or "").lower()
    if (
        content_type not in HTML_CONTENT_TYPES
        or artifact_path.suffix.lower() not in HTML_EXTENSIONS
    ):
        raise HTMLArtifactValidationError(
            "only HTML/XHTML observations are supported by this extractor"
        )

    body = bytearray()
    digest = hashlib.sha256()
    try:
        with artifact_path.open("rb") as artifact_file:
            while chunk := artifact_file.read(64 * 1024):
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise HTMLArtifactValidationError(
                        "artifact exceeds the configured HTML extraction size limit"
                    )
                digest.update(chunk)
                body.extend(chunk)
    except FileNotFoundError as error:
        raise HTMLArtifactValidationError("artifact file does not exist") from error

    actual_sha256 = digest.hexdigest()
    if not hmac.compare_digest(
        actual_sha256.lower(),
        observation.artifact_sha256.lower(),
    ):
        raise HTMLArtifactValidationError(
            "artifact SHA-256 does not match Observation"
        )

    return VerifiedHTMLArtifact(
        body=bytes(body),
        declared_charset=observation.declared_charset,
    )
