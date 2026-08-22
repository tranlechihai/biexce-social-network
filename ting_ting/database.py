"""Database engine, session factory, and schema initialization.

T-019: Alembic is the single source of truth for schema evolution on BOTH
SQLite and PostgreSQL.

SQLite startup semantics:
* fresh file (no ``users`` table)            -> ``alembic upgrade head``
* stamped (``alembic_version`` present)      -> ``alembic upgrade head``
  (no-op at head; applies pending migrations when behind), then verify head
* legacy unstamped create_all schema         -> strict structural check
  against head; if it matches -> ``alembic stamp head``; otherwise fail
  closed (no implicit ``create_all`` patching)

PostgreSQL startup semantics:
* fresh empty database (no tables)           -> ``alembic upgrade head``
* stamped (``alembic_version`` present)      -> ``alembic upgrade head``
  (no-op at head; applies pending migrations when behind), then verify head
* tables without a stamp                      -> fail closed (unmanaged DB)
In all PostgreSQL cases the app refuses to start unless the alembic_version
stamp equals the head revision (``_require_head``).
"""

import contextlib
import os
import re
from collections.abc import Generator
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import (
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from ting_ting.config import Settings
from ting_ting.models import Base


def enable_sqlite_runtime_pragmas(
    engine: Engine,
    enforce_foreign_keys: bool = True,
) -> None:
    """Apply the runtime PRAGMA set to *every* new SQLite connection.

    SQLite pragmas are per-connection unless documented otherwise, so a
    one-shot statement only covers a single connection.  This listener
    guarantees every pooled connection gets:

    * ``journal_mode=WAL`` — the WAL state is persisted in the database
      file, so the setting itself is cheap to re-assert; WAL lets readers
      proceed while a writer holds the write lock (no ``database is
      locked`` on the 1-worker + alembic/backup overlap we actually have).
      In-memory databases no-op back to memory journal.
    * ``synchronous=NORMAL`` — the recommended companion to WAL (full
      durability on commit, crash-safe with WAL);
    * ``busy_timeout=5000`` — brief writer/readers contention retries
      instead of failing immediately.
    * ``foreign_keys=ON`` (when ``enforce_foreign_keys``) — cascade deletes
      always work. **Migrations must pass ``enforce_foreign_keys=False``**:
      with it ON, a ``DROP TABLE`` on a parent cascades to every child table
      (SQLite treats DROP TABLE as DELETE of all rows), which silently wipes
      dependent rows during a table rebuild.  FK enforcement is an
      app-runtime concern, not a schema-change one.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        if hasattr(cursor, "execute"):
            if enforce_foreign_keys:
                cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            mode_row = cursor.fetchone()
            if mode_row and str(mode_row[0]).lower() == "wal":
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


# Backwards-compatible alias (pre-T-022 name).
enable_sqlite_foreign_keys = enable_sqlite_runtime_pragmas

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
# Alembic runtime (embedded) — the single schema authority
# ---------------------------------------------------------------------------

_ALEMBIC_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> AlembicConfig:
    """Alembic config resolved relative to the repo, not the CWD."""
    cfg = AlembicConfig(str(_ALEMBIC_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ALEMBIC_ROOT / "alembic"))
    return cfg


@contextlib.contextmanager
def _with_database_url(engine: Engine):
    """Point ``alembic/env.py`` at *this* engine's URL for the upgrade call.

    env.py reads ``TING_DATABASE_URL`` from the environment; overriding it
    here (and restoring afterwards) guarantees the embedded run touches the
    engine we are initializing — not whatever URL the process env holds.
    The password must NOT be hidden: env.py connects with this exact URL, and
    ``hide_password=True`` breaks any authenticated connection (PostgreSQL).
    The value is process-local (not logged) and restored before the block
    returns, so it never escapes the embedded alembic call.
    """
    url = engine.url.render_as_string(hide_password=False)
    old = os.environ.get("TING_DATABASE_URL")
    os.environ["TING_DATABASE_URL"] = url
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TING_DATABASE_URL", None)
        else:
            os.environ["TING_DATABASE_URL"] = old


def _alembic_head_revision() -> str:
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def _alembic_upgrade_head(engine: Engine) -> None:
    with _with_database_url(engine):
        command.upgrade(_alembic_config(), "head")


def _alembic_stamp_head(engine: Engine) -> None:
    with _with_database_url(engine):
        command.stamp(_alembic_config(), "head")


def _current_revision(engine: Engine) -> str | None:
    insp = inspect(engine)
    if "alembic_version" not in insp.get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()


def _require_head(engine: Engine) -> None:
    """Fail closed when the database is not at the head revision."""
    current = _current_revision(engine)
    head = _alembic_head_revision()
    if current != head:
        raise ValueError(
            f"Database schema is at revision {current!r} but head is {head!r}. "
            "Run 'alembic upgrade head'."
        )


# Structural expectations a legacy (pre-Alembic) create_all database must
# satisfy before we are allowed to stamp it at head. If any of these is
# missing the schema has drifted and must be fixed/rebuilt by an operator.
_LEGACY_REQUIRED_CHECK_CONSTRAINTS = (
    "ck_follow_not_self",
    "ck_activity_kind",
    "ck_friend_request_state",
    "ck_friend_request_not_self",
    "ck_friend_request_canonical",
    "ck_post_audience",
    "ck_mute_not_self",
    "ck_mute_exactly_one_target",
    "ck_report_reason",
    "ck_report_status",
)
_LEGACY_REQUIRED_FK_ACTIONS = (
    # (table, from_col, reftable, ondelete)
    ("posts", "author_id", "users", "CASCADE"),
    ("post_media", "post_id", "posts", "CASCADE"),
    ("likes", "post_id", "posts", "CASCADE"),
    ("comments", "post_id", "posts", "CASCADE"),
    ("comments", "parent_comment_id", "comments", "CASCADE"),
    ("saved_posts", "post_id", "posts", "CASCADE"),
    ("reposts", "post_id", "posts", "CASCADE"),
    ("activities", "post_id", "posts", "CASCADE"),
    ("post_mentions", "post_id", "posts", "CASCADE"),
    ("post_mentions", "mentioned_user_id", "users", "CASCADE"),
    ("post_hashtags", "post_id", "posts", "CASCADE"),
    ("notification_preferences", "user_id", "users", "CASCADE"),
    ("sessions", "user_id", "users", "CASCADE"),
    ("reports", "post_id", "posts", "SET NULL"),
    ("reports", "comment_id", "comments", "SET NULL"),
    # 0010 — a report outlives the accounts it references (evidence retention)
    ("reports", "reporter_id", "users", "SET NULL"),
    ("reports", "target_user_id", "users", "SET NULL"),
    ("reports", "resolved_by", "users", "SET NULL"),
)


def _legacy_schema_matches_head(engine: Engine) -> bool:
    """True when an unstamped SQLite schema is structurally == Alembic head.

    Checks: every mapped table with all its columns, the FK delete actions
    introduced by migrations 0002/0005/0006/0007, and the named check
    constraints. A single miss means drift — refuse to stamp.
    """
    insp = inspect(engine)
    try:
        existing_tables = set(insp.get_table_names())
        for table in Base.metadata.tables.values():
            if table.name not in existing_tables:
                return False
            have = {c["name"] for c in insp.get_columns(table.name)}
            want = {c.name for c in table.columns}
            if not want.issubset(have):
                return False

        with engine.connect() as conn:
            create_sql = {
                name: (sql or "")
                for name, sql in conn.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type='table'")
                )
            }

        for table, col, reftable, action in _LEGACY_REQUIRED_FK_ACTIONS:
            sql = create_sql.get(table)
            if sql is None:
                return False
            if _fk_delete_action(sql, col, reftable) != action:
                return False

        checks_ok = all(
            any(c in sql for sql in create_sql.values())
            for c in _LEGACY_REQUIRED_CHECK_CONSTRAINTS
        )
        return (
            checks_ok
            and _search_artifacts_present(engine)
            and _notification_artifacts_present(engine)
        )
    except Exception:
        # Introspection failure is drift from our point of view — fail closed.
        return False


def _fk_delete_action(create_sql: str, col: str, reftable: str) -> str | None:
    """ON DELETE action of ``FOREIGN KEY(col) REFERENCES reftable (...)`` in a
    CREATE TABLE statement (SQLite default: NO ACTION).

    The SQLite inspector in SQLAlchemy 2.0.x reports ``ondelete=None``, so the
    action is read from the persisted DDL instead.
    """
    pattern = (
        r"FOREIGN KEY\(\s*"
        + re.escape(col)
        + r"\s*\)\s*REFERENCES\s+"
        + re.escape(reftable)
        + r"\s*\(\s*\w+\s*\)"
        + r"(?:\s+ON\s+DELETE\s+(SET\s+NULL|SET\s+DEFAULT|CASCADE|RESTRICT|NO\s+ACTION))?"
    )
    match = re.search(pattern, create_sql, flags=re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).upper() if match.group(1) else "NO ACTION"


def _search_artifacts_present(engine: Engine) -> bool:
    """Migration-only native search structures required at head (T-026)."""
    try:
        with engine.connect() as conn:
            if engine.name == "sqlite":
                rows = conn.execute(text(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE name IN ('posts_fts','posts_fts_ai','posts_fts_ad','posts_fts_au')"
                )).all()
                return {(kind, name) for kind, name in rows} == {
                    ("table", "posts_fts"),
                    ("trigger", "posts_fts_ai"),
                    ("trigger", "posts_fts_ad"),
                    ("trigger", "posts_fts_au"),
                }
            if engine.name == "postgresql":
                return bool(conn.execute(text(
                    "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() "
                    "AND tablename = 'posts' AND indexname = 'ix_posts_search_fts'"
                )).scalar())
    except Exception:
        return False
    return False


def _require_search_artifacts(engine: Engine) -> None:
    if not _search_artifacts_present(engine):
        raise ValueError(
            "Database is stamped at head but native post-search artifacts are missing. "
            "Restore the T-026 FTS table/triggers or PostgreSQL GIN index before startup."
        )


def _notification_artifacts_present(engine: Engine) -> bool:
    required = {"ux_activities_unread_dedup", "ix_activities_user_created_id"}
    try:
        with engine.connect() as conn:
            if engine.name == "sqlite":
                names = {
                    row[0] for row in conn.execute(text(
                        "SELECT name FROM sqlite_master WHERE type='index' "
                        "AND tbl_name='activities'"
                    ))
                }
            elif engine.name == "postgresql":
                names = {
                    row[0] for row in conn.execute(text(
                        "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() "
                        "AND tablename='activities'"
                    ))
                }
            else:
                return False
        return required <= names
    except Exception:
        return False


def _require_notification_artifacts(engine: Engine) -> None:
    if not _notification_artifacts_present(engine):
        raise ValueError(
            "Database is stamped at head but T-027 notification dedup/order "
            "indexes are missing. Restore them before startup."
        )


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------

# Minimum expected columns on the 'users' table for compatibility.
_USERS_REQUIRED_COLUMNS: set[str] = {
    "id", "username", "email", "password_hash", "display_name", "bio",
}


def validate_and_initialize_schema(
    engine: Engine | None = None,
) -> Engine:
    """Validate the target and bring the schema to the Alembic head.

    Raises ``ValueError`` for:
    * non-SQLite/PostgreSQL backends,
    * incompatible existing 'users' columns,
    * stale or unknown revisions,
    * unstamped legacy SQLite schemas that do not structurally match head.
    """
    if engine is None:
        engine = get_engine()

    if engine.name not in {"sqlite", "postgresql"}:
        raise ValueError(
            f"Database backend must be sqlite or postgresql; got '{engine.name}'."
        )

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    if engine.name == "postgresql":
        # PostgreSQL: Alembic is the schema authority too, but the app never
        # auto-downgrades and never stamps an unmanaged database. Fresh empty
        # databases and stamped-but-behind databases get upgraded; anything
        # else (tables without an alembic_version stamp) is fail-closed.
        upgraded = False
        if "alembic_version" in existing_tables:
            _alembic_upgrade_head(engine)
            upgraded = True
        elif not existing_tables:
            _alembic_upgrade_head(engine)
            upgraded = True
        else:
            raise ValueError(
                "PostgreSQL database contains tables but no alembic_version "
                "stamp — it was not created by this application's migrations. "
                "Refusing to start; run 'alembic upgrade head' or point the "
                "app at the correct database."
            )
        # The table snapshot above predates the upgrade (a no-op when the DB
        # is already at head, but the full table list when it just ran);
        # re-inspect before deciding what is missing.
        if upgraded:
            existing_tables = set(inspect(engine).get_table_names())
        expected_tables = set(Base.metadata.tables)
        missing_tables = expected_tables - existing_tables
        if missing_tables:
            raise ValueError(
                "PostgreSQL schema is not at the required Alembic revision; "
                f"missing tables {sorted(missing_tables)!r}. Run 'alembic upgrade head'."
            )
        _require_head(engine)
        _require_search_artifacts(engine)
        _require_notification_artifacts(engine)
        return engine

    # --- SQLite: Alembic is the schema authority ---
    enable_sqlite_foreign_keys(engine)

    if "users" in existing_tables:
        existing_cols = {col["name"] for col in inspector.get_columns("users")}
        missing = _USERS_REQUIRED_COLUMNS - existing_cols
        if missing:
            raise ValueError(
                f"Incompatible existing 'users' table — expected columns "
                f"{_USERS_REQUIRED_COLUMNS!r} but missing {missing!r}. "
                "Will not mutate an incompatible database."
            )

    if "alembic_version" in existing_tables:
        # Stamped: apply pending migrations (no-op when already at head).
        _alembic_upgrade_head(engine)
    elif "users" in existing_tables:
        # Legacy create_all schema: stamp only when structurally == head.
        if not _legacy_schema_matches_head(engine):
            raise ValueError(
                "Existing SQLite schema is not Alembic-stamped and does not "
                "structurally match the head revision. Back up the database, "
                "then repair or rebuild it via Alembic — startup will not "
                "patch it implicitly."
            )
        _alembic_stamp_head(engine)
    else:
        # Fresh file (may contain unrelated extra tables, which survive).
        _alembic_upgrade_head(engine)

    if "users" not in inspect(engine).get_table_names():
        raise ValueError(
            "Schema initialization finished but the 'users' table is missing — "
            "refusing to start on an invalid database."
        )
    _require_head(engine)
    _require_search_artifacts(engine)
    _require_notification_artifacts(engine)
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
    """Initialize MVP tables on ``engine`` (create-only test fast-path).

    Test databases are built with ``create_all`` for speed; app startup still
    validates them through :func:`validate_and_initialize_schema` (which
    stamps them at head when structurally complete).
    """
    if engine.name == "sqlite":
        enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    return engine
