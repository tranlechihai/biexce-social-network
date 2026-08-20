"""Integration tests for schema initialization — AC9.

Verify:
* Creating required structures on a new target
* Leaving a compatible initialized target unchanged
* Failing before mutation on incompatible schema
* No drop, truncate, or database replacement
"""

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from ting_ting.database import (
    _create_test_engine,
    _init_test_engine,
    validate_and_initialize_schema,
)
from ting_ting.models import User


# ---------------------------------------------------------------------------
# AC9: Schema initialization
# ---------------------------------------------------------------------------

class TestAC9SchemaInit:
    """Tests run against file-based SQLite to inspect schema changes."""

    def test_creates_on_fresh_target(self, tmp_path: Path):
        """A brand-new database gets all required tables created."""
        db_path = str(tmp_path / "fresh.db")
        engine = _create_test_engine(db_path)

        validate_and_initialize_schema(engine)

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "users" in tables

        cols = {c["name"] for c in inspector.get_columns("users")}
        expected = {"id", "username", "email", "password_hash", "display_name", "bio"}
        assert expected.issubset(cols)
        engine.dispose()

    def test_leaves_compatible_initialized_target_unchanged(self, tmp_path: Path):
        """An already-compatible database is not mutated or truncated."""
        db_path = str(tmp_path / "existing.db")
        engine = _create_test_engine(db_path)
        _init_test_engine(engine)

        # Insert a record before re-initialization
        from sqlalchemy.orm import sessionmaker
        sm = sessionmaker(bind=engine, expire_on_commit=False)
        with sm() as s:
            u = User(
                username="preserved",
                email="preserved@example.com",
                password_hash="$2b$12$fakehashfordata",
            )
            s.add(u)
            s.commit()

        # Re-run initialization (simulating restart)
        validate_and_initialize_schema(engine)

        # Existing record must survive
        with sm() as s:
            row = s.get(User, 1)
        assert row is not None
        assert row.username == "preserved"
        engine.dispose()

    def test_incompatible_schema_fails_before_mutation(self, tmp_path: Path):
        """Missing required columns → ValueError before any mutation."""
        db_path = str(tmp_path / "bad_schema.db")
        engine = _create_test_engine(db_path)

        # Create a 'users' table missing required columns
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.commit()

        with pytest.raises(ValueError, match="Incompatible"):
            validate_and_initialize_schema(engine)

        # Verify the bad table was NOT dropped or altered
        inspector = inspect(engine)
        assert "users" in set(inspector.get_table_names())
        cols = {c["name"] for c in inspector.get_columns("users")}
        assert cols == {"id", "name"}
        engine.dispose()

    def test_unsupported_backend_rejected(self):
        """A backend other than SQLite/PostgreSQL is rejected immediately."""
        # Avoid loading a real non-SQLite driver; create a minimal stub.
        class _MockEngine:
            name = "mysql"
        with pytest.raises(ValueError, match="sqlite or postgresql"):
            validate_and_initialize_schema(_MockEngine())

    def test_no_implicit_drop_or_truncate(self, tmp_path: Path):
        """Initialize on a DB that has an extra non-MVP table — extra table survives."""
        db_path = str(tmp_path / "extra_table.db")
        engine = _create_test_engine(db_path)

        # Create an unrelated table first
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE other_data (payload TEXT)"))
            conn.execute(text("INSERT INTO other_data VALUES ('preserve-me')"))
            conn.commit()

        validate_and_initialize_schema(engine)

        # Both tables should exist
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "users" in tables
        assert "other_data" in tables

        # Data in other_data should survive
        with engine.connect() as conn:
            result = conn.execute(text("SELECT payload FROM other_data")).fetchone()
        assert result[0] == "preserve-me"
        engine.dispose()

    def test_no_ddl_alteration_on_existing_target(self, tmp_path: Path):
        """Initialization must not execute any DROP/CREATE INDEX/COLUMN DDL
        on a compatible existing target — only PRAGMA and SELECT are allowed.

        Regression: previous _migrate_friend_request_constraint would DROP
        uq_pair_state and CREATE ix_active_pair on every startup.
        """
        db_path = str(tmp_path / "no_ddl.db")
        engine = _create_test_engine(db_path)
        _init_test_engine(engine)

        # Snapshot schema (table+index definitions) before init
        with engine.connect() as conn:
            before = {
                r[0]: r[1]
                for r in conn.execute(
                    text(
                        "SELECT type, sql FROM sqlite_master "
                        "WHERE type IN ('table', 'index') ORDER BY name"
                    )
                ).fetchall()
            }

        # Re-run init (simulating a server restart)
        validate_and_initialize_schema(engine)

        # Schema must be identical — no DDL was executed
        with engine.connect() as conn:
            after = {
                r[0]: r[1]
                for r in conn.execute(
                    text(
                        "SELECT type, sql FROM sqlite_master "
                        "WHERE type IN ('table', 'index') ORDER BY name"
                    )
                ).fetchall()
            }

        assert before == after, (
            "Schema changed by initialization — DDL was executed. "
            f"Difference:\nBefore keys: {sorted(before.keys())}\n"
            f"After keys:  {sorted(after.keys())}"
        )
        engine.dispose()
