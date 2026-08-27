# HANDOFF — KAsset-Trader-Core

갱신: 2026-08-28 (Google 로그인 + Luna/Terra/Sol 3단 AI 라우터 배포·실검증)

## 이번 세션에서 한 일 (2026-08-28)

- **Google 간편로그인**: `POST /api/v1/auth/google` — RS256+JWKS 검증(aud/iss/exp/
  email_verified), `users.google_sub` 식별·자동가입, 미설정 시 503 fail-closed.
  migration `20260828_kasset_google_sub`. checker PASS. 서버 배포 완료(가짜 토큰 → 401).
- **3단 AI 모델 라우터** (`app/extensions/kasset/ai/model_router.py`): Responses API +
  strict json_schema, tool use 금지, 고정 instructions(캐싱). Luna(→저신뢰/actionable
  →Terra)(→저신뢰/HIGH/escalate→Sol=critical_review). 티어별 OpenRouter 폴백:
  Luna→`deepseek/deepseek-v4-flash-0731@preset/kasset-cheap`, Terra/Sol→v4-pro.
  폴백 트리거는 429/5xx/transport만. 운영 실검증: 사다리 관통(REVIEW/0.82→sol),
  가짜 OAI 키 강제 시 OR 폴백 응답 확인.
- **이벤트 파이프라인**: FeatureEngine(RSI14 Wilder/SMA20/volume_ratio/20일 돌파,
  순수함수) + EventDetector(±2%/거래량 2x/RSI 30·70/돌파/중요뉴스) →
  트리거 없으면 LLM 미호출. `kasset_market_events.run`(평일 15분 cron,
  `KASSET_MARKET_EVENTS_ENABLED` 기본 False) → BUY/SELL·conf≥0.60만
  AIRecommendation PENDING 저장. KR 대체거래소 partition은 "NTX"(NXT 표기 수용).
- **AI env(서버 .env.kasset)**: `KASSET_AI_API_MODEL=gpt-5.6-terra`, OAI 키,
  OpenRouter 키(프리셋 kasset-cheap = relace/deepinfra/coreweave allowlist).
  키 파일 개행 누락으로 env 라인이 붙는 사고 1회 — 분리 복구 완료(키 추가는 반드시
  개행 보장 후 append).
- checker 2회(1차 REWORK: NTX venue·shard 누락 → 수정, 2차 PASS). 커밋 `da2e403a` 배포됨.
- 알려진 로컬 한계: Windows에서 research/스크립트 계열 301 failed·77 errors는 fcntl·CRLF
  SHA·trailing-space 등 플랫폼 잡음(Linux CI 기준 무관). diff 영향 영역 격리 스위트는
  2965 passed(실패 2건은 stash 후에도 재현되는 기존 한계).

## 프로젝트 개요와 사용자가 원하는 방향

이 저장소는 기존 KAsset Trading Core다. 이번 통합은 기존 인증·DB·PAPER 시세·`PaperTradingService`를 재사용하면서 `V:/HANSE/KAsset-Trader/android`의 `TraderApi`와 호환되는 HTTP 표면을 추가했다.

고정 경계:

- `PAPER`는 기존 Core 기능을 facade로 재사용한다. 별도 가짜 거래 엔진을 만들지 않는다.
- `NH`는 PLUG 모의투자 잔고·보유·현재가 조회만 허용한다.
- NH 주문·정정·취소는 `409 BROKER_READ_ONLY`와 `NH PLUG는 현재 모의 Read-Only 단계입니다.`로 차단한다.
- NH 데이터 요청은 `https://moapi.nhplug.com:8443`의 account/balance/currentPrice allowlist만 허용한다. 운영 주문 host/path는 범위 밖이다.
- Broker Credential은 AES-256-GCM Vault에 암호화 저장하고 응답·로그·예외에는 원문을 노출하지 않는다.
- 기존 Core API·DB·서비스를 깨지 않고 Android 호환 router만 확장한다.

## 전체 진행 상태

- **완료 — 브랜치 통합:** `integrate/pr1-pr3` 브랜치에 PR1(브리핑)·PR3(NH 토큰 캐시)·PR2(추천 API)가
  upstream 최신 main 위로 통합됨. Alembic 단일 head `20260827_kasset_multi_user_core`.
- **완료 — 다중 사용자 컷오버:** pairing 제거 → 공개 계정 register/login/refresh/revoke
  (device-bound JWT). 주문·체결·잔고·credential·추천·risk·kill switch 전부 `owner_user_id` 스코프.
  migration이 단일 trader 조건으로 legacy 데이터를 backfill하고, 조건 위반 시 fail-closed.
- **완료 — 토큰 경계:** kasset-android 토큰은 모바일 표면 + `/api/v1/ai/recommendations`에서만
  유효. generic Core trader 게이트(loss-cut 승인 등)는 401 거부
  (`app/extensions/kasset/api/paths.py::is_kasset_token_allowed_path`).
- **완료 — AI PAPER 자동화:** 4개 결정론 전략 + producer(합의 synthesis) + consumer
  (preview→policy 재확인→submit, LIVE 금지) + backtest. `AI_PAPER_AUTO_EXECUTION_ENABLED`
  기본 false의 fail-closed TaskIQ 스윕(`kasset.paper_automation.run`, 5분 주기), owner 실패 격리.
- **완료 — 배포 매니페스트:** `deploy/kasset/{compose.yaml,Caddyfile,env.example}`,
  `scripts/kasset_{backup,restore,smoke}.sh` (CSP 중립).
- **완료 — 검증:** 로컬 PostgreSQL 16에서 kasset 93 + routers 22 + middleware 6 +
  migration 체인 13 + 컷오버 가드 1 + 자동화 배선 5 passed. ruff/ty clean.
  checker 2회(전체→델타) 후 잔여 차단 finding 0.
- **완료 — Naver Cloud 배포:** `main` `76923cfe`가 `/opt/kasset-trader-core`에 배포됨.
  migration head `20260827_kasset_multi_user_core`, 기존 운영자(`kasset-mobile`, id=1)가
  runtime_state 소유자로 backfill. api/db/redis healthy, caddy running.
- **완료 — live E2E:** 서버에서 계정 register→login→system/status, generic trader 게이트
  401 거부, 추천 PENDING 조회→APPROVED 결정→DB 영속, 주문 ledger 불변(0→0) 실측.
  smoke 계정·fixture는 정리함.
- **사고·복구 기록:** 배포 중 db 컨테이너 재생성으로 데이터가 초기화됐다. 원인은
  `docker-compose.kasset.yml`의 볼륨 매핑 결함 — `timescale/timescaledb-ha:pg17`의
  PGDATA는 `/home/postgres/pgdata`인데 볼륨이 `/var/lib/postgresql/data`에 걸려 있어
  실데이터가 컨테이너 레이어에 있었다. 재생성 직전에 받은 pg_dump
  (`/root/backups/kasset_pre_multiuser_20260827_2353.dump`)로 전량 복원했고, 볼륨 매핑을
  `postgres_data:/home/postgres/pgdata`(uid 1000 chown)로 수정해 재발을 차단했다.
- **연기(사용자 결정 2026-08-27) — 이전 리허설:** VPS 이전과 빈 호스트 복원 검증은
  약 3개월 후(지원 자원 종료 전)에 진행한다. 그때까지 현 Naver Cloud 서버를 유지하고,
  `scripts/kasset_backup.sh` 기반 주기 백업과 `/root/backups`의 pg_dump 보관을 전제로 한다.

현재 브랜치: `main` `76923cfe` (origin/main 동일). 서버도 동일 커밋.

## 이번 세션에서 한 일

1. 로컬 임시 PostgreSQL 16(`E:/LVDT_Projects/.pgtmp`)을 세워 이전 세션에서 불가능했던
   DB-backed 검증 전부를 실측했다.
2. DB 실측으로 드러난 결함 수정: `test_android_contract` 더미 DB → 빈 결과 세션,
   `test_multi_user_contract`의 만료 인스턴스 동기 접근(MissingGreenlet) → id 사전 캡처,
   briefing `unavailableReason` 기계 코드 → 사용자 표시용 한국어.
3. Migration 체인 3종 수리: kasset 모델을 `app/models/__init__`에 등록(create_all 완전성),
   63자 초과 FK 이름 단축(`fk_kasset_android_paper_accounts_paper_account_id`),
   chain fixture에 users CI 인덱스 drop 추가, POSIX 전용 alembic 경로 →
   `sys.executable -m alembic`.
4. checker 검수(전체 1회 + 델타 1회) finding 해소:
   - HIGH: kasset 토큰이 generic trader 게이트 통과 → `is_kasset_token_allowed_path`로
     모바일 표면 + 추천 API만 허용, `get_current_user`가 경로 검사. 회귀 테스트 추가.
   - MEDIUM: 자동화 미배선 → `automation/job.py`(안전 게이트·owner 어댑터·스윕) +
     `app/tasks/kasset_paper_automation_tasks.py`(5분 cron, fail-closed) +
     `AI_PAPER_AUTO_EXECUTION_ENABLED` config 선언. owner 실패 격리 포함, 테스트 5개.
   - MEDIUM: migration 가드 미검증 → `test_multi_user_migration_guards.py`가 실제 alembic
     CLI로 2-trader upgrade 거부와 2-owner downgrade 거부를 실측.
   - LOW(FK 접미사): 관례 이름 64자 > PostgreSQL 63자 한계로 기각.
5. `workflow_dispatch` 계약 테스트 갱신, `ci_shards/shard-1.txt`에 신규 테스트 등록,
   Caddy `/health` 공개 계약 유지 수정, PAPER preview에 사용자 kill switch 반영,
   env/런북의 pairing 잔재를 계정 인증으로 정리.

검증 실측 (로컬 PostgreSQL 16, `.venv` python):

```text
pytest tests/extensions/kasset -p no:randomly            → 93 passed
pytest tests/routers/test_ai_recommendations.py + middleware → 28 passed
pytest migration 체인 3종                                 → 13 passed
pytest tests/extensions/kasset/test_multi_user_migration_guards.py → 1 passed
pytest tests/middleware tests/ci tests/infra             → 305+ passed
  (예외 1: trailing-space 파일명 테스트는 Windows FS 한계, diff 무관)
ruff check / format --check app tests scripts            → clean
ty check app/ --error-on-warning                         → clean
alembic heads                                            → 20260827_kasset_multi_user_core 단일
Android :app:testDebugUnitTest                           → 55 tests, 0 failures
```

독립 검수: checker 전체 1회(REWORK) → 수정 → 델타 1회(잔여 차단 0). FINAL: PASS.

## 다음 세션이 바로 할 일

1. 사용자가 배포를 승인하면: `integrate/pr1-pr3`를 main에 merge·push하고
   `/opt/kasset-trader-core`에 배포, `alembic upgrade head`(가드가 단일 trader를 요구함),
   `scripts/kasset_smoke.sh` 실행.
2. 배포 후 Android 계정 가입→로그인→PAPER→NH 조회→추천 승인/거절 live E2E와
   주문 ledger 불변을 확인한다.
3. `AI_PAPER_AUTO_EXECUTION_ENABLED`는 운영자가 명시적으로 켤 때까지 false로 둔다.
4. 빈 Linux 호스트 복원 리허설은 대상 호스트 확보 후 `scripts/kasset_backup.sh`/`restore`로 진행.

남은 기술 위험:

- mobile JWT와 Core JWT가 같은 `SECRET_KEY`를 공유한다. 경로 스코프로 차단했지만
  audience claim 분리가 더 강한 후속 개선이다.
- 자동화 producer는 라이브러리+테스트로 존재하며 외부 AI 파이프라인이 추천 POST API로
  공급하는 구조다. producer의 스케줄 배선은 별도 제품 결정이 필요하다.
- 진짜 부분체결 도입 시 PAPER correlation 조회 `scalar_one_or_none()`의
  `MultipleResultsFound` 가능성은 여전하다.


## 세션 이력

- 2026-08-27: Naver Cloud에 main 76923cfe 배포, 볼륨 결함 사고를 pg_dump로 복구, live 계정·추천 E2E 실측.
- 2026-08-27: 다중 사용자 컷오버·AI PAPER 자동화·배포 매니페스트를 실제 PostgreSQL로 검증하고 checker PASS로 종결.
- 2026-08-26: Android 호환 API, PAPER facade, NH Mock Read-Only, Credential Vault, 검증·런북 완료; 독립 고위험 재검수 PASS.
