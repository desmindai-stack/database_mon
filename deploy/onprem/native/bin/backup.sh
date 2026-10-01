#!/usr/bin/env bash
# ExecStart for dbace-backup.service (docs/ONPREM_NATIVE.md section 2) - the native counterpart
# of the Docker package's manual "docker exec dbace-db pg_dump ..." example, but scheduled and
# automatic.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/dbace}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
PG_BINDIR="${PG_BINDIR:?PG_BINDIR must be set in the environment - install.sh writes it}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$BACKUP_DIR/dbace-$STAMP.dump"

PGPASSWORD="$DBACE_DB_PASSWORD" "$PG_BINDIR/pg_dump" -h 127.0.0.1 -U dbace -Fc dbace > "$FILE"

find "$BACKUP_DIR" -maxdepth 1 -name 'dbace-*.dump' -mtime "+$RETENTION_DAYS" -delete

echo "backup written: $FILE ($(du -h "$FILE" | cut -f1))"
