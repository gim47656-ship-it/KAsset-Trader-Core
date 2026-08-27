#!/bin/sh
set -eu

umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$ROOT_DIR/deploy/kasset/compose.yaml"
ENV_FILE=${KASSET_COMPOSE_ENV_FILE:-"$ROOT_DIR/deploy/kasset/.env"}
BACKUP_DIR=
FORCE_OVERWRITE=false

usage() {
    cat <<'EOF'
Usage: kasset_restore.sh --backup DIRECTORY [--env-file PATH] [--force-overwrite]

Verifies every SHA-256 checksum, checks the archive/target database identity,
refuses a non-empty target by default, restores the PostgreSQL custom archive,
runs the one-shot Alembic migration, starts the stack, and runs the health smoke.

Safe path on a fresh Linux host:
  1. Configure deploy/kasset/.env and its external secret files.
  2. Ensure the selected KASSET_PROJECT_NAME points to a new/empty database volume.
  3. Run:
       scripts/kasset_restore.sh --env-file deploy/kasset/.env \
         --backup ./backups/<UTC stamp>

--force-overwrite is destructive: application processes are stopped and the
configured target database is dropped and recreated before restore. It must be
spelled explicitly and should only follow a separate current backup.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --backup)
            [ "$#" -ge 2 ] || { echo "--backup requires a directory" >&2; exit 2; }
            BACKUP_DIR=$2
            shift 2
            ;;
        --env-file)
            [ "$#" -ge 2 ] || { echo "--env-file requires a path" >&2; exit 2; }
            ENV_FILE=$2
            shift 2
            ;;
        --force-overwrite)
            FORCE_OVERWRITE=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[ -n "$BACKUP_DIR" ] || { echo "--backup is required" >&2; usage >&2; exit 2; }
[ -d "$BACKUP_DIR" ] || { echo "Backup directory not found: $BACKUP_DIR" >&2; exit 1; }
[ -r "$BACKUP_DIR/SHA256SUMS" ] || { echo "Missing SHA256SUMS" >&2; exit 1; }
[ -r "$BACKUP_DIR/manifest.tsv" ] || { echo "Missing manifest.tsv" >&2; exit 1; }
[ -r "$BACKUP_DIR/database.dump" ] || { echo "Missing database.dump" >&2; exit 1; }
[ -r "$ENV_FILE" ] || { echo "Compose env file is not readable: $ENV_FILE" >&2; exit 1; }
[ -r "$COMPOSE_FILE" ] || { echo "Compose file is not readable: $COMPOSE_FILE" >&2; exit 1; }

for command_name in docker sha256sum awk tr; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "Required command not found: $command_name" >&2
        exit 1
    }
done

docker compose version >/dev/null

compose() {
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

(
    cd "$BACKUP_DIR"
    sha256sum -c SHA256SUMS
)

backup_format=$(awk -F '\t' '$1 == "format" { print $2; exit }' "$BACKUP_DIR/manifest.tsv" | tr -d '\r')
backup_database=$(awk -F '\t' '$1 == "database_name" { print $2; exit }' "$BACKUP_DIR/manifest.tsv" | tr -d '\r')
backup_archive_sha=$(awk -F '\t' '$1 == "archive_sha256" { print $2; exit }' "$BACKUP_DIR/manifest.tsv" | tr -d '\r')
actual_archive_sha=$(sha256sum "$BACKUP_DIR/database.dump" | awk '{print $1}')

[ "$backup_format" = "kasset-postgresql-custom-v1" ] || {
    echo "Unsupported backup format: $backup_format" >&2
    exit 1
}
[ -n "$backup_database" ] || { echo "Manifest database_name is empty" >&2; exit 1; }
[ "$backup_archive_sha" = "$actual_archive_sha" ] || {
    echo "Archive checksum does not match manifest.tsv" >&2
    exit 1
}

compose up -d --wait postgres redis

target_database=$(compose exec -T postgres sh -ec 'printf %s "$POSTGRES_DB"' | tr -d '\r')
[ "$backup_database" = "$target_database" ] || {
    echo "Backup database '$backup_database' does not match target '$target_database'" >&2
    exit 1
}

relation_count=$(compose exec -T postgres sh -ec '
    exec psql -X -v ON_ERROR_STOP=1 -At \
      --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
      --command "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('\''pg_catalog'\'', '\''information_schema'\'') AND n.nspname NOT LIKE '\''pg_toast%'\'' AND c.relkind IN ('\''r'\'', '\''p'\'', '\''v'\'', '\''m'\'', '\''S'\'');"
' | tr -d '\r[:space:]')

case "$relation_count" in
    ''|*[!0-9]*)
        echo "Could not determine whether the target database is empty" >&2
        exit 1
        ;;
esac

if [ "$relation_count" -ne 0 ] && [ "$FORCE_OVERWRITE" != true ]; then
    echo "Refusing restore: target database contains $relation_count user relation(s)." >&2
    echo "Restore to an empty database, or take a current backup and repeat with --force-overwrite." >&2
    exit 1
fi

if [ "$FORCE_OVERWRITE" = true ]; then
    echo "Explicit --force-overwrite received; stopping application processes." >&2
    compose stop caddy api worker scheduler migration
    compose exec -T postgres sh -ec '
        dropdb --force --if-exists --username "$POSTGRES_USER" "$POSTGRES_DB"
        createdb --username "$POSTGRES_USER" --owner "$POSTGRES_USER" "$POSTGRES_DB"
    '
fi

compose exec -T postgres sh -ec '
    exec pg_restore --exit-on-error --no-owner --no-acl \
      --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
' <"$BACKUP_DIR/database.dump"

# The archive may be older than the immutable application image. Always converge
# the restored schema before any API, worker, or scheduler is started.
compose run --rm --no-deps migration
compose up -d --wait api worker scheduler caddy

"$SCRIPT_DIR/kasset_smoke.sh" --env-file "$ENV_FILE"

printf 'Restore, alembic upgrade, and health smoke completed for database %s.\n' "$target_database"
