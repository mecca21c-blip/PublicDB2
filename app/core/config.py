"""Project-owned runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "db" / "publicdb2.sqlite3"
DATABASE_URL_ENV = "PUBLICDB2_DATABASE_URL"


def get_database_url() -> str:
    configured = os.getenv(DATABASE_URL_ENV)
    if configured:
        return configured
    return f"sqlite+pysqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"
