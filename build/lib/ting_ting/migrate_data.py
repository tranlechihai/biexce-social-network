"""One-time, fail-closed SQLite to PostgreSQL data transfer."""

import argparse
import os
import sys

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine

from ting_ting.models import Base


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
    with source.connect() as source_connection, target.begin() as target_connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in source_tables:
                copied[table.name] = 0
                continue
            rows = [dict(row._mapping) for row in source_connection.execute(select(table))]
            if rows:
                target_connection.execute(table.insert(), rows)
            copied[table.name] = len(rows)

        for table in Base.metadata.sorted_tables:
            actual = target_connection.scalar(select(func.count()).select_from(table))
            if actual != copied[table.name]:
                raise RuntimeError(
                    f"Verification failed for table '{table.name}': "
                    f"expected {copied[table.name]}, got {actual}."
                )

        if target.name == "postgresql":
            for table in Base.metadata.sorted_tables:
                if "id" not in table.c or not table.c.id.primary_key:
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
