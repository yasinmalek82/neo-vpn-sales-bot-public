#!/bin/sh
# PostgreSQL backup for the bot database (production).
#
# Usage (one-shot via compose profile):
#   cd docker && docker compose --profile backup run --rm backup
#
# Or mount and cron this script against a reachable Postgres host.
# Env: DB_HOST, DB_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD,
#      BACKUP_DIR, BACKUP_RETAIN_DAILY, BACKUP_RETAIN_WEEKLY

set -e
umask 077

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETAIN_DAILY="${BACKUP_RETAIN_DAILY:-7}"
RETAIN_WEEKLY="${BACKUP_RETAIN_WEEKLY:-4}"
DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-bot_db}"
DB_USER="${POSTGRES_USER:-bot_user}"
DB_PASS="${POSTGRES_PASSWORD:-bot_pass}"

DATE=$(date +"%Y%m%d_%H%M%S")
DAY_OF_WEEK=$(date +"%u")  # 1-7 (Mon-Sun)
IS_WEEKLY=0

if [ "$DAY_OF_WEEK" = "7" ]; then
    IS_WEEKLY=1
fi

mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"

# POSIX sh reports only the last command's pipeline status. Keep pg_dump out
# of a pipeline so a failed connection cannot publish an empty "good" backup.
# Staging under BACKUP_DIR also makes publication an atomic rename.
WORK_DIR=$(mktemp -d "$BACKUP_DIR/.backup.XXXXXX")
cleanup() {
    rm -f "$WORK_DIR/dump.sql" "$WORK_DIR/dump.sql.gz" "$WORK_DIR/weekly.sql.gz"
    rmdir "$WORK_DIR"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

echo "Starting backup at $(date)"

# Daily backup
DAILY_FILE="$BACKUP_DIR/daily/${DB_NAME}_${DATE}.sql.gz"
echo "Creating daily backup: $DAILY_FILE"
PGPASSWORD="$DB_PASS" pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" > "$WORK_DIR/dump.sql"
if [ ! -s "$WORK_DIR/dump.sql" ]; then
    echo "Backup failed: database dump is empty" >&2
    exit 1
fi
gzip -c "$WORK_DIR/dump.sql" > "$WORK_DIR/dump.sql.gz"
gzip -t "$WORK_DIR/dump.sql.gz"
mv "$WORK_DIR/dump.sql.gz" "$DAILY_FILE"

# Weekly backup (on Sundays)
if [ "$IS_WEEKLY" -eq 1 ]; then
    WEEKLY_FILE="$BACKUP_DIR/weekly/${DB_NAME}_weekly_${DATE}.sql.gz"
    echo "Creating weekly backup: $WEEKLY_FILE"
    cp "$DAILY_FILE" "$WORK_DIR/weekly.sql.gz"
    mv "$WORK_DIR/weekly.sql.gz" "$WEEKLY_FILE"
fi

# Cleanup old daily backups
echo "Cleaning up daily backups older than $RETAIN_DAILY days"
find "$BACKUP_DIR/daily" -name "*.sql.gz" -mtime +"$RETAIN_DAILY" -delete

# Cleanup old weekly backups
echo "Cleaning up weekly backups older than $RETAIN_WEEKLY weeks"
find "$BACKUP_DIR/weekly" -name "*.sql.gz" -mtime +$((RETAIN_WEEKLY * 7)) -delete

echo "Backup completed at $(date)"
echo "Daily backups:"
ls -lh "$BACKUP_DIR/daily" || true
echo "Weekly backups:"
ls -lh "$BACKUP_DIR/weekly" || true
