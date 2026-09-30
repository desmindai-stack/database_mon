#!/usr/bin/env bash
# dbace-backup.service'in ExecStart'ı (docs/ONPREM_NATIVE.md §2) — Docker paketindeki
# "docker exec dbace-db pg_dump ..." elle örneğinin native karşılığı, ama zamanlanmış ve otomatik.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/dbace}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
PG_BINDIR="${PG_BINDIR:?PG_BINDIR ortam değişkeni tanımlı olmalı — install.sh bunu yazar}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$BACKUP_DIR/dbace-$STAMP.dump"

PGPASSWORD="$DBACE_DB_PASSWORD" "$PG_BINDIR/pg_dump" -h 127.0.0.1 -U dbace -Fc dbace > "$FILE"

find "$BACKUP_DIR" -maxdepth 1 -name 'dbace-*.dump' -mtime "+$RETENTION_DAYS" -delete

echo "yedek alındı: $FILE ($(du -h "$FILE" | cut -f1))"
