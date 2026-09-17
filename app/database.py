"""Database engine, session factory and schema bootstrap.

The same code path serves SQLite (default, zero-configuration) and PostgreSQL
(``postgresql+psycopg://...``) because only the connection string differs.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_directory(url: str) -> None:
    """Create the parent folder of a relative SQLite file so it can be opened."""
    if not url.startswith("sqlite"):
        return
    path_part = url.split("///", 1)[-1]
    if path_part in ("", ":memory:"):
        return
    target = Path(path_part)
    if not target.is_absolute():
        target = Path.cwd() / target
    target.parent.mkdir(parents=True, exist_ok=True)


_settings = get_settings()
_ensure_sqlite_directory(_settings.database_url)

_is_sqlite = _settings.database_url.startswith("sqlite")
engine = create_engine(
    _settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=not _is_sqlite,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    from . import models  # noqa: F401  (register mappers before create_all)

    Base.metadata.create_all(bind=engine)
