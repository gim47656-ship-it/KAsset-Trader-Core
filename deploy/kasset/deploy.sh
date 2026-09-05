#!/usr/bin/env bash
# KAsset Core 운영 배포 스크립트. GitHub Actions self-hosted runner(운영서버)에서 실행된다.
#
# 지금까지 수동으로 하던 절차를 그대로 옮겼다:
#   git checkout <sha> → .env.kasset의 CORE_IMAGE_TAG/VCS_REF 갱신 → docker compose build api
#   → (alembic 변경이 있으면 DB 백업 후 migration) → up -d 5개 서비스 → /health 200 확인
#   → 실패 시 이전 SHA로 롤백.
#
# 사용: deploy/kasset/deploy.sh <target-sha>
# 환경: ALLOW_MIGRATION=1 이면 alembic/versions 변경이 있어도 진행(DB 백업 후 migration 실행).
#       그렇지 않으면 alembic 변경이 감지되면 배포하지 않고 exit 2.
set -euo pipefail

TARGET_SHA="${1:?usage: deploy.sh <target-sha>}"
ALLOW_MIGRATION="${ALLOW_MIGRATION:-0}"
REPO_DIR="${KASSET_REPO_DIR:-/opt/kasset-trader-core}"
ENV_FILE=".env.kasset"
SERVICES=(api worker scheduler mcp ai-mcp)
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-180}"

log() { printf '[deploy %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() { printf '::error::%s\n' "$*" >&2; exit 1; }

cd "$REPO_DIR"
compose() { docker compose -f docker-compose.kasset.yml --env-file "$ENV_FILE" "$@"; }

env_value() { grep -oP "^$1=\K.*" "$ENV_FILE" || true; }

CURRENT_SHA="$(env_value CORE_IMAGE_TAG)"
DOMAIN="$(env_value KASSET_DOMAIN)"
[ -n "$DOMAIN" ] || fail "KASSET_DOMAIN이 $ENV_FILE에 없다"

git fetch -q origin main
git cat-file -e "${TARGET_SHA}^{commit}" || fail "알 수 없는 commit: $TARGET_SHA"
TARGET_SHA="$(git rev-parse "$TARGET_SHA")"

if [ "$CURRENT_SHA" = "$TARGET_SHA" ]; then
  log "이미 $TARGET_SHA 가 배포되어 있다. 재기동만 수행."
fi

# ── migration 감지 ─────────────────────────────────────────────────────────────
MIGRATE=0
if [ -n "$CURRENT_SHA" ] && git cat-file -e "${CURRENT_SHA}^{commit}" 2>/dev/null; then
  if git diff --name-only "$CURRENT_SHA" "$TARGET_SHA" -- alembic/versions | grep -q .; then
    MIGRATE=1
  fi
else
  log "현재 배포 SHA($CURRENT_SHA)를 로컬 git에서 찾지 못해 migration 변경을 판정할 수 없다 → migration 있는 것으로 간주"
  MIGRATE=1
fi
if [ "$MIGRATE" = 1 ] && [ "$ALLOW_MIGRATION" != 1 ]; then
  printf '::error::alembic/versions 변경이 포함된 배포다. 자동배포는 건너뛴다. workflow_dispatch에서 allow_migration=true 로 수동 승인 배포하라.\n' >&2
  git diff --name-only "$CURRENT_SHA" "$TARGET_SHA" -- alembic/versions >&2 || true
  exit 2
fi

# ── checkout + env 갱신 ─────────────────────────────────────────────────────────
BACKUP_ENV="${ENV_FILE}.pre-${TARGET_SHA:0:8}"
cp "$ENV_FILE" "$BACKUP_ENV"
git checkout -q "$TARGET_SHA"
sed -i "s/^CORE_IMAGE_TAG=.*/CORE_IMAGE_TAG=$TARGET_SHA/; s/^VCS_REF=.*/VCS_REF=$TARGET_SHA/" "$ENV_FILE"
log "checkout $TARGET_SHA, env 갱신 (백업: $BACKUP_ENV)"

rollback() {
  log "롤백 시작 → $CURRENT_SHA"
  cp "$BACKUP_ENV" "$ENV_FILE"
  if [ -n "$CURRENT_SHA" ] && git cat-file -e "${CURRENT_SHA}^{commit}" 2>/dev/null; then
    git checkout -q "$CURRENT_SHA"
  fi
  compose up -d --no-build "${SERVICES[@]}" || true
  log "롤백 완료(이미지 $(env_value CORE_IMAGE_TAG)). DB migration은 자동으로 되돌리지 않는다."
}

# ── build ──────────────────────────────────────────────────────────────────────
log "docker compose build api"
compose build api

# ── migration (승인된 경우만) ───────────────────────────────────────────────────
if [ "$MIGRATE" = 1 ]; then
  BACKUP_DIR="$REPO_DIR/backups"
  mkdir -p "$BACKUP_DIR"
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  DUMP="$BACKUP_DIR/kasset-pre-migration-${TARGET_SHA:0:8}-$STAMP.dump.gz"
  log "migration 전 DB 백업 → $DUMP"
  docker exec kasset-trader-db-1 pg_dump -U kasset -d kasset --format=custom | gzip > "$DUMP.tmp"
  [ -s "$DUMP.tmp" ] || { rm -f "$DUMP.tmp"; rollback; fail "DB 백업이 비었다"; }
  mv "$DUMP.tmp" "$DUMP"
  log "alembic upgrade head"
  if ! compose --profile migration run --rm -T migration; then
    rollback
    fail "migration 실패. DB 백업: $DUMP"
  fi
fi

# ── up ─────────────────────────────────────────────────────────────────────────
log "up -d ${SERVICES[*]}"
if ! compose up -d --no-build "${SERVICES[@]}"; then
  rollback
  fail "compose up 실패"
fi

# ── health ─────────────────────────────────────────────────────────────────────
deadline=$((SECONDS + HEALTH_TIMEOUT_SEC))
ok=0
while [ $SECONDS -lt $deadline ]; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$DOMAIN/health" || true)"
  running="$(docker ps --format '{{.Image}}' | grep -c "kasset-trader-core:$TARGET_SHA" || true)"
  if [ "$code" = "200" ] && [ "$running" -ge "${#SERVICES[@]}" ]; then
    ok=1
    break
  fi
  sleep 5
done
if [ "$ok" != 1 ]; then
  log "health 실패(code=${code:-none}, running=${running:-0}/${#SERVICES[@]})"
  docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep kasset-trader || true
  rollback
  fail "배포 후 health 확인 실패 → 롤백함"
fi

log "OK: $TARGET_SHA 배포 완료, /health 200, 컨테이너 ${#SERVICES[@]}개 새 이미지"
docker ps --format '{{.Names}}\t{{.Status}}' | grep kasset-trader-core >/dev/null || true
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E "kasset-trader-(api|worker|scheduler|mcp|ai-mcp)" | sed "s/kasset-trader-core://"
