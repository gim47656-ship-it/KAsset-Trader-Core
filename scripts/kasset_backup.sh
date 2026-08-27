#!/bin/sh
set -eu

umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$ROOT_DIR/deploy/kasset/compose.yaml"
ENV_FILE=${KASSET_COMPOSE_ENV_FILE:-"$ROOT_DIR/deploy/kasset/.env"}
OUTPUT_ROOT=${KASSET_BACKUP_DIR:-"$ROOT_DIR/backups"}

usage() {
    cat <<'EOF'
Usage: kasset_backup.sh [--env-file PATH] [--output DIRECTORY]

Creates a PostgreSQL custom-format dump, SHA-256 checksum set, manifest,
resolved stack configuration, and service/image/volume/config/secret inventory.
No credential value is printed or copied; Compose secret definitions contain
only the configured source paths.

Fresh Linux host:
  1. Install Docker Engine and the Docker Compose plugin.
  2. Configure deploy/kasset/.env and the external 0600 secret files described
     by deploy/kasset/env.example.
  3. Start the database or full stack, then run:
       scripts/kasset_backup.sh --env-file deploy/kasset/.env --output ./backups
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --env-file)
            [ "$#" -ge 2 ] || { echo "--env-file requires a path" >&2; exit 2; }
            ENV_FILE=$2
            shift 2
            ;;
        --output)
            [ "$#" -ge 2 ] || { echo "--output requires a directory" >&2; exit 2; }
            OUTPUT_ROOT=$2
            shift 2
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

[ -r "$ENV_FILE" ] || { echo "Compose env file is not readable: $ENV_FILE" >&2; exit 1; }
[ -r "$COMPOSE_FILE" ] || { echo "Compose file is not readable: $COMPOSE_FILE" >&2; exit 1; }

for command_name in docker sha256sum awk date mkdir chmod; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "Required command not found: $command_name" >&2
        exit 1
    }
done

docker compose version >/dev/null

compose() {
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

section_names() {
    section=$1
    file=$2
    awk -v wanted="$section" '
        $0 == wanted ":" { inside = 1; next }
        inside && /^[^[:space:]]/ { exit }
        inside && /^  [^[:space:]][^:]*:/ {
            line = $0
            sub(/^  /, "", line)
            sub(/:.*/, "", line)
            print line
        }
    ' "$file"
}

compose up -d --wait postgres

timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
backup_dir="$OUTPUT_ROOT/$timestamp"
if [ -e "$backup_dir" ]; then
    echo "Refusing to overwrite an existing backup directory: $backup_dir" >&2
    exit 1
fi
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
dump_file="$backup_dir/database.dump"
compose exec -T postgres sh -ec '
    exec pg_dump \
      --username "$POSTGRES_USER" \
      --dbname "$POSTGRES_DB" \
      --format custom \
      --compress 9 \
      --no-owner \
      --no-acl
' >"$dump_file"

[ -s "$dump_file" ] || { echo "pg_dump produced an empty archive" >&2; exit 1; }

dump_sha=$(sha256sum "$dump_file" | awk '{print $1}')
compose config >"$backup_dir/stack-config.yaml"
compose config --services >"$backup_dir/services.txt"
compose config --images >"$backup_dir/images.txt"
compose config --volumes >"$backup_dir/logical-volumes.txt"

project_name=$(awk '$1 == "name:" { print $2; exit }' "$backup_dir/stack-config.yaml")
[ -n "$project_name" ] || project_name=kasset

docker volume ls \
    --filter "label=com.docker.compose.project=$project_name" \
    --format '{{.Name}}' >"$backup_dir/docker-volumes.txt"

{
    printf '%s\n' '[services]'
    cat "$backup_dir/services.txt"
    printf '\n%s\n' '[images]'
    cat "$backup_dir/images.txt"
    printf '\n%s\n' '[logical_volumes]'
    logical_volumes=$(section_names volumes "$backup_dir/stack-config.yaml")
    if [ -n "$logical_volumes" ]; then printf '%s\n' "$logical_volumes"; else printf '%s\n' '(none)'; fi
    printf '\n%s\n' '[configs]'
    configs=$(section_names configs "$backup_dir/stack-config.yaml")
    if [ -n "$configs" ]; then printf '%s\n' "$configs"; else printf '%s\n' '(none)'; fi
    printf '\n%s\n' '[secrets]'
    secrets=$(section_names secrets "$backup_dir/stack-config.yaml")
    if [ -n "$secrets" ]; then printf '%s\n' "$secrets"; else printf '%s\n' '(none)'; fi
    printf '\n%s\n' '[docker_volumes]'
    if [ -s "$backup_dir/docker-volumes.txt" ]; then
        cat "$backup_dir/docker-volumes.txt"
    else
        printf '%s\n' '(none)'
    fi
} >"$backup_dir/inventory.txt"

postgres_version=$(compose exec -T postgres postgres --version | tr -d '\r')
database_name=$(compose exec -T postgres sh -ec 'printf %s "$POSTGRES_DB"' | tr -d '\r')
database_user=$(compose exec -T postgres sh -ec 'printf %s "$POSTGRES_USER"' | tr -d '\r')
{
    printf 'format\t%s\n' 'kasset-postgresql-custom-v1'
    printf 'created_utc\t%s\n' "$timestamp"
    printf 'compose_project\t%s\n' "$project_name"
    printf 'database_name\t%s\n' "$database_name"
    printf 'database_user\t%s\n' "$database_user"
    printf 'archive\t%s\n' 'database.dump'
    printf 'archive_sha256\t%s\n' "$dump_sha"
    printf 'postgres_version\t%s\n' "$postgres_version"
    printf 'migration_after_restore\t%s\n' 'alembic upgrade head'
    printf 'smoke_after_restore\t%s\n' 'scripts/kasset_smoke.sh'
} >"$backup_dir/manifest.tsv"

(
    cd "$backup_dir"
    sha256sum \
        database.dump \
        manifest.tsv \
        stack-config.yaml \
        services.txt \
        images.txt \
        logical-volumes.txt \
        docker-volumes.txt \
        inventory.txt >SHA256SUMS
)

printf 'Backup created: %s\n' "$backup_dir"
printf 'Archive SHA-256: %s\n' "$dump_sha"
printf 'Restore only to an empty database unless --force-overwrite is explicit.\n'
