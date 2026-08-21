"""One-time, fail-closed SQLite to PostgreSQL data transfer.

Guarantees (fail-closed, nothing partial is applied):

* the source is a real application database: stamped sources must be at the
  current alembic head; every source table that exists must carry every
  column the current schema expects (drift is refused, not silently skipped);
* the target is a migrated (alembic head) and completely empty PostgreSQL
  database — a non-empty target means someone already ran a copy;
* primary keys and row values are preserved;
* per-table row counts AND max(id) are verified after the copy, inside the
  same transaction as the copy itself (verification failure rolls back);
* on PostgreSQL, every id sequence is re-anchored past MAX(id) afterwards,
  so the first post-migration insert cannot collide with a copied ID;
* rows that satisfy a self-referencing foreign key (comments -> parent
  comment) are inserted with parents first.
"""

import argparse
import os
import sys

from sqlalchemy import Integer, create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine

from ting_ting.models import Base

# Tables whose rows reference other rows of the same table through a
# self-referencing foreign key. Parent ids are always smaller than child ids
# (a child row cannot exist before its parent), so an id sort guarantees
# FK-legal insert order.
_SELF_REFERENCING = {"comments"}


def _has_int_id(table) -> bool:
    """True for a single-column, INTEGER-typed id primary key.

    Not every table's PK is a serial integer — ``sessions.id`` is a VARCHAR
    uuid — so sequence re-anchoring and max(id) checks must be limited to
    integer ids (``setval`` on a text column is a type error on PostgreSQL).
    """
    columns = list(table.primary_key.columns) if table.primary_key else []
    return (
        len(columns) == 1
        and columns[0].name == "id"
        and isinstance(columns[0].type, Integer)
    )


def _verify_source(source: Engine) -> None:
    """Refuse to copy from a database the application could not run on."""
    inspector = inspect(source)
    source_tables = {
        name: {c["name"] for c in inspector.get_columns(name)}
        for name in set(inspector.get_table_names()) & set(Base.metadata.tables)
    }

    # Stamped source: must be at the head the current ORM expects. A stamped
    # source behind head means its schema (and possibly its data) is older
    # than what the app writes — refuse; the operator must upgrade the
    # source first (alembic upgrade head on the SQLite file).
    if "alembic_version" in inspector.get_table_names():
        with source.connect() as conn:
            current = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            from ting_ting.database import _alembic_head_revision
            head = _alembic_head_revision()
            if current != head:
                raise ValueError(
                    f"Source database is at alembic revision {current!r} but "
                    f"the application expects {head!r}. Run "
                    "alembic upgrade head against the SQLite source first."
                )

    # Existing tables must be structurally complete (covers both unstamped
    # legacy sources and stamped ones — a cheap double-check).
    for name, columns in source_tables.items():
        expected = {c.name for c in Base.metadata.tables[name].columns}
        missing = expected - columns
        if missing:
            raise ValueError(
                f"Source table {name!r} is missing columns {sorted(missing)!r}; "
                "the schema has drifted from the application. Refusing to copy."
            )

    # Foreign-key integrity: PostgreSQL enforces every FK on every row, so a
    # source containing orphan rows (e.g. left behind by maintenance done
    # with SQLite's default foreign_keys=OFF) would deep-fail mid-copy. The
    # app runtime enforces FKs, but manual/dev database surgery often does
    # not — check explicitly and fail with an actionable message.
    with source.connect() as conn:
        violations = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        # foreign_key_check rows: (table, rowid, referenced_table, fk_id)
        shown = "; ".join(
            f"{r[0]}(rowid={r[1]}) -> missing row in {r[2]}"
            for r in violations[:10]
        )
        raise ValueError(
            f"Source database has {len(violations)} foreign-key violation(s) "
            f"(orphan rows): {shown}. Clean up the source first (back it up), "
            "then retry. Aborted before any data was copied."
        )


def copy_database(
    source: Engine,
    target: Engine,
    *,
    require_postgresql: bool = True,
) -> dict[str, int]:
    """Copy all application tables atomically while preserving primary keys."""
    if source.name != "sqlite":
        raise ValueError("Source database must be SQLite.")
    if require_postgresql and target.name != "postgresql":
        raise ValueError("Target database must be PostgreSQL.")

    _verify_source(source)

    source_tables = set(inspect(source).get_table_names())
    target_tables = set(inspect(target).get_table_names())
    required_target = set(Base.metadata.tables)
    missing_target = required_target - target_tables
    if missing_target:
        raise ValueError(
            f"Target schema is missing tables {sorted(missing_target)!r}; "
            "run 'alembic upgrade head' first."
        )

    with target.connect() as connection:
        nonempty = [
            table.name
            for table in Base.metadata.sorted_tables
            if connection.scalar(select(func.count()).select_from(table))
        ]
    if nonempty:
        raise ValueError(
            f"Target database is not empty (tables: {sorted(nonempty)!r}); no data copied."
        )

    copied: dict[str, int] = {}
    copied_max: dict[str, int | None] = {}
    with source.connect() as source_connection, target.begin() as target_connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in source_tables:
                copied[table.name] = 0
                copied_max[table.name] = None
                continue
            rows = [dict(row._mapping) for row in source_connection.execute(select(table))]
            if table.name in _SELF_REFERENCING:
                rows.sort(key=lambda row: row["id"])
            if rows:
                target_connection.execute(table.insert(), rows)
            copied[table.name] = len(rows)
            copied_max[table.name] = (
                max(row["id"] for row in rows) if rows and _has_int_id(table) else None
            )

        for table in Base.metadata.sorted_tables:
            actual = target_connection.scalar(select(func.count()).select_from(table))
            if actual != copied[table.name]:
                raise RuntimeError(
                    f"Verification failed for table '{table.name}': "
                    f"expected {copied[table.name]}, got {actual}."
                )
            if copied_max[table.name] is not None:
                actual_max = target_connection.scalar(func.max(table.c.id))
                if actual_max != copied_max[table.name]:
                    raise RuntimeError(
                        f"Verification failed for table '{table.name}': "
                        f"max(id) expected {copied_max[table.name]}, got {actual_max}."
                    )

        if target.name == "postgresql":
            for table in Base.metadata.sorted_tables:
                if not _has_int_id(table):
                    continue
                # Table names are trusted constants from SQLAlchemy metadata.
                target_connection.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
                    f"EXISTS (SELECT 1 FROM {table.name}))"
                ))

    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Biexce Social SQLite data to PostgreSQL.")
    parser.add_argument(
        "--source",
        default="sqlite:///./ting_ting.db",
        help="SQLite source URL (default: sqlite:///./ting_ting.db)",
    )
    args = parser.parse_args()
    target_url = os.environ.get("TING_DATABASE_URL", "")
    if not target_url:
        print("ERROR: TING_DATABASE_URL must contain the PostgreSQL target URL.", file=sys.stderr)
        raise SystemExit(2)

    source = create_engine(args.source)
    target = create_engine(target_url, pool_pre_ping=True)
    try:
        copied = copy_database(source, target)
    except Exception as exc:
        print(f"ERROR: migration aborted: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        source.dispose()
        target.dispose()

    print("Migration completed and verified.")
    for table, count in copied.items():
        print(f"  {table}: {count}")


if __name__ == "__main__":
    main()
