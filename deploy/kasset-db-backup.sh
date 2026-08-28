#!/bin/sh
# Daily logical backup of the kasset PostgreSQL container. Keeps 7 days.
set -eu
BACKUP_DIR=/root/backups/kasset-daily
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/kasset-$STAMP.dump.gz"
docker exec kasset-trader-db-1 pg_dump -U kasset -d kasset --format=custom \
  | gzip > "$OUT.tmp"
[ -s "$OUT.tmp" ] || { rm -f "$OUT.tmp"; echo "empty dump" >&2; exit 1; }
mv "$OUT.tmp" "$OUT"
find "$BACKUP_DIR" -name "kasset-*.dump.gz" -mtime +7 -delete
echo "backup ok: $OUT ($(stat -c %s "$OUT") bytes)"
