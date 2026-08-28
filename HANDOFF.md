# HANDOFF — KAsset-Trader-Core

갱신: 2026-08-29 심야 (`range=1D` 분봉 서빙 배포·마이그레이션 테스트 프라이밍·shard exact-cover·CI 전체 초록)

## 이번 세션에서 한 일 (2026-08-29 심야)

### 0. 요약 — CI가 처음으로 전 잡 초록이 됐고, `1일` 분봉이 운영에 붙었다

run `33194653480` **success**. `lint`, `taskiq-smoke`, `test (3.13, 1..4)`, `security`,
`frontend`, `ci-required` 전부 통과. 직전 run `33192185520`의 유일한 실패였던
shard 1을 원인까지 확정해 닫았다.

### 1. `GET /api/v1/market/candles?range=1D` 분봉

`range` 집합 `1D|1W|1M|3M|6M`, 응답 최상위에 `interval`(`"1m"`/`"1d"`) 추가. 캔들 요소
스키마는 불변이다.

- 상향 엔드포인트를 실측으로 정정했다: `GET /api/v1/candles?symbol=…&interval=1m` = **200**,
  `/api/v1/stocks/{symbol}/candles` = **404**.
- 기존 pager(`app/services/brokers/toss/candles.py`, 요청당 `min(remaining, 200)`)를 재사용해
  정규장 390분을 200+190 두 호출로 받는다. Toss `count.maximum = 200`, rate limit 그룹은
  기존 `MARKET_DATA_CHART`.
- Toss market calendar의 `regular_market` window로 당일 정규장만 남긴다 → `20:00 KST`
  시간외 봉이 섞이지 않는다.
- **빈 분봉에 일봉을 대체하지 않는다.** 장 마감이면 `{"interval":"1m","candles":[]}`를 그대로
  내보내고 앱이 `장 시작 전`을 그린다. 없는 가격을 만들지 않는다.

운영 실측(배포 후, 미국 정규장 중):

```text
KRX 035420 range=1D → {"interval":"1m","candles":[]}          (KRX 마감)
KRX 035420 range=1W → interval "1d"
US  TQQQ   range=1D → interval "1m", 235봉, 13:30Z~17:18Z 1분 간격
                       open 72.95 / high 74.18 / low 71.42 / volume 합 5359548
```

### 2. CI shard 1 실패 — run-owned database guard 누락

`tests/infra/test_database_guard_completeness.py::test_every_noconftest_postgresql_survivor_calls_the_database_guard`
단일 실패(6229 passed). `tests/extensions/kasset/test_multi_user_migration_guards.py`가
`asyncpg.connect` + `create_async_engine`으로 PostgreSQL을 직접 열면서
`validate_run_owned_database_url`을 호출하지 않았다. `--noconftest`로 conftest 가드가
우회될 때 실행 소유가 아닌 DB에 붙는 것을 막는 소스 수준 계약이다.

수리는 한 줄이다: `make_url(settings.DATABASE_URL)` →
`validate_run_owned_database_url(settings.DATABASE_URL)` (`tests/services/paper_evaluation/test_migration.py:151`과
동일 패턴). 이 계약은 allowlist가 아니라 실제 가드 호출을 요구하므로 예외 등록으로 피하지 않았다.

### 3. 마이그레이션 테스트 프라이밍 수리 (리비전 무변경)

`DuplicateColumnError: instruments.aliases`의 원인은 마이그레이션이 아니라 **테스트가
과거 스키마를 재구성하는 방식**이었다. `instruments` 실제 생성자는
`alembic/versions/b3e58be9e79b_init.py:54-66`(aliases 없음)이고 `aliases`는
`20260828_kasset_nickname_aliases.py:31-34`에서만 추가되는데, ORM
`app/models/trading.py:56-69`가 이미 `Instrument.aliases`를 선언한다. 실패 테스트들이
current-head `Base.metadata.create_all` → 과거 리비전 `stamp` → 체인 재생 순서로 돌면서
후대 산물(`instruments.aliases`, `symbol_master`)을 되돌리지 않아 충돌했다.

**리비전 ID·순서·DDL은 건드리지 않았다.** 운영 `alembic_version`이 계산되지 않게 되는
위험이 그쪽에 있다. 수정 파일은 테스트 4개(`paper_evaluation/test_migration.py`,
`order_proposals/callback_inbox/test_migration_chain.py`,
`extensions/kasset/test_multi_user_migration_guards.py`, `paper_cohort/test_migration.py`).
`test_alembic_reports_exactly_one_head`는 하드코딩된 낡은 `HEAD_REVISION`을 쓰고 있었고
`ScriptDirectory`가 보고하는 head와 대조하도록 바꿨다.

### 4. `taskiq-smoke` shard exact-cover

`ci_shards/shard-*.txt`에 없던 15개 파일(이번에 추가한 kasset/nhplug 테스트)을 가장 가벼운
shard에 정렬 위치로 삽입했다. shard 크기 `468/446/446/445`, 총 1805 유일.
`file_shard_plan generate`는 **돌리지 않았다** — 전체 재배치로 무관한 churn을 만들고,
Windows 수집 결손 때문에 POSIX 전용 39개 파일이 manifest에서 빠진다.

`docs/runbooks/ci-file-shard-manifests.md` §4.3을 신설해 Windows에서 `check`를 재현할 수
없는 이유와 대체 절차를 남겼다. 문서의 명령을 그대로 실행해 검증했다(누락 0, 중복 없음).
함정 3가지가 문서에 있다: `cmd.exe /c "..."`로 `-m "not live"` 전달 시 인자 분리, manifest
CRLF(`tr -d '\r'`), Windows Python 텍스트 모드 CRLF 변환(`newline="\n"`), `<(...)` 불가.

### 5. 배포

`d2e90eb6` 이미지 재빌드 후 `api worker scheduler mcp` 재생성. 서버 git은 `a83d0e7c`까지
동기화했고 그 델타는 `tests/` 한 파일이라 재빌드하지 않았다. api·mcp healthy.

서버 `remote.origin.fetch`가 `kasset-integration` 한 브랜치로 제한돼 있어 `origin/main`이
없었다. `+refs/heads/*:refs/remotes/origin/*`로 고쳤다 — 다음 배포에서 같은 곳에 걸리지 않는다.

### 6. 검증

```text
ruff check app/ tests/ research/ scripts/     → All checks passed!
ruff format --check (4303 files)              → already formatted
ty check app/ --error-on-warning              → All checks passed!
pytest tests/infra/test_database_guard_completeness.py
     + tests/extensions/kasset/test_multi_user_migration_guards.py → 2 passed
CI run 33194653480                            → 전 잡 success
```

## 직전 세션 기록 (2026-08-29 저녁)

### 0. 요약 — 시세 지연을 REST 폴링에서 WS 푸시로 바꿨다

서버측은 완료·배포·라이브 검증까지 끝났다. **앱 WS 클라이언트는 별도 슬라이스**이며
그것이 붙기 전까지 사용자 체감 지연은 그대로다.

운영 라이브 측정(`wss://175-45-201-51.sslip.io/api/v1/market/stream`, 미국 dayMarket):

| 항목 | 값 |
|---|---|
| 60초 홀드 프레임 | `TOSS_API_WS` **243건** vs REST baseline 2건 |
| AAPL | 214건 수신, 가격 변화 85회 |
| 지연(공급자 `asOf`→수신, n=415) | median **1117ms**, p95 1290ms, min 353ms |
| 도착 간격 | median 0.6ms(버스트), p95 789ms |

기존 REST 경로는 2초 폴링 + 서버 2초 캐시라 체감 2~4초였다. 재현 스크립트는
`E:/tmp/ws_live_hold.py`, `E:/tmp/ws_latency.py`(커밋 대상 아님).

### 1. 실시간 시세 스트림 (`app/extensions/kasset/api/stream/`, 9개 신규 모듈)

`GET /api/v1/market/stream` WebSocket. 상향 Toss WS는 **Redis 리스로 단일 소유자**를
선출해 전역 1개만 유지한다. 명세상 동시 연결이 계정당 2개이고 초과 시 가장 오래된
연결이 서버에 의해 조용히 종료되므로, 프로세스별 연결은 서로를 죽이며 무한 재연결
루프가 된다. 선택이 아니라 필수다.

- 구독은 상향과 동일한 **선언형 full-replace**. `unsubscribe` verb가 없어 놓친 해제로
  예산이 새는 경로가 구조적으로 없다. 연결이 끊기면 그 연결의 구독분을 회수한다.
- 100건 예산(채널×종목) 초과분은 조용히 누락되지 않고 `status.pollingTopics`로 강등
  통보한다. 불변식 `streaming ∪ demoted == 요청 전체`를 테스트로 고정했다.
- keepalive는 명세대로 **평문 `PING` 60초**. 서버 송신은 idle 타이머를 리셋하지 않으므로
  시세가 쏟아지는 중에도 보내야 한다(서버 idle 한도 180초).
- 배포 표면 변경 0: 새 서비스·이미지·환경변수 없음. Caddy는 `reverse_proxy`가 Upgrade를
  자동으로 나르므로 기능 변경 없이 handshake 101이 실측 확인됐다(주석 7줄만 추가).

### 2. 시장지표 12종 + 지수 3종 (`market_overview.py`, `schemas.py`)

`MarketOverviewResponse.indicators[]` 신설. VIX, US10Y, 국고채 6종, WTI/BRENT/GOLD, BTC.
지수에 DJI/RUT/SOX 추가. US 지표는 기존 지수 배치에 합쳐 왕복을 늘리지 않았고 Upbit·Toss는
기존 소스와 병렬이다. 국고채는 전일종가 소스가 없어 등락을 `null`로 두고 위조하지 않았다.

### 3. 와이어 정밀도 — 앱이 `7767.33984375`를 렌더하고 있었다

`_decimal_text()`가 `Decimal(str(value))` 후 양자화를 하지 않아 yfinance float32 잔재와
FX Decimal 나눗셈 결과(`8.671256687620105017577911603`, 28자리)가 그대로 나갔다.
Naver·Toss 문자열 공급자만 우연히 무사했다. **소수 최대 2자리**로 양자화한다(KRW는 정수).
`Decimal.normalize()`는 큰 정수를 `1E+8`로 바꿔 와이어 정규식을 깨므로 쓰지 않는다.

### 4. CI가 kasset 테스트를 한 번도 돌리지 않고 있었다

CI는 `ci_shards/shard-N.txt` 매니페스트에서 파일 목록을 읽고, 그 매니페스트는
`pytest --collect-only` 결과를 기준 집합으로 쓴다. `tests/extensions/kasset/api/`가
패키지가 아니어서 `test_candles.py`·`test_market_stream.py`가
`tests/brokers/kis/mock_scalping_ws/`의 동명 파일과 모듈명 충돌(`import file mismatch`)을
일으켜 **수집 자체가 실패**했고, 수집되지 않은 파일은 매니페스트에 들어가지 못해
영구히 실행되지 않았다. 실제로 kasset 시세·세션·호가 테스트 11개가 미실행이었다.
`tests/extensions/kasset/api/__init__.py`를 추가해 해결했다(`tests/services/brokers/toss/`가
같은 이유로 이미 두고 있던 관례).

**남은 조치**: 이제 수집되므로 shard 매니페스트를 재생성해야 CI가 실제로 돌린다.
Windows에서 재생성하면 안 된다 — `tests/scripts/b0x/*`가 POSIX 전용 `fcntl`로,
`tests/research/*`가 CRLF 민감 frozen SHA 가드로 로컬에서만 실패해 기준 집합이 좁아지고
오히려 테스트를 CI에서 빼버린다. Linux에서 `test-durations-refresh.yml`로 재생성할 것.

### 5. QA 토큰 자동 갱신 (`scripts/`)

access 30분·refresh 7일이고 갱신마다 refresh가 회전한다. SSH 1회로 쌍을 받고 이후
7일간 순수 HTTP로 무한 갱신한다. `mint_android_qa_token.py`는 claim을 손으로 조립하지
않고 운영 경로 `MobileAuthService._issue`를 호출하므로 게이트가 바뀌어도 조용히 401이
되지 않는다. `device_id=qa-cli`라 실기기 세션을 빼앗지 않는다.

```bash
TOK=$(python scripts/kasset_qa_token.py)   # 이 한 줄로 끝
```

### 6. 확정한 외부 계약 사실 (추측 아님, 명세·실측)

- **Toss는 미국도 된다.** 공식 소개문 "Korean (KRX) and US stock market data".
  `/api/v1/prices`가 **KR+US 혼합 배치**를 받는다(005930·000660·TQQQ·AAPL 동시 응답 확인).
  미국 개별 종목 `source=TOSS_API_PRICES`.
- US 4세션: dayMarket 09:00–17:00 / preMarket 17:00–22:30 / regularMarket 22:30–05:00 /
  afterMarket 05:00–08:50 (KST). 10분 빼고 사실상 24시간이다.
- 공식 레이트리밋: `MARKET_DATA` 15, `MARKET_DATA_CHART` 20, `MARKET_INDICATOR` 10,
  `MARKET_INDICATOR_CHART` 5. 우리 로컬 `_BASE_LIMITS`의 `MARKET_DATA` 10 /
  `MARKET_DATA_CHART` 5는 과도하게 보수적이다(미조정, 차트 처리량 여유 있음).
- WS: 동시 연결 계정당 2개(초과 시 최오래 연결 종료), 연결당 구독 100건(채널×종목),
  선언 빈도 5회/초, `trade`·`orderbook`은 LOSSY / `personal:order`는 LOSSLESS.

### 7. 검수

`b16f9261..71e442db` 고정 Diff에 `checker` 1회 → **REWORK**(MAJOR 2건).
① `ping_interval=None`으로 dead-peer 감지를 끈 상태에서 수동 `PING` 무응답을 추적하지
않아 half-open TCP에서 소유자가 리스를 쥔 채 시세가 무기한 정지하고 `DEGRADED` 통보조차
안 갔다. ② 느린 소비자 종료가 클라이언트의 다음 프레임 수신 뒤에만 감지되어 수신 전용
백그라운드 클라이언트가 좀비로 잔존했다. 둘 다 실제 경쟁 조건 재현 테스트와 함께 수정
→ 독립 재검토 **PASS**.

## 알려진 미해결

- **결정(2026-08-29 심야) — WS 스트림 범위는 `quote:` + `orderbook:` 두 종류로 확정하고
  `personal:order`는 LIVE 개방까지 보류한다.** 지금 붙여도 전달할 것이 없다: PAPER 체결은
  제출 응답 안에서 동기 확정되고(`app/extensions/kasset/api/paper_orders.py:271-285`) OPEN
  주문을 나중에 채우는 스윕이 `app/tasks/`에 없다. LIVE도 닫혀 있다. 게다가
  `personal:order`는 LOSSLESS라 conflation을 적용하면 안 되고(2초 이상 막히면 서버가 연결
  종료), 재연결 뒤 `GET /api/v1/orders` 재동기화가 필수다. 전달 보장이 시세와 정반대여서
  같은 소켓 상태기계에 얹으면 conflation이 체결을 삼킨다. LIVE를 열 때 별도 슬라이스로 설계한다.
- Toss `dayMarket`(09:00–17:00 KST) 구간을 우리 세션 모델이 `CLOSED`로 표시한다. Toss는
  그 시간 미국 거래를 허용한다. 5번째 상태 추가는 스키마·앱 변경이 필요해 미착수.
- LIVE 주문은 아직 닫혀 있다(`LIVE_TRADING_ENABLED=false`, `NHPLUG_MOCK_ENABLED=true`).
  개방은 사용자 결정 사항이다.

해소됨(이 세션): 앱 WS 클라이언트 구현·실기기 확인, `test_multi_user_migration_guards.py`
alembic 충돌, `ruff format --check` 기존 실패 11건, CI shard 1 실패.


### 1. 시세 지연의 실제 원인 제거 — Cloudflare 우회

집→서버 RTT가 **280~930ms**였다. 원인은 Cloudflare가 한국 트래픽을 **LAX**로
우회시킨 것(`CF-RAY: …-LAX`, free tier). 오리진에 직접 TLS 엣지를 세워 컷오버했다.

- `docker-compose.kasset.yml`에 `caddy` 서비스 추가(기존 `deploy/kasset/Caddyfile` 재사용),
  `KASSET_DOMAIN=175-45-201-51.sslip.io`로 Let's Encrypt 자동 발급.
- Android 기본 URL을 `https://175-45-201-51.sslip.io`로 컷오버(앱 `29e43dc4`).
- 터널 삭제 후 `cloudflared` 서비스 제거. `hsps-portal.xyz` zone **무접촉**(ERP `erp` 302 /
  `service` 200 기준선 동일 확인).
- 실측: TLS 핸드셰이크 **585~983ms → 27~67ms**, 앱 요청 서버 처리 6~11ms.
  15초 폴링 지속 관측 `/market/overview` 평균 367ms, `/market/quotes` 137ms.
- **도메인은 사지 않는다**(사용자 결정). sslip.io가 곧 공인 IP이고 인증서·DNS 무료다.
  생 IP는 TLS 불가(실측: IP SAN 인증서 없음 → 핸드셰이크 실패, 앱도 `BaseUrl.kt:41`에서
  `http://` 거부). VPS 이전 시 `KASSET_DOMAIN`과 앱 기본 URL 두 줄만 바꾸면 된다.

### 2. 호가 401 — Android 토큰 허용목록 누락

NH 호가 WS 수집과 라우트가 모두 정상인데 앱의 1초 호가 폴링이 **전부 401**이었다.
`app/extensions/kasset/api/paths.py`의 `_EXACT_PATHS`에 `/api/v1/market/orderbook`이
없어 `AuthMiddleware` → `get_current_user`가 막았다. 기존 `test_orderbook.py`는
미들웨어 없는 bare FastAPI + `dependency_overrides`라 구조적으로 못 잡았다.

- 허용목록에 추가하고, **설치된 라우트 전수**를 검사하는 계약 테스트를 신설
  (`tests/extensions/kasset/api/test_token_path_allowlist.py`, 4 passed).
- 실측: 배포 전 프로브 `android routes: 35 | NOT allowed: 1 | MISSING: /api/v1/market/orderbook`
  → 배포 후 앱 1초 폴링 19회 **전부 200**.

### 3. "일부 시세가 안 불러와짐" — 근본 원인 2건

`kr_symbol_universe`가 **0행**이라 `candles.daily.kr.sync`가 매일 대상 0건으로 공전했고,
저장 일봉은 수동 시드된 3종목(005930/000660/035420)뿐이었다. 그 결과:

| 결함 | 증상 | 수정 |
|---|---|---|
| D1 | 관심종목에 새로 넣은 KRX 종목의 `previousClose`가 `null` → 등락률 `-`, 차트 빈 배열 | 읽기 경로에 Toss 일봉 폴백 |
| D2 | 미국 종목이 `PAPER_YAHOO`로 내려가 **한 세션 지연**된 종가를 현재가로 표시, `changeRate` 미양자화(27자리) | 미국을 Toss 경로로 |

- `toss_market_data.py`: `previous_closes()`(거래일 단위 캐시, 종목당 하루 1회, 실패 60초
  음성 캐시, 동시 4개 제한), `daily_bars()`(60초 캐시). 당일 봉을 전일 종가로 쓰지 않는다.
- `krx_quotes.py`: 시장을 인자로 받아 미국 티커를 Toss `prices`로 해석. 응답 `market`·통화를
  시장에 맞추고 일봉 파티션은 기존 `_market_route`를 재사용. NH 폴백은 KRX에만 유지.
- `router.py`: `/market/quote` 미국 분기를 Toss로, `/market/candles`는 저장 일봉이
  **비었을 때만** Toss로 폴백(저장 우선 규칙 유지).
- `daily_candles/sync_service.py`: `_resolve_universe`에 **활성 관심종목 합집합**을 추가.
  거래소 행 없는 미국 종목은 읽기 경로가 조회하는 `NASD` 파티션 기본값.
- 실측(프로덕션): 005380 `None/None → 401000 / -0.62`, 035720 `→ 36000 / 2.36`,
  000270 `→ 127100 / 0.39`, TQQQ `73.30000305175781 / 4.0158958167… / PAPER_YAHOO`
  → `73.06 / -0.33 / USD / TOSS_API_PRICES`. 차트 005380·035720·TQQQ 전부 5행 복구,
  005930은 저장 경로 유지. 일봉 동기화 대상 **0건 → 3건**, `sync_one`으로 005380
  60행 기록 확인.

### 4. 실시간 소스 확정 — Toss 단독

사용자 요구: US 프리/정규/애프터 + KR 정규장/NXT **5개 세션 전부 실시간**.

- **Toss가 전부 커버**한다. AsyncAPI 3.0.0 v1.2.2(`/openapi-docs/latest/asyncapi.json`)
  원문: "푸시는 모든 세션에서 제공됩니다. 미국은 프리·정규·애프터·데이마켓, 국내는
  KRX 정규장과 NXT 프리·정규·애프터마켓 합산입니다." 토픽은 `trade:us` `orderbook:us`
  `trade:kr` `orderbook:kr` `personal:order` 5개. 상한 **계정당 2연결 × 연결당 100구독**,
  선언 5회/초, 180초 무송신 종료(60초 PING), 구독 직후 스냅샷 없음(REST 선조회 필요),
  시세 LOSSY·주문이벤트 LOSSLESS.
- **우리 운영 키로 실측 성공**: `subscribed:[trade:us:TQQQ, …, orderbook:us:TQQQ,
  trade:kr:005930] rejected:[]`, 미국 프리마켓 체결·호가 프레임 2초에 10건 수신.
  → 미국 실시간 **유료 약정 불필요**(스펙에도 언급 0건).
- **NH는 구조적으로 불가**: WS 상한 2 tr_cd × 30종목이라 KRX(`ob`+`oc`) + NXT(`nb`+`nc`)
  4채널을 못 붙인다. 통합 채널 `mb`/`mc`는 `oc`와 24필드가 완전 동일해 **시장 식별
  필드가 없어** KRX/NXT 분리 불가. 미국은 `RH`/`RC`가 "유료시세 사용 약정 고객만"이고
  무약정은 지연(`rh`/`rc`).
- **Toss에 모의투자·가상계좌는 없다**(실측: openapi/asyncapi `servers`가 운영 단 하나,
  두 스펙 전문에서 모의/샌드박스/mock/virtual/가상 **매치 0건**, `accountType`은 실계좌
  enum만). 따라서 Toss 주문은 곧 실제 돈이고, **서버 PAPER 시뮬레이션이 유일한 안전
  리허설 경로**로 남는다. NH 코드는 삭제하지 않고 미배선 휴면 어댑터로 둔다.
- `market_calendar.py`에 세션 판별기(`KrTossSession`/`UsTossSession`)가 **이미 구현**돼 있다.

### 검증

- `tests/extensions/kasset/api/` 117 passed, `tests/extensions/kasset/` 248 passed,
  `tests/unit/services/daily_candles/test_sync_service.py` 13 passed,
  신설 `test_market_quote_coverage.py` 7 passed. ruff format·check clean.
- **기존 실패 1건(내 변경과 무관)**: `tests/extensions/kasset/test_multi_user_migration_guards.py`
  `DuplicateColumnError: column "aliases" of relation "instruments" already exists`.
  변경분을 `git stash`한 원본 HEAD에서도 동일 재현 확인.
- 테스트 DB는 `AUTO_TRADER_TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5434/test_db`
  (기본값 5432는 이 PC에서 안 뜬다).
- 앱 화면 시각 확인은 **미완**: 폰 무선 디버깅 포트가 세션 중 끊겼다(`100.90.45.34:46079`
  connection refused). API 계약은 앱과 동일한 요청·토큰으로 프로덕션 실측 완료.

### 알려진 잔여 결함 (이번에 고치지 않음)

- `DailyCandlesRepository.upsert_rows`가 `result.rowcount`를 반환하는데 asyncpg는
  executemany에서 의미 있는 값을 주지 않아 **항상 0**이다. 005380 60행을 실제로 쓰고도
  `rows_upserted: 0`을 보고했다. 데이터는 정상이고 지표만 틀렸지만, 이 지표 때문에
  유니버스 0행 결함이 오래 보이지 않았다.
- `orderbook_store.py:_require_configuration()`이 `mock_enabled()`를 요구하면서 **운영 WS**
  (`api.nhplug.com:7070`)에 접속한다. `NHPLUG_MOCK_ENABLED=false`로 실전 전환하는 순간
  실시간 호가가 409 `BROKER_NOT_CONNECTED`로 죽는다.
- `docs/API-CONTRACT.md:51`이 모의 WS(`moapi:17070`)를 정본으로 기술 — 코드는 운영 `:7070`.
- 미국 `orderbook:us`는 **1단계(최우선 호가)만** 준다. 10단 사다리는 KRX REST에서만 가능.

## 직전 세션 기록 (2026-08-28 저녁)

- 시장 데이터 표면 3종(`/market/overview` 15초 캐시, `/market/indices/{symbol}`,
  `/market/quotes` 50종목 배치) + Toss 실시간 시세 1순위 폴백 체인.
- 세션 토큰 계약 수정: access token `sid`를 `refresh_token_hash`와 비교하던 게이트 제거
  (refresh 직후 이전 토큰이 401이 되어 로그인 화면으로 튕겼다).
- 검증: 집중 스위트 통과, `checker` 1회 PASS(`0b1650f2`), 운영 재배포 후 Toss 실시세 실측.

## 직전 세션 기록 (2026-08-28 심야)

- **Cloudflare Tunnel 컷오버**: 신규 터널 `kasset-trader`(id 75836947-…)로
  `https://api.hsps-portal.xyz` 공개(HANSE_ERP 터널 무접촉). compose에 `cloudflared`
  추가(`--protocol http2` — Naver가 UDP 7844 차단), `TUNNEL_TOKEN`은 `.env.kasset`.
  Android 기본 URL을 터널 도메인으로 컷오버(앱 커밋 `ae69d587`)하고 sslip.io Caddy
  edge 제거(`2206aa14`, 443 refused 확인). 대시보드 mutating API는 `x-atok` 헤더 필요.
- **VPS 이전 런북** `docs/runbooks/vps-migration.md`: 상태 인벤토리, Toss/NH 허용 IP
  갱신 필요성, 단계별 이전 명령. compose 스택은 저장소로 편입(`18203413`).
- **DB 백업/복원**: `/etc/cron.d/kasset-db-backup` 매일 03:30 KST pg_dump(7일 보존,
  실행 검증) + `/usr/local/sbin/kasset-db-restore.sh`(사본 `deploy/`).
- **캔들 API**: `GET /api/v1/market/candles`(count≤120 클램프, 문자열 OHLCV 오름차순,
  NXT→NTX) 배포(`dbbd7a50`), 55 passed.
- **스캔 쿨다운·cron 정렬**(`619cb807`): 유효 PENDING 추천 있으면 재분석 skip,
  스캔은 KST 평일 9-16시 매시 :10(기존 UTC */15의 야간 공회전 제거).
- **Android 리디자인 진행 중**: 정본은 KAsset-Trader `docs/design/redesign-spec.md`
  + `docs/design/stitch/*.html`(KR Pro Style 11장, 커밋 `7a517356`). 종목 로고는
  Toss CDN `icn-sec-fill-{symbol}.png` + 이니셜 폴백. 주문 UI는 PAPER 전용으로
  계약 개정(`10dbf2f2`).

- **관심종목 API** (`app/extensions/kasset/api/watchlist.py`, 커밋 `524728ac`):
  owner-scoped GET/POST/DELETE `/api/v1/watchlist` + 활성 instrument 부분검색.
  재등록 idempotent, 삭제는 soft(is_active=false).
- **다중 trader 스캔**: `kasset_market_events.run`이 활성 trader 전원을 독립 순회
  (한 owner 실패가 다른 owner를 중단시키지 않음). 서버 실검증: user 1(0종목),
  user 4(3종목 스캔, 캔들 부재로 insufficient_data 정직 skip).
- **스캔 인프라 가동(서버)**: compose에 worker/scheduler 추가 —
  `taskiq worker|scheduler ... app.tasks.kasset_market_events_tasks`(모듈 한정 로드).
  `.env.kasset`에 `KASSET_MARKET_EVENTS_ENABLED=true`. KRX exchange +
  005930/000660/035420 instruments + user 4 watch items 시드 완료.
- **관심종목 일봉 수집 태스크** (`kasset_watchlist_candles.sync`, 커밋 `b21bcc1c`):
  KST 평일 9-16시 매시 5분, Toss 일봉 60개 → kr_candles_1d(source='toss') upsert,
  종목별 실패 격리. 사용자가 Toss Open API 키 발급(허용 IP `175.45.201.51`),
  `.env.kasset`에 `TOSS_API_ENABLED/CLIENT_ID/CLIENT_SECRET` 반영 완료.
  실검증: 3종목 × 60일 캔들 적재(2026-06-04~08-28) → 스캔 실행 시 3단 라우터가
  실지표로 판정(035420 HOLD/0.84/sol, 005930 HOLD/0.70/sol, 000660 IGNORE/0.84/luna).
  HOLD/IGNORE는 설계대로 미저장(BUY/SELL·conf≥0.60만 저장). NH PLUG는
  계좌/잔고/현재가 3경로 고정이라 차트 불가(대안 아님).
- **warm MCP 서비스**: compose `mcp` 서비스(auto-trader-mcp, analysis_readonly,
  streamable-http :8768, 토큰 `MCP_ANALYSIS_AUTH_TOKEN`) + 호스트 127.0.0.1:8768
  노출, `/root/.codex/config.toml`에 등록(`/root/.codex/env.sh` source 필요).
  **헤드리스 `codex exec`는 MCP 도구 승인을 우회할 수 없음**(openai/codex#24135;
  유일 우회는 `--dangerously-bypass...`라 프롬프트 인젝션 시 셸 위험 → 미채택).
  구독 브리지는 evidence-in-prompt 방식 유지, MCP는 대화형 codex/타 에이전트용.

## 이번 세션에서 한 일 (2026-08-28)

- **구독형 AI 브리지 가동 (2026-08-28)**: 서버 호스트에 Codex CLI 0.150.1 설치,
  ChatGPT 구독으로 로그인(auth는 `/root/.codex` + 컨테이너용 사본 `/opt/kasset-codex`,
  uid 10001). api 컨테이너에 codex 바이너리·`CODEX_HOME=/var/lib/kasset-codex` 마운트,
  `.env.kasset`에 `KASSET_AI_SUBSCRIPTION_CMD=codex exec --skip-git-repo-check
  --sandbox read-only -`. `subscription_cli.py` invoker(커밋 `19797c21`)가 stdin으로
  계약을 주고 마지막 JSON을 SkillResult로 검증, 실패는 전부 AiProviderUnavailable로
  API 티어 폴스루. 실검증: 컨테이너 내 codex JSON 응답 + API/OR 키 비운 구독 단독
  run_skill 성공(HOLD/0.62). run_skill 경로는 이제 구독→API(gpt-5.6)→OpenRouter 순.
  다음 후보: codex config에 Core `app/mcp_server` 도구(stdio) 등록.
- **서버 SSH가 Tailscale 전용으로 바뀜 (2026-08-28)**: 접속은
  `ssh -i <PEM> root@100.73.186.78` (tailnet `vm-naver-kasset`, 계정 gim47656@).
  공인 `175.45.201.51:22`는 nftables `ssh_guard` 테이블이 차단한다 — 허용은
  lo / tailscale0 / 웹터미널 게이트웨이 `180.210.76.21`뿐. 규칙 파일
  `/etc/nftables/kasset-ssh-guard.nft`(+ sysconfig include), `nftables`·`tailscaled`
  둘 다 enable. 80/443(앱 API)은 계속 공개. 비상 복구는 모두의 AI 실험실 웹터미널.
  주의: 사무실 PC는 Tailscale 설치 전까지 SSH 불가.
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

현재 브랜치: `main` `a83d0e7c` (origin/main 동일). 서버도 동일 커밋.

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

1. 장중(KRX 09:00–15:30 KST)에 KRX `range=1D`가 실제 분봉을 채우는지 확인한다. 지금은
   마감이라 빈 배열 경로만 확인됐다(미국 종목으로는 235봉 실측 완료).
   `curl -s -H "Authorization: Bearer $TOK" "$B/market/candles?market=KRX&symbol=005930&range=1D"`
2. LIVE 주문 개방 여부를 사용자와 확정한다. 켜기 전 `NHPLUG_MOCK_ENABLED=false`와 실계좌
   자격증명, kill switch 동작을 순서대로 확인해야 한다.
3. `personal:order` WS 채널은 시세와 전달 보장이 반대이므로 별도 슬라이스로 설계한다.
4. 빈 Linux 호스트 복원 리허설은 대상 호스트 확보 후 `scripts/kasset_backup.sh`/`restore`로 진행.

남은 기술 위험:

- mobile JWT와 Core JWT가 같은 `SECRET_KEY`를 공유한다. 경로 스코프로 차단했지만
  audience claim 분리가 더 강한 후속 개선이다.
- 자동화 producer는 라이브러리+테스트로 존재하며 외부 AI 파이프라인이 추천 POST API로
  공급하는 구조다. producer의 스케줄 배선은 별도 제품 결정이 필요하다.
- 진짜 부분체결 도입 시 PAPER correlation 조회 `scalar_one_or_none()`의
  `MultipleResultsFound` 가능성은 여전하다.


## 세션 이력

- 2026-08-29 심야: `range=1D` 분봉 서빙(상향 엔드포인트 실측 정정, 정규장 필터, 빈 분봉 일봉 대체 금지) 배포, CI shard 1의 run-owned DB guard 누락 수리로 **전 잡 초록**(run 33194653480), 마이그레이션 테스트 프라이밍 수리(리비전 무변경), shard exact-cover와 런북 §4.3 신설.
- 2026-08-29: WS 실시간 스트림 신설(Redis 리스 단일 소유자, 선언형 full-replace, 라이브 지연 median 1117ms·60초 243틱), 시장지표 12종·지수 3종 추가, 와이어 소수점 2자리 양자화, CI가 kasset 테스트 11개를 수집 실패로 한 번도 돌리지 않던 공백 수리, QA 토큰 7일 무한 갱신 도구. checker REWORK(MAJOR 2건) → 수정 후 재검토 PASS. 소비자 범위 1801 passed.
- 2026-08-28 밤: Cloudflare LAX 우회 제거(오리진 Caddy TLS, RTT 585~983ms→27~67ms), 호가 401 허용목록 수정, 저장 일봉 밖 종목의 시세·차트 복구(Toss 일봉 폴백 + 관심종목 유니버스 합집합), 미국 시세를 Yahoo→Toss로 전환, 실시간 소스 Toss 단독 확정(5세션 실측).
- 2026-08-28 저녁: 시장 개요·지수 상세·배치 시세 API 신설과 Toss 실시간 시세 연결, access token sid 게이트 제거로 세션 유지 수정, checker PASS 후 운영 배포.
- 2026-08-27: 다중 사용자 컷오버·AI PAPER 자동화·배포 매니페스트를 실제 PostgreSQL로 검증하고 checker PASS로 종결.
