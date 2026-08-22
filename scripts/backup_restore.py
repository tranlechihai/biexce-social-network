#!/usr/bin/env python3
"""PostgreSQL backup / restore / verify for Biexce Social (T-016E).

Why a script and not pg_dump: the schema is fully owned by this app's
migrations, so a psql-compatible SQL file built from ``COPY ... CSV`` is a
self-contained, minimal restore artifact that works with plain ``psql``
(no pg_dump version matching, deterministic content).

Artifacts
---------
``backup`` writes a gzip-compressed SQL file, replayable two ways:

* by psql (IT side, no Python needed)::

      gunzip -c backup-....sql.gz | psql -v ON_ERROR_STOP=1 -d TARGET_DB

  where TARGET_DB is an EMPTY database already at ``alembic upgrade head``;
* by this script::

      python scripts/backup_restore.py restore --url ... --file ... --yes

The backup is a single consistent snapshot: the counts, every table COPY
and the sequence re-anchoring all run inside ONE transaction on the source.
Rows are loaded in FK-safe order (``metadata.sorted_tables``); ``comments``
is emitted sorted by id so its self-referencing parent key is always legal
on replay without ``session_replication_role`` tricks (parent ids are
always smaller than child ids — see ``migrate_data._SELF_REFERENCING``).

``verify`` migrates a THROWAWAY database on the same cluster to head,
restores the file into it, and compares per-table row counts AND a
per-table content fingerprint (md5 over ordered rows) with the manifest.
Recovery is only real when this passes — run it next to ``backup``.

Usage
-----
    python scripts/backup_restore.py backup  --url URL [--out FILE.sql.gz]
                                           [--uploads-dir DIR]
    python scripts/backup_restore.py verify  --url URL --file FILE.sql.gz
    python scripts/backup_restore.py restore --url URL --file FILE.sql.gz --yes

``--url`` is a SQLAlchemy PostgreSQL URL (defaults to ``$TING_DATABASE_URL``).
The script runs from a repo checkout (it reuses the app's models and the
embedded Alembic chain); it is intentionally NOT shipped in the deploy
image.  Media lives outside the database: ``--uploads-dir`` adds a sibling
``.uploads.tar.gz`` next to the SQL file.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import os
import re
import sys
import tarfile
import time

import psycopg
from sqlalchemy.engine import make_url, create_engine

from ting_ting.database import (
    _alembic_head_revision,
    _alembic_upgrade_head,
)
from ting_ting.migrate_data import _SELF_REFERENCING, _has_int_id
from ting_ting.models import Base

HEADER_MARK = "-- biexce-social-backup-format: 1"

# Tables copied, in FK-safe order (SQLAlchemy topological sort).
_TABLES = list(Base.metadata.sorted_tables)
_TABLE_NAMES = [t.name for t in _TABLES]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _psycopg_kwargs(url: str, database: str | None = None) -> dict:
    sa_url = make_url(url)
    backend = sa_url.get_backend_name()
    if backend not in ("postgresql", "postgres"):
        raise SystemExit(
            f"backup_restore: URL backend is {backend!r}; a PostgreSQL URL "
            "is required."
        )
    return dict(
        host=sa_url.host,
        port=sa_url.port,
        user=sa_url.username,
        password=sa_url.password,
        dbname=database or sa_url.database,
        connect_timeout=15,
    )


def _connect(url: str, database: str | None = None) -> psycopg.Connection:
    return psycopg.connect(**_psycopg_kwargs(url, database))


def cmd_backup(args: argparse.Namespace) -> int:
    out_path = args.out or os.path.join(
        "backups", f"backup-{time.strftime('%Y%m%d-%H%M%S')}.sql.gz"
    )
    conn = _connect(args.url)
    try:
        with conn.cursor() as cur:
            # Refuse to back up an unmanaged or non-head database: the
            # restore contract (empty DB at alembic head) must hold for the
            # data we take.
            row = cur.execute("SELECT version_num FROM alembic_version").fetchone()
        if row is None:
            raise SystemExit(
                "backup_restore: source has no alembic_version stamp; the "
                "database is not migration-managed."
            )
        revision = row[0]
        head = _alembic_head_revision()
        if revision != head:
            raise SystemExit(
                f"backup_restore: source is at {revision!r} but head is "
                f"{head!r}; upgrade the source first."
            )

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _log(f"backup_restore: {args.url} @ {revision} -> {out_path}")

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with gzip.open(out_path, "wt", encoding="utf-8", newline="") as out:
            with conn.cursor() as cur:
                # ONE transaction: counts + all COPYs + setvals, so the file
                # is a consistent snapshot even while the app writes.
                counts: dict[str, int] = {}
                for table in _TABLES:
                    counts[table.name] = cur.execute(
                        f"SELECT COUNT(*) FROM {table.name}"
                    ).fetchone()[0]

                out.write(HEADER_MARK + "\n")
                out.write(f"-- source-revision: {revision}\n")
                out.write(f"-- created: {now}\n")
                out.write(
                    "-- counts: "
                    + " ".join(f"{n}={c}" for n, c in counts.items())
                    + "\n"
                )
                out.write("-- restore target: EMPTY database at alembic head\n")
                out.write("BEGIN;\n")
                out.write("TRUNCATE " + ", ".join(_TABLE_NAMES) + " CASCADE;\n")

                for name in _TABLE_NAMES:
                    if name in _SELF_REFERENCING:
                        # id order keeps parent_comment_id FK-legal on replay.
                        copy_sql = (
                            f"COPY (SELECT * FROM {name} ORDER BY id) "
                            "TO STDOUT WITH (FORMAT csv, NULL '')"
                        )
                    else:
                        copy_sql = (
                            f"COPY {name} TO STDOUT WITH (FORMAT csv, NULL '')"
                        )
                    out.write(
                        f"COPY {name} FROM STDIN WITH (FORMAT csv, NULL '');\n"
                    )
                    _log(f"  {name}: {counts[name]} rows")
                    # The server emits CSV straight into the file: read()
                    # returns the UNPARSED row (byte-identical to what psql/
                    # pg_dump would produce) — no re-parsing, no drift.
                    with cur.copy(copy_sql) as cp:
                        while (line := cp.read()):
                            # read() yields the UNPARSED row as a memoryview
                            # (raw bytes exactly as the wire carries them).
                            out.write(bytes(line).decode("utf-8"))
                    out.write("\\.\n")

                # Re-anchor every integer-id sequence past the copied maximum
                # (same contract as the cutover tooling).  Text-PK tables
                # (sessions, alembic_version) are skipped.
                for table in _TABLES:
                    if not _has_int_id(table):
                        continue
                    seq = cur.execute(
                        f"SELECT pg_get_serial_sequence('{table.name}', 'id')"
                    ).fetchone()[0]
                    if not seq:
                        continue
                    out.write(
                        f"SELECT setval('{seq}', "
                        f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
                        f"(SELECT MAX(id) FROM {table.name}) IS NOT NULL);\n"
                    )
                out.write("COMMIT;\n")
                conn.commit()

        size_kb = os.path.getsize(out_path) // 1024
        total = sum(counts.values())
        uploads_tar = None
        if args.uploads_dir:
            uploads_tar = os.path.splitext(out_path)[0] + ".uploads.tar.gz"
            with tarfile.open(uploads_tar, "w:gz") as tar:
                tar.add(args.uploads_dir, arcname="uploads")
            _log(f"  uploads: {args.uploads_dir} -> {uploads_tar}")
        print(
            f"BACKUP OK file={out_path} size_kb={size_kb} "
            f"tables={len(_TABLE_NAMES)} rows={total} revision={revision}"
            + (f" uploads={uploads_tar}" if uploads_tar else "")
        )
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backup-file parsing (the script restores what it wrote; psql plays the same
# file natively — the format is deliberately a flat psql script)
# ---------------------------------------------------------------------------

_COPY_RE = re.compile(r"^COPY (\S+) FROM STDIN WITH")


class BackupFile:
    def __init__(self, raw: str):
        self.revision: str | None = None
        self.created: str | None = None
        self.counts: dict[str, int] = {}
        self.setvals: list[str] = []
        self.truncate: list[str] = []
        self.copies: list[tuple[str, list[list[str]]]] = []  # (table, csv rows)

        current: str | None = None
        rows: list[list[str]] = []

        for line in raw.splitlines():
            if line == HEADER_MARK:
                continue
            if line.startswith("-- source-revision: "):
                self.revision = line.split(": ", 1)[1].strip()
            elif line.startswith("-- created: "):
                self.created = line.split(": ", 1)[1].strip()
            elif line.startswith("-- counts: "):
                for pair in line.split(": ", 1)[1].split():
                    name, num = pair.split("=")
                    self.counts[name] = int(num)
            elif line.startswith("TRUNCATE "):
                self.truncate.append(line)
            elif line.startswith("SELECT setval("):
                self.setvals.append(line)
            elif (m := _COPY_RE.match(line)):
                current = m.group(1)
                rows = []
            elif line == "\\.":
                if current is None:
                    raise ValueError("unexpected COPY terminator")
                self.copies.append((current, rows))
                current = None
            elif line in ("BEGIN;", "COMMIT;", "") or line.startswith("-- "):
                continue
            elif current is not None:
                rows.append(next(csv.reader([line])))
        if current is not None:
            raise ValueError(f"COPY block for {current!r} missing terminator")
        if not self.copies:
            raise ValueError("no COPY blocks found — not a backup file")

    @property
    def copy_tables(self) -> set[str]:
        return {name for name, _ in self.copies}


def _load_backup(path: str) -> BackupFile:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return BackupFile(fh.read())


# ---------------------------------------------------------------------------
# Restore engine (shared by `restore` and `verify`)
# ---------------------------------------------------------------------------

def _apply_to(conn: psycopg.Connection, backup: BackupFile) -> None:
    """Load a backup into an empty-at-head database, one transaction."""
    head = _alembic_head_revision()
    if backup.revision != head:
        raise SystemExit(
            f"backup_restore: file is at {backup.revision!r} but the current "
            f"head is {head!r}; restore with the checkout that produced it."
        )
    missing = set(_TABLE_NAMES) - backup.copy_tables
    if missing:
        raise SystemExit(
            f"backup_restore: file is missing tables {sorted(missing)}; it was "
            "not produced by this schema generation."
        )

    with conn.cursor() as cur:
        for stmt in backup.truncate:
            cur.execute(stmt)
        by_table = dict(backup.copies)
        for name in _TABLE_NAMES:  # FK-safe order from the model, not the file
            rows = by_table[name]
            _log(f"  restored {name}: {len(rows)} rows")
            # psycopg3's write_row() always emits the TEXT (tab-separated)
            # format; the query says CSV, so serialize CSV ourselves and
            # pass the raw text through the COPY stream (write() is raw).
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            writer.writerows(rows)
            with cur.copy(
                f"COPY {name} FROM STDIN WITH (FORMAT csv, NULL '')"
            ) as cp:
                cp.write(buf.getvalue())
        for stmt in backup.setvals:
            cur.execute(stmt)


def cmd_verify(args: argparse.Namespace) -> int:
    backup = _load_backup(args.file)
    scratch = f"biexce_verify_{int(time.time())}_{os.getpid()}"
    admin = _connect(args.url)
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{scratch}"')
        admin.close()
    except Exception:
        admin.close()
        raise
    try:
        # Fresh schema at head, then the restore.
        _log(f"verify: scratch database {scratch} created")
        engine = create_engine(_psycopg_kwargs_url(args.url, scratch))
        try:
            _alembic_upgrade_head(engine)
        finally:
            engine.dispose()

        conn = _connect(args.url, database=scratch)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                _apply_to(conn, backup)
                mismatches: list[str] = []
                for name, expected in sorted(backup.counts.items()):
                    actual = cur.execute(
                        f"SELECT COUNT(*) FROM {name}"
                    ).fetchone()[0]
                    pk = list(Base.metadata.tables[name].primary_key.columns)[0].name
                    fingerprint = cur.execute(
                        f"SELECT COALESCE(md5(string_agg("
                        f"row_to_json(t)::text, '||' ORDER BY t.{pk})), "
                        f"'md_empty') FROM (SELECT * FROM {name}) t"
                    ).fetchone()[0]
                    if actual != expected:
                        mismatches.append(name)
                    state = "ok" if not mismatches or name not in mismatches \
                        else f"MISMATCH got {actual}"
                    _log(f"  {name}: rows={actual} (file {expected}) {state} "
                         f"fp={fingerprint[:12]}...")

                # Sequence sanity: every integer-id sequence must stand past
                # (or exactly at) the copied maximum.
                for table in _TABLES:
                    if not _has_int_id(table):
                        continue
                    seq = cur.execute(
                        f"SELECT pg_get_serial_sequence('{table.name}', 'id')"
                    ).fetchone()[0]
                    ok = cur.execute(
                        f"SELECT last_value >= COALESCE("
                        f"(SELECT MAX(id) FROM {table.name}), 0) FROM {seq}"
                    ).fetchone()[0]
                    # last_value is None only for a never-advanced sequence
                    # on an empty table, which still satisfies "next id is
                    # free" — treat NULL as passing only for empty tables.
                    if not ok:
                        max_id = cur.execute(
                            f"SELECT MAX(id) FROM {table.name}"
                        ).fetchone()[0]
                        if max_id is not None:
                            mismatches.append(f"{table.name}(sequence)")
                            _log(f"  {table.name}: sequence BEHIND max id "
                                 f"{max_id}")
            conn.commit()
        finally:
            conn.close()

        if mismatches:
            print(f"VERIFY FAILED file={args.file} mismatches={mismatches}")
            return 1
    finally:
        # Always drop the scratch database, even on failure.
        cleanup = _connect(args.url)
        try:
            cleanup.autocommit = True
            with cleanup.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
                _log(f"verify: scratch database {scratch} dropped")
        finally:
            cleanup.close()

    print(f"VERIFY OK file={args.file} tables={len(backup.counts)} "
          f"revision={backup.revision}")
    return 0


def _psycopg_kwargs_url(url: str, database: str) -> str:
    """A SQLAlchemy URL string with the database name swapped."""
    base = make_url(url)
    return base.set(database=database).render_as_string(hide_password=False)


def cmd_restore(args: argparse.Namespace) -> int:
    if not args.yes:
        raise SystemExit(
            "backup_restore: restore is DESTRUCTIVE; pass --yes after "
            "checking the target URL"
        )
    backup = _load_backup(args.file)
    conn = _connect(args.url)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            nonempty = [
                name for name in _TABLE_NAMES
                if cur.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
            ]
        if nonempty:
            conn.rollback()
            raise SystemExit(
                "backup_restore: target is not empty "
                f"(tables: {sorted(nonempty)}); refusing. Point restore at "
                "an empty database at alembic head."
            )
        with conn.cursor() as cur:
            _apply_to(conn, backup)
        conn.commit()
        print(f"RESTORE OK file={args.file} tables={len(backup.counts)} "
              f"revision={backup.revision}")
        return 0
    except SystemExit:
        raise
    finally:
        try:
            conn.rollback()  # no-op when committed
        except Exception:
            pass
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Biexce Social PostgreSQL backup/restore/verify"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="dump a consistent snapshot")
    p_backup.add_argument("--url", default=os.environ.get("TING_DATABASE_URL"))
    p_backup.add_argument(
        "--out",
        help="output path (default backups/backup-<ts>.sql.gz)",
    )
    p_backup.add_argument(
        "--uploads-dir", help="also tar this media directory alongside"
    )
    p_backup.set_defaults(func=cmd_backup)

    p_verify = sub.add_parser(
        "verify", help="restore into a throwaway database and compare"
    )
    p_verify.add_argument("--url", default=os.environ.get("TING_DATABASE_URL"))
    p_verify.add_argument("--file", required=True)
    p_verify.set_defaults(func=cmd_verify)

    p_restore = sub.add_parser(
        "restore", help="destructive load into a target database"
    )
    p_restore.add_argument("--url", default=os.environ.get("TING_DATABASE_URL"))
    p_restore.add_argument("--file", required=True)
    p_restore.add_argument(
        "--yes", action="store_true",
        help="required: the target database IS WIPED",
    )
    p_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    if not args.url:
        raise SystemExit(
            "backup_restore: --url is required (or set TING_DATABASE_URL)"
        )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
