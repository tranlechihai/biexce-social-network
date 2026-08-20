#!/usr/bin/env bash
# Back up the Biexce Social database and uploaded media.
#
# Usage: scripts/backup.sh
# Output: backups/backup-<timestamp>.sqlite   (consistent snapshot)
#         backups/uploads-<timestamp>.tar.gz  (media files, if any)
# Retention: keeps the 10 newest of each, deletes older.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Database URL: env var wins, then .env
if [[ -z "${TING_DATABASE_URL:-}" && -f .env ]]; then
    line=$(grep -E '^TING_DATABASE_URL=' .env | tail -1 || true)
    [[ -n "$line" ]] && TING_DATABASE_URL="${line#TING_DATABASE_URL=}"
fi
DB_URL="${TING_DATABASE_URL:-}"

case "${DB_URL:-}" in
    postgresql*://*|postgres*://*)
        echo "PostgreSQL database detected — using pg_dump."
        command -v pg_dump >/dev/null || { echo "ERROR: pg_dump not found in PATH" >&2; exit 1; }
        # pg_dump does not understand SQLAlchemy driver suffixes — convert
        # postgresql+psycopg://... (or any +driver) to postgresql://...
        PG_SCHEME="${DB_URL%%://*}"
        PG_SCHEME="${PG_SCHEME%%+*}"
        PG_URL="${PG_SCHEME}://${DB_URL#*://}"
        TS="$(date +%Y%m%d-%H%M%S)"
        mkdir -p backups
        pg_dump "$PG_URL" -Fc -f "backups/backup-${TS}.dump"
        echo "Wrote backups/backup-${TS}.dump (custom format; restore with pg_restore -d <url> backup-${TS}.dump)"
        ;;
    *)
    DB_FILE=""
    case "${DB_URL#sqlite:///}" in
        /*) DB_FILE="${DB_URL#sqlite:///}" ;;
        *)  DB_FILE="$REPO/${DB_URL#sqlite:///}" ;;
    esac
    if [[ -z "$DB_FILE" || ! -f "$DB_FILE" ]]; then
        DB_FILE="$REPO/ting_ting.db"
    fi
    TS="$(date +%Y%m%d-%H%M%S)"
    mkdir -p backups
    "$REPO/.venv/bin/python" - "$DB_FILE" "backups/backup-${TS}.sqlite" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close(); dst.close()
print(f"Wrote {sys.argv[2]} (online-consistent snapshot)")
PY
esac

# Media files
if [[ -d uploads && -n "$(ls -A uploads 2>/dev/null)" ]]; then
    TS="$(date +%Y%m%d-%H%M%S)"
    tar -C "$REPO" -czf "backups/uploads-${TS}.tar.gz" uploads
    echo "Wrote backups/uploads-${TS}.tar.gz"
fi

# Retention: keep 10 newest per kind
for kind in sqlite dump tar.gz; do
    ls -1t backups/*."$kind" 2>/dev/null | tail -n +11 | xargs -r rm -f
done
echo "Backup complete."