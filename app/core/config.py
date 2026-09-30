"""Project-owned runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT_ENV = "PUBLICDB2_PROJECT_ROOT"
PROJECT_ROOT = Path(os.getenv(PROJECT_ROOT_ENV, Path(__file__).resolve().parents[2])).resolve()
DATA_ROOT = PROJECT_ROOT / "data"
DATABASE_ROOT = DATA_ROOT / "db"
DATABASE_PATH = DATABASE_ROOT / "publicdb2.sqlite3"
RAW_ROOT = DATA_ROOT / "raw"
IMPORT_ROOT = DATA_ROOT / "imports"
EXPORT_ROOT = DATA_ROOT / "exports"
TEMP_ROOT = DATA_ROOT / "temp"
LOG_ROOT = PROJECT_ROOT / "logs"
BACKUP_ROOT = PROJECT_ROOT / "backups"
CONFIG_ROOT = PROJECT_ROOT / "config"
DEFAULT_DATABASE_PATH = DATABASE_PATH
DATABASE_URL_ENV = "PUBLICDB2_DATABASE_URL"
HTTP_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
PUBLICDB_USER_AGENT = "PublicDB2/0.1 (+https://publicdb.rhythmus.co.kr)"


@dataclass(frozen=True)
class RuntimePaths:
    project_root: Path
    data_root: Path
    database_root: Path
    database_path: Path
    raw_root: Path
    import_root: Path
    export_root: Path
    temp_root: Path
    log_root: Path
    backup_root: Path
    config_root: Path


def runtime_paths(project_root: Path | None = None) -> RuntimePaths:
    root = (project_root or PROJECT_ROOT).resolve()
    data = root / "data"
    return RuntimePaths(root, data, data / "db", data / "db" / "publicdb2.sqlite3", data / "raw", data / "imports", data / "exports", data / "temp", root / "logs", root / "backups", root / "config")


def ensure_runtime_directories(paths: RuntimePaths | None = None) -> RuntimePaths:
    selected = paths or runtime_paths()
    for path in (selected.database_root, selected.raw_root, selected.import_root, selected.export_root, selected.temp_root, selected.log_root, selected.backup_root, selected.config_root):
        path.mkdir(parents=True, exist_ok=True)
    return selected


def get_database_url() -> str:
    configured = os.getenv(DATABASE_URL_ENV)
    if configured:
        return configured
    return f"sqlite+pysqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"
