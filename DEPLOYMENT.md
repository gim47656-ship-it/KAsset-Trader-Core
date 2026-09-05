# Auto Trader 배포 가이드

이 문서는 `docker-compose.prod.yml` 기반 프로덕션 배포 절차를 설명합니다.

## 배포 파이프라인 개요

```
feature branch → (PR) → main → (merge) → production → (CI: ghcr-images.yml, legacy) → GHCR → (서버) → deploy.sh
```

- **main**: 개발 브랜치 (모든 PR의 base)
- **production**: 배포 브랜치 (GHCR 이미지 빌드 트리거)

| 단계 | 트리거 | 자동화 |
|------|--------|--------|
| feature → main | PR 머지 | 수동 (리뷰 필수) |
| main → production | 브랜치 머지 | 수동 (`git merge main`) |
| production → GHCR | push to `production` | **자동** (GitHub Actions `ghcr-images.yml`(legacy; KAsset 운영 배포는 `deploy-kasset.yml`)) |
| GHCR → 서버 배포 | 이미지 pull + restart | 수동 (`scripts/deploy.sh`) |

### 전체 배포 절차 (main 머지 후)

```bash
# 1. production 브랜치에 main 머지
git checkout production
git pull origin production
git merge main
git push origin production

# 2. GitHub Actions 빌드 완료 대기 (약 5-10분)
#    확인: https://github.com/<repo>/actions/workflows/ghcr-images.yml

# 3. 서버에서 배포 실행
cd /home/mgh3326/auto_trader
scripts/deploy.sh --auto-migrate

# 4. 서비스 상태 확인
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
curl http://localhost:8000/healthz
curl http://localhost:8765/mcp
```

## 1. 사전 준비

- Docker / Docker Compose 설치
- `.env.prod` 작성
- GHCR 접근 권한 준비

```bash
cp env.prod.example .env.prod
```

필수 항목:
- `GITHUB_REPOSITORY`
- `DATABASE_URL`
- `REDIS_URL`
- API 키(Toss/Upbit/Google/OpenDART)
- `DOCS_ENABLED=false`

Sentry:
- `SENTRY_DSN`
- `SENTRY_ENVIRONMENT`
- `SENTRY_TRACES_SAMPLE_RATE=1.0`
- `SENTRY_PROFILES_SAMPLE_RATE=1.0`
- `SENTRY_SEND_DEFAULT_PII=false` (ROB-1305 기본값, 옵트인 필요)
- `SENTRY_ENABLE_LOG_EVENTS=true`
- release는 이미지 빌드 SHA가 자동으로 주입됩니다.

선택 항목 (HTTPS reverse proxy):
- `ACME_EMAIL`
- `DOMAIN_NAME`

## 2. 이미지 배포

```bash
# 이미지 pull
docker compose --env-file .env.prod -f docker-compose.prod.yml pull

# 마이그레이션
docker compose --env-file .env.prod -f docker-compose.prod.yml --profile migration up migration

# 서비스 실행
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d

# HTTPS reverse proxy가 필요하면 별도로 실행
docker compose -f docker-compose.monitoring-rpi.yml up -d caddy
```

## 3. 서비스 확인

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
curl http://localhost:8000/healthz
```

주요 서비스:
- `api`
- `worker`
- `mcp`
- `upbit_websocket`


### 주식 운영 경계와 체결 증거

- KR/US 운영 계좌와 주문은 Toss만 사용합니다. KIS 자격증명, 서비스, WebSocket,
  MCP 도구는 배포하지 않습니다. 남은 KIS 어댑터와 레저 모델은 과거 행 조회
  호환성을 위한 비활성 코드입니다.
- NH PLUG는 국내주식 모의계좌의 계좌·잔고·보유·현재가 조회만 지원합니다.
  주문·정정·취소와 미국주식 지원을 가정하지 마세요.
- Toss는 이 경로에서 체결 WebSocket을 사용하지 않습니다. 실주문을 허용하는
  프로덕션은 `TOSS_FILL_POLL_ENABLED=true`와
  `TOSS_FILL_POLL_CRON=*/2 * * * *`를 설정해
  `toss_live.poll_fills_periodic`을 최대 2분 간격으로 실행해야 합니다.
  작업은 Toss 주문 증거를 읽어 accepted-only 레저를 멱등 reconcile합니다.
- worker 재기동 뒤 로그에서 `toss_live.poll_fills_periodic` 실행을 확인합니다.
  poller를 운영할 수 없는 예외 상황에서는 accepted 주문마다 즉시
  `toss_reconcile_orders(order_id=<broker-order-id>, dry_run=False)`를 실행해야
  하며, 둘 다 없으면 주식 실주문을 활성화하지 않습니다.
## 4. 로그 확인

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f api
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f worker
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f mcp
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f upbit_websocket
```

## 5. Sentry 확인

- 단일 프로젝트에서 `service` 태그로 분리 조회:
  - `auto-trader-api`
  - `auto-trader-worker`
  - `auto-trader-mcp`
  - `auto-trader-upbit-ws`
- 에러/트랜잭션/프로파일 유입 확인

## 6. 업데이트 절차

```bash
# 권장: 배포 스크립트 사용
scripts/deploy.sh                    # 마이그레이션 없이 빠른 배포
scripts/deploy.sh --auto-migrate     # 마이그레이션 포함
scripts/deploy.sh --auto-migrate --backup  # 백업 + 마이그레이션

# 수동 실행
docker compose --env-file .env.prod -f docker-compose.prod.yml pull
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d
```

## 7. 롤백

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml down
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d
```

## 8. 문제 해결

### Sentry 이벤트 미수집

- `SENTRY_DSN` 값 확인
- 샘플링 설정(`SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE`) 확인
- 로그 이벤트 정책(`SENTRY_ENABLE_LOG_EVENTS`) 확인

### 즉시 비활성화

- `SENTRY_DSN`을 비우고 재기동
