#!/bin/sh
# Restore the kasset PostgreSQL database from a kasset-db-backup dump.
# Usage: kasset-db-restore.sh /root/backups/kasset-daily/kasset-<STAMP>.dump.gz
set -eu
DUMP=${1:?usage: kasset-db-restore.sh <dump.gz>}
[ -f "$DUMP" ] || { echo "no such dump: $DUMP" >&2; exit 1; }
echo "This will DROP and recreate the kasset schema from $DUMP."
printf "Type RESTORE to continue: "
read -r answer
[ "$answer" = "RESTORE" ] || { echo "aborted"; exit 1; }
cd /opt/kasset-trader-core
docker compose --env-file .env.kasset -f docker-compose.kasset.yml stop api worker scheduler mcp
gunzip -c "$DUMP" | docker exec -i kasset-trader-db-1 pg_restore \
  -U kasset -d kasset --clean --if-exists --no-owner
docker compose --env-file .env.kasset -f docker-compose.kasset.yml up -d api worker scheduler mcp
echo "restore complete"
