# HANDOFF — KAsset-Trader-Core

갱신: 2026-08-26 (KAsset Trader Android 호환 API와 PAPER/NH Mock Read-Only 통합 완료)

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

- **완료 — Android 호환 API:** `/health`, `/api/v1/auth/*`, brokers/system/account/positions/market/orders/fills/risk/ai.
- **완료 — 인증:** pairing secret constant-time 비교, device-bound access/refresh JWT, refresh 회전, revoke.
- **완료 — Credential Vault:** 앱키/시크릿/계좌번호 개별 AES-256-GCM 암호화, masked 응답, migration `20260826_kasset_credential_vault`.
- **완료 — PAPER:** 기본 계좌, 잔고·보유·시세, preview/submit/list/detail/cancel/amend/fills, `clientOrderId` idempotency, Kill Switch/Trading Mode gate.
- **완료 — NH Mock Read-Only:** credential verify, account allowlist, token cache, 잔고·보유·현재가 정규화, 주문 이중 차단.
- **완료 — 자동 검증:** 관련 API/NH/PAPER/보안 테스트 75건, ruff, ty, Alembic head 확인.
- **대기 — 외부/DB 통합:** 로컬 PostgreSQL과 실제 NH operator credential이 없어 DB-backed 서버 E2E와 NH 외부 read-only smoke는 미실행.

현재 브랜치/관련 head: `kasset-integration` / `833afe9f`.
Android 관련 head: HANSE `main` / `4e620fea`.

## 이번 세션에서 한 일

1. `app/extensions/kasset/api/**`에 Android 호환 인증, 오류 봉투, DTO, broker registry, runtime state, credential vault, PAPER/NH adapter, router를 추가했다.
2. `app/extensions/kasset/models.py`, `alembic/versions/20260826_kasset_credential_vault.py`로 device session, encrypted broker credential, Android PAPER order, runtime state 저장을 추가했다.
3. NH PLUG client에 exact mock host/port/path, redirect 거부, 계좌 allowlist, read-only dispatch, secret redaction을 적용했다. 공식 OpenAPI의 `currentPrice` 필드명은 문서로 대조했지만 실제 credential 응답은 아직 확인하지 않았다.
4. PAPER 주문의 idempotency, 시장가/지정가 체결, 취소·정정, 리스크·Kill Switch 회귀를 추가했다.
5. 독립 검수에서 발견된 NH `Broker.display_name` 누락, NH orders/fills의 잘못된 409, PAPER 부분체결 정정 총수량 왜곡, UTC `Z` 불일치를 `833afe9f`에서 수정했다.
6. README, `env.example`, `docs/runbooks/kasset-android-nh-mock-readonly.md`에 실행·안전·외부 smoke 절차를 기록했다.

검증 실측:

```text
uv run pytest -q tests/extensions/kasset/api tests/services/brokers/nhplug tests/scripts/test_nhplug_mock_smoke.py
→ 75 passed in 2.93s

uv run ruff check app/extensions/kasset/api tests/extensions/kasset/api
uv run ty check app/extensions/kasset/api tests/extensions/kasset/api
→ All checks passed

uv run alembic heads
→ 20260826_kasset_credential_vault (head)
```

독립 검수:

- `security-reviewer`: PASS. Credential 원문 누출, 운영 주문 경로, 평문 영구 저장에 대한 차단 확인. 공유 JWT secret의 generic Core endpoint 접근 가능성은 LOW 잔여 위험.
- `checker-deep`: 수정 전 HIGH/MEDIUM 지적 후 `833afe9f`와 Android `4e620fea` 재검수 PASS. 기존 차단 항목 모두 해소.

환경 한계:

- PostgreSQL/Docker/psql이 없어 전체 DB 통합 실행은 `asyncpg ConnectionRefusedError [WinError 1225]`로 불가했다. 같은 환경에서 반복하지 않는다.
- 실제 NH App Key/App Secret/Mock 계좌가 없어 `--confirm-read` smoke는 실행하지 않았다.

## 다음 세션이 바로 할 일

1. PostgreSQL을 준비하고 `uv run alembic upgrade head` 후 `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`을 실행한다.
2. Android에서 페어링→PAPER 잔고/보유/시세→preview/submit→같은 `clientOrderId` 재시도→내역→Kill Switch까지 실제 DB로 확인한다.
3. 운영자가 제공한 NH 모의 credential로 런북의 account/quote `--confirm-read` smoke를 실행하고 실제 `Output_0`/`Output_1` 키를 adapter 매핑과 대조한다. 주문 명령과 운영 host는 사용하지 않는다.

남은 기술 위험:

- 한 PAPER 주문에 여러 `PaperTrade`가 생기는 진짜 부분체결이 추가되면 correlation 조회가 `scalar_one_or_none()`라 `MultipleResultsFound` 가능성이 있다. 현재 경로는 한 번에 FILLED라 도달하지 않는다.
- `SystemStatus.database.status`는 broker/runtime DB 조회 성공 후 `ok`를 반환하지만 별도 ping과 `migrationRevision` 보고는 아직 없다.
- mobile JWT와 기존 Core JWT가 같은 `SECRET_KEY`를 사용한다. mobile route는 client/device를 검사하지만 generic Core bearer endpoint의 audience 분리는 후속 보안 강화 항목이다.

## 세션 이력

- 2026-08-26: Android 호환 API, PAPER facade, NH Mock Read-Only, Credential Vault, 검증·런북 완료; 독립 고위험 재검수 PASS.
