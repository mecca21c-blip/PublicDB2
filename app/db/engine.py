"""Centralized SQLAlchemy engine construction."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url

from app.core.config import get_database_url


def _prepare_sqlite_directory(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    url = database_url or get_database_url()
    _prepare_sqlite_directory(url)
    engine = create_engine(url, echo=echo)
    if engine.url.get_backend_name() == "sqlite":
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                database = engine.url.database
                if database and database != ":memory:":
                    cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()
    return engine
