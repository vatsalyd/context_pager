#!/usr/bin/env bash
# Nightly consistent SQLite backups for the relay, 7-day retention, optional S3.
# Installed as a cron job by setup_relay.sh.
set -euo pipefail

DB_DIR="${PAGER_DB_DIR:-/etc/context-pager}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/context-pager}"
RETENTION_DAYS="${PAGER_BACKUP_RETENTION_DAYS:-7}"
S3_BUCKET="${PAGER_S3_BUCKET:-}"
AWS_BIN="${PAGER_AWS_BIN:-aws}"

[[ -f /etc/context-pager/backup.env ]] && . /etc/context-pager/backup.env

mkdir -p "$BACKUP_DIR"

for db in users.db; do
    if [[ ! -f "$DB_DIR/$db" ]]; then
        continue
    fi
    # sqlite3 .backup produces a consistent copy even while the relay is writing.
    sqlite3 "$DB_DIR/$db" ".backup '$BACKUP_DIR/$db.$(date +%F).bak'"
done

# Retention: keep the last N days, always keep one file per day.
find "$BACKUP_DIR" -name '*.bak' -mtime "+$RETENTION_DAYS" -delete

if [[ -n "$S3_BUCKET" ]]; then
    "$AWS_BIN" s3 sync "$BACKUP_DIR" "s3://$S3_BUCKET/context-pager/" --delete --quiet
fi
