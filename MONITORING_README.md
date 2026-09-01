# Monitoring & Observability Guide

현재 브랜치는 OTEL/Grafana/Signoz 스택이 제거된 상태이며, 표준 모니터링은 Sentry입니다.

## 운영 정책

- 단일 Sentry 프로젝트 사용
- 프로세스 구분은 `service` 태그로 처리
- `SENTRY_DSN` 값이 있으면 환경(dev/staging/prod)과 무관하게 활성화
- 수집 범위: 에러 + 트레이스 + 프로파일
- 샘플링: `traces=1.0`, `profiles=1.0`
- `send_default_pii=false` 기본(ROB-1305, 옵트인 필요), 민감키(`authorization`, `cookie`, `token`, `secret`, `password`)는 마스킹
- `logger.error`는 Sentry 이벤트로 전송

## 계측 대상 프로세스

- API (`auto-trader-api`)
- Celery worker (`auto-trader-worker`)
- MCP server (`auto-trader-mcp`)
- Upbit websocket (`auto-trader-upbit-ws`)

## 환경 변수

```bash
SENTRY_DSN=
SENTRY_ENVIRONMENT=
SENTRY_TRACES_SAMPLE_RATE=1.0
SENTRY_PROFILES_SAMPLE_RATE=1.0
SENTRY_SEND_DEFAULT_PII=false
SENTRY_ENABLE_LOG_EVENTS=true
```

## 실행 커맨드

```bash
# API
uv run uvicorn app.main:api --reload --host 0.0.0.0 --port 8000

# Worker
uv run celery -A app.core.celery_app.celery_app worker --loglevel=info

# MCP
uv run python -m app.mcp_server.main

# 실행 체결 WebSocket은 Upbit 전용
uv run python websocket_monitor.py --mode upbit
```

주식 체결은 WebSocket이 아니라 worker의
`toss_live.poll_fills_periodic`으로 확인합니다. Toss 실주문 운영 시
`TOSS_FILL_POLL_ENABLED=true`, `TOSS_FILL_POLL_CRON=*/2 * * * *`로 최대
2분 간격을 유지하고, worker 로그에서 주기 실행과 오류 부재를 확인합니다.
NH PLUG는 국내주식 모의계좌 조회 전용이므로 체결 모니터링 대상이 아닙니다.
KIS WebSocket과 KIS 서비스는 운영 대상이 아닙니다.

## 운영 확인

```bash
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f mcp
docker compose -f docker-compose.prod.yml logs -f upbit_websocket
```

Sentry UI 확인 항목:
- `service:auto-trader-api` 등 태그 필터로 프로세스 분리 조회
- release가 현재 배포 커밋 SHA로 표시되는지 확인
- API/worker/Upbit WS/MCP 이벤트 유입 확인
- worker의 `toss_live.poll_fills_periodic` 성공 주기와 마지막 오류 확인
- 트랜잭션 및 프로파일 생성 확인
