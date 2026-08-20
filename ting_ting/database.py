"""Database engine, session factory, and schema validation.

SQLite keeps the create-only development path. PostgreSQL schema changes are
owned by Alembic and application startup only validates their presence.
"""

from collections.abc import Generator

from sqlalchemy import (
    create_engine,
    event,
    inspect,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from ting_ting.config import Settings
from ting_ting.models import (
    Activity, Base, Block, Comment, Follow, FriendRequest, Like,
    Mute, Post, PostMedia, Report, Repost, SavedPost, UserProfile,
)


def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Enforce FK constraints on *every* new SQLite connection in the pool.

    SQLite ships with ``foreign_keys`` OFF per connection, so a one-shot
    PRAGMA only covers a single connection.  This listener guarantees all
    pooled connections cascade deletes correctly.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        if hasattr(cursor, "execute"):
            cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

# ---------------------------------------------------------------------------
# Engine / session
# ---------------------------------------------------------------------------

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = Settings()
        options = {"pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(settings.database_url, **options)
        if _engine.name == "sqlite":
            enable_sqlite_foreign_keys(_engine)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        engine = get_engine()
        _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return _SessionLocal


def get_db() -> Generator:
    """FastAPI dependency: yield a session and auto-close."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Schema initialization  —  create-only, never destructive
# ---------------------------------------------------------------------------

# Tables whose presence triggers the compatibility gate.  We only gate on
# ``users`` — adding new tables (friend_requests, blocks) for an existing,
# compatible target is safe because ``create_all`` is idempotent for missing
# tables.
REQUIRED_TABLES: set[str] = {"users"}

# Minimum expected columns on the 'users' table for compatibility.
_USERS_REQUIRED_COLUMNS: set[str] = {
    "id", "username", "email", "password_hash", "display_name", "bio",
}

# Tables introduced by t-002, t-003, and t-004 that must exist for full MVP functionality.
_SOCIAL_TABLES = [
    FriendRequest.__table__,
    Block.__table__,
    Post.__table__,
    Like.__table__,
    Comment.__table__,
    UserProfile.__table__,
    Follow.__table__,
    Activity.__table__,
    SavedPost.__table__,
    Repost.__table__,
    PostMedia.__table__,
    Mute.__table__,
    Report.__table__,
]


def validate_and_initialize_schema(
    engine: Engine | None = None,
) -> Engine:
    """Validate a supported target and initialize SQLite development schemas.

    Phases:
    PostgreSQL must already be upgraded with ``alembic upgrade head``. Startup
    never performs implicit production DDL.

    Raises ``ValueError`` for:
    * non-SQLite backends,
    * incompatible existing Ting Ting columns,
    """
    if engine is None:
        engine = get_engine()

    if engine.name not in {"sqlite", "postgresql"}:
        raise ValueError(
            f"Database backend must be sqlite or postgresql; got '{engine.name}'."
        )

    if engine.name == "sqlite":
        enable_sqlite_foreign_keys(engine)

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    if engine.name == "postgresql":
        expected_tables = set(Base.metadata.tables)
        missing_tables = expected_tables - existing_tables
        if missing_tables:
            raise ValueError(
                "PostgreSQL schema is not at the required Alembic revision; "
                f"missing tables {sorted(missing_tables)!r}. Run 'alembic upgrade head'."
            )

    # 3) Compatibility gate: if *any* required table already exists, the
    # ``users`` table must have all expected columns.
    if REQUIRED_TABLES & existing_tables:
        if "users" in existing_tables:
            existing_cols = {
                col["name"] for col in inspector.get_columns("users")
            }
            missing = _USERS_REQUIRED_COLUMNS - existing_cols
            if missing:
                raise ValueError(
                    f"Incompatible existing 'users' table — expected columns "
                    f"{_USERS_REQUIRED_COLUMNS!r} but missing {missing!r}. "
                    "Will not mutate an incompatible database."
                )
        if engine.name == "postgresql":
            return engine
        # Existing SQLite schema is compatible — create missing social/post tables only
        # (create_all is safe for tables that already exist).
        Base.metadata.create_all(engine, tables=_SOCIAL_TABLES)
        return engine

    # No required SQLite table exists — create all development tables.
    Base.metadata.create_all(engine)
    return engine


# ---------------------------------------------------------------------------
# Test helpers (imported by conftest)
# ---------------------------------------------------------------------------

def _create_test_engine(db_path: str) -> Engine:
    """Create a fresh engine bound to the given file path."""
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    enable_sqlite_foreign_keys(engine)
    return engine


def _init_test_engine(engine: Engine) -> Engine:
    """Initialize MVP tables on ``engine`` (create-only)."""
    if engine.name == "sqlite":
        enable_sqlite_foreign_keys(engine)
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    if not (REQUIRED_TABLES & existing):
        Base.metadata.create_all(engine)
    else:
        # Compatible existing target — add any missing social tables.
        Base.metadata.create_all(engine, tables=_SOCIAL_TABLES)
    return engine
