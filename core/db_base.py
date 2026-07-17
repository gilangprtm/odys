import os
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from sqlalchemy import event, create_engine, Column, DateTime, Text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.declarative import declarative_base, declared_attr
from sqlalchemy.orm import sessionmaker

from src.runtime_paths import get_app_root
from core.platform_compat import safe_chmod, IS_WINDOWS

logger = logging.getLogger(__name__)

Base = declarative_base()


def utcnow_naive() -> datetime:
    """Return naive UTC for existing DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    """Mixin that adds timestamp fields to models"""
    @declared_attr
    def created_at(cls):
        return Column(DateTime, default=utcnow_naive, nullable=False)

    @declared_attr
    def updated_at(cls):
        return Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)


# Ensure data dir exists before SQLite connects.
from src.constants import DATA_DIR
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)


def _default_database_url() -> str:
    return f"sqlite:///{Path(DATA_DIR) / 'app.db'}"


def _normalize_sqlite_url(url: str) -> str:
    try:
        parsed = make_url(url)
    except Exception:
        return url
    if parsed.get_backend_name() != "sqlite":
        return url
    db_path = parsed.database
    if (
        not db_path or db_path == ":memory:"
        or str(db_path).lower().startswith("file:")
        or os.path.isabs(str(db_path))
    ):
        return url
    absolute_path = (Path(get_app_root()) / str(db_path)).resolve().as_posix()
    return parsed.set(database=absolute_path).render_as_string(hide_password=False)


DATABASE_URL = _normalize_sqlite_url(os.getenv("DATABASE_URL", _default_database_url()))

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

_SQLITE_SIDECARS = ("-journal", "-wal", "-shm")


def _sqlite_db_path(url) -> Optional[str]:
    if url.get_backend_name() != "sqlite":
        return None
    db_path = url.database
    if not db_path or db_path == ":memory:":
        return None
    db_path = str(db_path)
    query = {
        str(key).lower(): str(value).strip().lower()
        for key, value in dict(getattr(url, "query", {}) or {}).items()
    }
    uri_enabled = query.get("uri") in {"1", "true", "yes", "on"}
    is_file_uri = db_path.lower().startswith("file:")
    if not uri_enabled or not is_file_uri:
        return db_path
    if db_path.lower().startswith("file::memory:") or query.get("mode") == "memory":
        return None
    parsed = urlparse(db_path)
    fs_path = parsed.path or ""
    if not fs_path or fs_path == ":memory:":
        return None
    authority = parsed.netloc
    if authority and authority.lower() != "localhost":
        fs_path = f"//{authority}{fs_path}"
    return unquote(fs_path)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class EncryptedText(TypeDecorator):
    """Text column transparently encrypted at rest via src.secret_storage."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from src.secret_storage import encrypt
        return encrypt(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from src.secret_storage import decrypt
        return decrypt(value)
