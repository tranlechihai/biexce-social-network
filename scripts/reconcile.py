#!/usr/bin/env python3
"""Media/DB reconciliation (T-022).

Cross-checks stored media on disk against the database:

* ``missing``  — a DB row references a file that is gone from disk
                 (delivery will 404; data already lost, flagged).
* ``orphan``   — a file on disk is referenced by no DB row (harmless but
                 leaks storage; ``--fix`` deletes them).

Checked sources: ``post_media.path``, ``user_profiles.avatar_path`` and
``user_profiles.avatar_url`` (local ``/media/`` + ``/uploads/`` paths only —
external URLs are ignored).

Usage:
    .venv/bin/python scripts/reconcile.py                 # report only
    .venv/bin/python scripts/reconcile.py --fix           # also delete orphans
    .venv/bin/python scripts/reconcile.py --db-url sqlite:///./ting_ting.db

Exit codes: 0 = consistent (or orphans removed with --fix and no missing),
1 = missing files found (unrecoverable by this tool), 2 = usage/DB error.
"""

import argparse
import os
import sys
from pathlib import Path

DEFAULT_UPLOADS_DIR = Path(__file__).resolve().parent.parent / "uploads"
LOCAL_PREFIXES = ("/media/", "/uploads/")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db-url", default=None, help="SQLAlchemy URL (default: TING_DATABASE_URL or app settings)")
    parser.add_argument("--uploads-dir", default=str(DEFAULT_UPLOADS_DIR), help="Media storage directory")
    parser.add_argument("--fix", action="store_true", help="Delete orphan files (default: report only)")
    return parser.parse_args(argv)


def resolve_db_url(args) -> str:
    if args.db_url:
        return args.db_url
    env_url = os.environ.get("TING_DATABASE_URL")
    if env_url:
        return env_url
    from ting_ting.config import Settings  # deferred: keeps --help fast/offline
    return Settings().database_url


def local_filename(stored_path: str | None) -> str | None:
    if not stored_path or not stored_path.startswith(LOCAL_PREFIXES):
        return None
    name = stored_path.rsplit("/", 1)[-1]
    # Reject traversal-shaped values; reconcile only ever reports.
    return name or None


def gather_db_paths(db_url: str):
    """Return the set of local filenames referenced by the database."""
    from sqlalchemy import create_engine, select
    from ting_ting.models import PostMedia, UserProfile

    engine = create_engine(db_url)
    names: set[str] = set()
    with engine.connect() as conn:
        for (path,) in conn.execute(select(PostMedia.path)):
            name = local_filename(path)
            if name:
                names.add(name)
        for row in conn.execute(select(UserProfile.avatar_path, UserProfile.avatar_url)):
            for path in row:
                name = local_filename(path)
                if name:
                    names.add(name)
    engine.dispose()
    return names


def run(args) -> int:
    uploads_dir = Path(args.uploads_dir)
    if not uploads_dir.is_dir():
        print(f"uploads dir not found: {uploads_dir}", file=sys.stderr)
        return 2

    db_url = resolve_db_url(args)
    try:
        referenced = gather_db_paths(db_url)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(f"could not read database {db_url}: {exc}", file=sys.stderr)
        return 2

    disk_files = {p.name for p in uploads_dir.iterdir() if p.is_file()}

    missing = sorted(referenced - disk_files)
    orphans = sorted(disk_files - referenced)

    print(f"DB referenced files : {len(referenced)}")
    print(f"files on disk       : {len(disk_files)}")
    print(f"missing (DB->disk)  : {len(missing)}")
    for name in missing:
        print(f"  MISSING  {name}")
    print(f"orphans (disk->DB)  : {len(orphans)}")
    for name in orphans:
        print(f"  ORPHAN   {name}")

    removed = 0
    if args.fix:
        for name in orphans:
            (uploads_dir / name).unlink(missing_ok=True)
            removed += 1
        print(f"--fix: removed {removed} orphan file(s)")

    if missing:
        return 1
    if orphans and not args.fix:
        print("orphan files found — rerun with --fix to remove them")
        return 1
    print("reconcile OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
