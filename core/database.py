"""
core/database.py — backward-compatible re-export shim.

4-way split:
  core/db_base.py       — engine, Base, EncryptedText, SessionLocal, URL utils
  core/db_models.py     — all SQLAlchemy model classes
  core/db_migrations.py — migration functions, init_db(), query helpers
  core/database.py      — re-exports everything (this file)

All existing `from core.database import ...` continue to work unchanged.
"""

from core.db_base import (
    Base, engine, SessionLocal, DATABASE_URL,
    utcnow_naive, TimestampMixin, EncryptedText,
    _sqlite_db_path, _SQLITE_SIDECARS, set_sqlite_pragma,
)

from core.db_models import (
    Session, ChatMessage, Document, DocumentVersion,
    GalleryAlbum, GalleryImage, EmailAccount,
    ModelEndpoint, ProviderAuthSession, McpServer,
    Comparison, Signature, ApiToken, Webhook,
    UserTool, UserToolData, CrewMember, ScheduledTask,
    EditorDraft, TaskRun, Memory, Note,
    CalendarCal, CalendarEvent, CalendarDeletedEvent, Integration,
)

from core.db_migrations import (
    init_db,
    get_db, get_db_session, get_session_mode, set_session_mode,
    get_session_by_id, get_upcoming_events, get_session_stats,
    get_detailed_stats, update_session_last_accessed,
    archive_session, bulk_insert_messages, cleanup_old_sessions,
)

# Run migrations at import time (preserves original side-effect behaviour).
init_db()
