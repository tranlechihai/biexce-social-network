#!/usr/bin/env bash
# Restore a SQLite backup taken by scripts/backup.sh.
#
# Usage: scripts/restore_sqlite.sh <backup-file>
#
# Steps: verify the backup, stop the service, swap the file, verify the
# restored file, restart the service. Abort without touching the live DB
# if the backup fails verification.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

BACKUP="${1:?Usage: scripts/restore_sqlite.sh <backup-file>}"
[[ -f "$BACKUP" ]] || { echo "ERROR: backup file not found: $BACKUP" >&2; exit 1; }

DB_FILE="$REPO/ting_ting.db"
STAMP="$(date +%Y%m%d-%H%M%S)"

check_db() {
    "$REPO/.venv/bin/python" - "$1" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
ok, = con.execute("PRAGMA integrity_check").fetchone()
ver = con.execute("SELECT version_num FROM alembic_version").fetchone()
users = con.execute("SELECT COUNT(*) FROM users").fetchone()
con.close()
if ok != "ok":
    sys.exit(f"integrity_check failed: {ok}")
print(f"verified: integrity=ok, alembic={ver[0]}, users={users[0]}")
PY
}

echo "== Verifying backup =="
check_db "$BACKUP"

echo "== Backing up the current live DB (safety copy) =="
mkdir -p backups
cp -p "$DB_FILE" "backups/pre-restore-${STAMP}.sqlite"

echo "== Stopping service =="
systemctl --user stop biexce-social-user.service

echo "== Restoring =="
cp -p "$BACKUP" "$DB_FILE"
check_db "$DB_FILE"

echo "== Restarting service =="
systemctl --user restart biexce-social-user.service
sleep 2
systemctl --user is-active biexce-social-user.service
echo "Restore complete. Live DB is now the backup content."