#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$ROOT_DIR/deploy/kasset/compose.yaml"
ENV_FILE=${KASSET_COMPOSE_ENV_FILE:-"$ROOT_DIR/deploy/kasset/.env"}
PUBLIC_URL=

usage() {
    cat <<'EOF'
Usage: kasset_smoke.sh [--env-file PATH] [--url https://DOMAIN/health]

Checks the resolved service state, calls the API health endpoint inside the
container, then calls the public Caddy /health TLS endpoint. The public URL
normally derives from KASSET_DOMAIN in the Compose env file; --url is useful
for an alternate DNS name that presents a valid certificate.

Fresh Linux host sequence:
  docker compose --env-file deploy/kasset/.env -f deploy/kasset/compose.yaml up -d --wait
  scripts/kasset_smoke.sh --env-file deploy/kasset/.env
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --env-file)
            [ "$#" -ge 2 ] || { echo "--env-file requires a path" >&2; exit 2; }
            ENV_FILE=$2
            shift 2
            ;;
        --url)
            [ "$#" -ge 2 ] || { echo "--url requires an HTTPS URL" >&2; exit 2; }
            PUBLIC_URL=$2
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

for command_name in docker curl awk tr; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "Required command not found: $command_name" >&2
        exit 1
    }
done

docker compose version >/dev/null

compose() {
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

read_env_value() {
    wanted=$1
    awk -v wanted="$wanted" '
        /^[[:space:]]*#/ { next }
        {
            line = $0
            sub(/^[[:space:]]*/, "", line)
            key = line
            sub(/[[:space:]]*=.*/, "", key)
            if (key == wanted) {
                sub(/^[^=]*=[[:space:]]*/, "", line)
                sub(/[[:space:]]*$/, "", line)
                value = line
            }
        }
        END {
            if (value ~ /^".*"$/ || value ~ /^'"'"'.*'"'"'$/) {
                value = substr(value, 2, length(value) - 2)
            }
            print value
        }
    ' "$ENV_FILE"
}

if [ -z "$PUBLIC_URL" ]; then
    domain=$(read_env_value KASSET_DOMAIN)
    [ -n "$domain" ] || { echo "KASSET_DOMAIN is empty; pass --url explicitly" >&2; exit 1; }
    PUBLIC_URL="https://$domain/health"
fi

case "$PUBLIC_URL" in
    https://*/health|https://*/health/) ;;
    *)
        echo "Smoke URL must use HTTPS and end in /health: $PUBLIC_URL" >&2
        exit 1
        ;;
esac

compose ps

internal_body=$(compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).read().decode('utf-8'))")
internal_compact=$(printf '%s' "$internal_body" | tr -d '[:space:]')
case "$internal_compact" in
    *'"status":"ok"'*) ;;
    *)
        echo "Internal API health response did not report status=ok" >&2
        exit 1
        ;;
esac

public_body=$(curl --fail --silent --show-error --location --max-time 20 "$PUBLIC_URL")
public_compact=$(printf '%s' "$public_body" | tr -d '[:space:]')
case "$public_compact" in
    *'"status":"ok"'*) ;;
    *)
        echo "Public Caddy health response did not report status=ok" >&2
        exit 1
        ;;
esac

printf 'Internal API health: ok\n'
printf 'Public TLS health: ok (%s)\n' "$PUBLIC_URL"
