# HANDOFF — KAsset-Trader-Core
갱신: 2026-09-03 (KRW/USD 정산 장부 분리로 같은 owner·계좌에서 KRX·US 자동주문 동시 허용, 자동주문 표시·체결 푸시 WIP 검증 완료·PR #43)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 운영 broker 범위는 KR/US 실계좌·주문·체결의 Toss와 KR mock read-only 조회의 NH PLUG이며, KIS 미설정은 의도된 상태다. 역사 KIS ledger/read model은 보존하되 production runtime에는 연결하지 않는다. owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인 hash, 주문 idempotency, accepted-only ledger와 broker evidence fill을 보존하고 검증 목적으로 주문을 만들지 않는다.

## 2026-09-03 밤 — KRW/USD 정산 장부 분리 (PR #43 뒤 후속 PR, 배포 직전)
- 증상: owner 4는 `AUTO_PAPER`·kill switch off·promotion bypass on인데 미국장 자동주문이 구조적으로 불가능했다. 원인은 `AITradingLimits.currency` 하나가 시장 관문 역할을 겸한 것이다 — `evaluate_hard_risk`의 `expected_market = "KRX" if limits.currency == "KRW" else "US"`가 US 주문을 `POSITION` 실패로 떨어뜨리고, `usage()`가 KRW·USD 원가·손익·주문수를 환산 없이 합산했다. 같은 PAPER 계좌에 `cash_krw`와 `cash_usd`가 나란히 있으므로 계좌·owner 분리는 해법이 아니다.
- 수정(`app/extensions/kasset/automation/policy.py`, `vertical_slice.py::_pre_ai_sizing`, `api/router.py`, `schemas/ai_recommendations.py`): 주문 `market`이 정산 장부를 결정한다(`settlement_book`: `KRX→KRW→equity_kr`, `US→USD→equity_us`, 그 외 fail-closed). 장부마다 `operating_budget_krw`(기본 10,000,000원)·`operating_budget_usd`(기본 10,000 USD, 사용자 결정)를 갖고, BUDGET·DAILY_MAX_LOSS·POSITION·ORDER_COUNT·같은 종목 재진입·동시 보유가 장부별로 독립 평가된다. 거래일 경계도 장부별(KRW=KST, USD=America/New_York 자정)이다. `usage()`는 `AndroidPaperOrder.currency`, `PaperPosition.instrument_type`, `PaperTrade.currency`로 필터하고, snapshot은 `usage_by_currency`(두 장부)와 표시 장부 `usage`를 함께 든다. 포지션 조회에 `instrument_type`이 추가돼 교차시장 동일 심볼 충돌이 없다. FX 환산은 의도적으로 없다.
- `currency` 설정은 이제 앱의 표시·편집 선택자일 뿐 시장을 막지 않는다. PUT `operatingBudget`은 요청 `currency` 장부에만 적용되고 반대 장부는 저장값을 보존한다(병합은 `put_snapshot` 안). 저장 canonical 키는 `operating_budget_krw`/`operating_budget_usd`이며 legacy `operating_budget`은 읽기만 한다(owner 4 행 `currency=KRW, operating_budget=5000000` → KRW 5,000,000·USD 10,000). 응답 `settings`에 `operatingBudgetKrw`/`operatingBudgetUsd`가 추가됐고 PUT 요청 스키마(`extra="forbid"`)는 불변이라 현재 Android와 호환된다. **앱에는 아직 USD 예산 입력칸이 없어 USD 장부는 기본 10,000 USD로 돈다** — 앱 후속 작업.
- daily routine `recommendation_market_scope`(기본 `KR_US`)는 후보 시장 선택만 담당하며 이번 변경과 무관하게 유지된다. Position Manager 청산은 보유 포지션의 시장 장부로 평가되므로 KRW 표시 owner의 US 포지션 SELL도 통과한다.
- 검증: focused pytest 9파일 `196 passed`, `ruff check`/`format --check` 통과, `tests/extensions/kasset` + `tests/schemas/test_ai_recommendations_schema.py` 전체 `1155 passed, 2 warnings in 93.96s`(로컬 PostgreSQL 16 run-owned DB). 독립 `checker` 1회(`integration-risk`): **PASS, CRITICAL/MAJOR/MINOR 0**. checker가 남긴 참고: `get_snapshot`이 두 장부 usage를 매번 계산해 Hard Risk 1회당 SQL 20회(기존 13회)로 owner 1명 규모에서는 문제없지만 다중 owner 확장 시 metadata-only 읽기 분리가 필요하다.
- 자동주문 표시·체결 푸시 WIP(사무실 작업)는 이 PC에서 재검증했다: `ruff check` 21파일 통과, `format --check` 통과, focused pytest 10파일 `225 passed`, 신규 테스트 shard 등록 확인. PR #43 `feat/order-name-and-push`(6d053a64)로 발행했고 이 장부 분리 커밋은 그 위에 쌓인 `feat/multi-currency-hard-risk`다.
- 운영 참고: owner 4는 2026-09-03 22:06 KST에 앱에서 `AUTO_PAPER`로 전환됐다(`user_settings.updated_at 13:06:41Z`). 배포 전 코드에서는 US 주문이 여전히 `expectedMarket=KRX`로 차단되므로, 이 PR 배포 뒤 첫 미국장 sweep부터 USD 장부(10,000 USD, 위험 4단계: 종목당 25%=2,500 USD, 매수 5건/매도 3건)로 자동주문이 가능해진다.

배포 절차(사용자 승인: 검증 통과 즉시, 미장 중이라도):
1. PR #43 → 후속 PR 순서로 required checks 통과 후 merge. migration 없음.
2. 서버 `/opt/kasset-trader-core`: `git fetch origin && git checkout main && git pull --ff-only`; `SHA=$(git rev-parse HEAD)`.
3. DB full custom archive를 `backups/pre-multi-currency-<UTC>/database.dump`에 저장하고 `pg_restore --list`로 검증.
4. `.env.kasset`을 `.env.kasset.bak-predeploy-<UTC>`로 백업한 뒤 `CORE_IMAGE_TAG`와 `VCS_REF`를 `$SHA`로 갱신. `VCS_REF="$SHA" docker compose --env-file .env.kasset -f docker-compose.kasset.yml build api`.
5. `docker compose --env-file .env.kasset -f docker-compose.kasset.yml up -d api worker scheduler mcp` 뒤 **반드시** `--profile ai-mcp up -d ai-mcp`(profiles 서비스는 기본 `up -d`에 포함되지 않는다).
6. 5개 container `/app/.build-vcs-ref`가 `$SHA`이고 `/health`가 `ok`인지, owner 4 `GET /api/v1/ai/trading/state` 대신 운영 컨테이너 안에서 `AITradingPolicyService().get_snapshot(db, 4)`가 KRW 5,000,000·USD 10,000·`usage_by_currency` 두 키를 돌려주는지 읽기 전용으로 확인한다.

## 전체 진행 상태
- 운영 배포 이미지는 `kasset-trader-core:acf093d81e62c4bb2e41b5c4d3889dfd32972321`다(PR #41 merge, `main`). `api`, `worker`, `scheduler`, `mcp`, `ai-mcp` 5개 container의 `/app/.build-vcs-ref`가 모두 이 SHA이고 `/health`는 `ok`다. 서버 checkout도 `main` 같은 커밋이라 tree diff가 없다. migration head는 `20260903_kasset_rvol_shadow`다. 직전 이미지는 `d73a4e55`(PR #39 merge `1c74f3f2`)였다.
- 미국장 자동주문 0건의 실제 원인은 provider 장애가 아니라 `intraday_data._NAIVE_TIMEZONE`에 `"US"` 매핑이 없던 것이었다. Toss US 분봉은 ET-naive를 돌려주는데(`market_data/service.py::_to_contract_timestamp`가 `equity_us`를 America/New_York로 변환한 뒤 tz를 제거) 매핑 부재로 모든 미국 후보가 첫 봉에서 `intraday_timestamp_unusable`로 탈락했다. `"US": ZoneInfo("America/New_York")` 추가로 해소했다.
- PAPER Hard Risk의 `AI` 규칙은 `AI_SHADOW`(항상 통과, 근거만 기록)로 강등됐다. AI·뉴스·공시는 검증되지 않은 입력을 보므로 주문 veto를 갖지 않는다는 운영 결정이다. 차단은 kill switch, `DAILY_MAX_LOSS`, `BUDGET`, `POSITION`, `ORDER_COUNT`, stale quote, 거래시간, promotion, position sizing만 담당한다. `AI_SHADOW` detail에는 실제 관측 confidence가 남고(반대 0.72도 0으로 붕괴하지 않음) 부재·NaN·Infinity·파싱 실패만 0으로 기록된다.
- owner cycle 로그에 관문별 funnel이 추가됐다: `setup_selected`, `setup_statuses`, `setup_rejections`, `trigger_statuses`, `trigger_failures`, `pre_ai_exclusions`, `review_rejections`. 어느 관문이 몇 건을 죽였는지 로그만으로 분리된다.
- 배포 후 실측(2026-09-02 UTC): 개장 직후 14:01~14:30 cycle은 candidates 100 → ranked 84 → setup qualified 3(rejected 81, 최다 `no_breakout_family_direction` 53) → actionable 3 → `trigger_statuses={'unavailable': 3}`, `trigger_failures={'relative_volume_unavailable': 3, 'no_directional_trigger': 2~3, 'intraday_relative_strength_disagrees': 1}`. 14:40 cycle부터 `trigger_statuses={'not_triggered': 3}`, `relative_volume_not_confirmed`로 바뀌었다. 즉 RVOL은 완료 5분봉 13~16개(개장 후 65~80분)가 모여야 계산되는 워밍업 구조이며, 그 이후로는 데이터 결함 없이 순수 조건 미충족으로 판정된다. 주문은 여전히 0건이고 이는 정상 무주문이다.
- 승격(promotion) 레코드는 `review.kasset_strategy_promotions` 0건이다. 런타임 fingerprint는 `faacff97e2a877e8ef439bde0de72d0541ab3a6509a88fa254c826a493a4fd56`(source_commit `5a5f737f`)이며 `intraday_data.py`·`vertical_slice.py`가 `STRATEGY_CODE_PATHS`에 있어 이번 변경으로 회전했다. 레코드가 0건이라 fingerprint mismatch는 발생하지 않지만, `promotion_bypass_enabled`가 켜진 owner만 자동주문이 가능하다. 현재 owner 4 = `true`, owner 1·5 = `false`(= `strategy_promotion_required`로 차단). 자동화 sweep이 잡는 owner는 유효 추천이 있는 owner뿐이라 `owners=0`은 추천 0건의 결과다.
- **US 분봉 수집이 살아났다.** `sync_us_candles`의 대상 심볼이 `Toss US 보유 ∪ manual_holdings(US) ∪ 전체 user_watch_items의 equity_us`가 됐다. watchlist는 owner 스코프를 걸지 않는다 — 스케줄 job은 `user_id=1`로 돌지만 실제 관심종목은 user 4에 붙어 있어 owner 스코프를 걸면 대상이 다시 비기 때문이다(계좌 보유·수동 보유는 owner 스코프 유지). 배포 후 실측: 대상 7종목(AMD, GOOGL, MU, NVDA, SNDK, SOXL, TQQQ), `us_candles_1m` 2,520행(13:30Z~19:27Z), `us_candles_5m` 504행.
- Toss 계좌 스냅샷 실패가 더는 US 수집 전체를 중단시키지 않는다. 예외를 격리해 보유 기여만 비우고 나머지 소스로 진행하며 `holdings_snapshot_ok=False`를 반환한다. 배포 후 19:30Z 스케줄 실행에서 `Toss portfolio snapshot unavailable during US candle sync error_type=TimeoutError` 경고가 났지만 수집은 계속돼 행이 증가했다 — 이전 설계라면 개장 사이클 전체가 날아갔다.
- `public.kr_candles_1m`은 0행이지만 KR 1분봉은 `research.kr_candles_1m_toss`에 2,503,731행으로 적재된다(최신 2026-09-02 11:00Z). `invest_screener_snapshots`·`market_quote_snapshots`가 0행인 것은 후보를 tvscreener 실시간 호출로 조달하고 스냅샷을 저장하지 않는 설계 때문이다. 뉴스는 `news_articles` 7,600건(최신 2026-09-03 03:32).
- **정규장 밖 무인 집행을 차단했다.** 무인 sweep은 `_out_of_session_block_reason`으로 해당 시장 정규장 여부를 먼저 확인하고, 밖이면 `out_of_regular_session`으로 BLOCKED한다(캘린더로 시장을 증명할 수 없으면 `unsupported_session_market`). claim을 태우지 않으므로 다음 정규장 sweep이 같은 추천을 다시 잡는다. 사람이 화면을 보고 결정하는 수동 경로(`POST /orders`, `run_approved_recommendation_once`)는 이 관문을 쓰지 않는다.
  - 배경: PAPER 체결 시뮬레이터는 마지막 시세로 즉시 채우고, 기존 `_stale_quote_block_reason`은 **정규장 중에만** 작동했다. 그래서 장외에는 시간 관문이 전혀 없었고 아래 강제 재집행이 KR 장 마감 후(15:15 UTC = 00:15 KST)에 그대로 체결됐다. Hard Risk와 PAPER preview에도 거래시간 검사는 없다.
  - 기존 테스트 `test_after_hours_stale_reference_quote_is_not_blocked`가 "장외 체결 허용"을 계약으로 고정하고 있었다. 이를 `test_after_hours_sweep_is_blocked_out_of_regular_session`(장외 BLOCKED, 시세 조회 0회, claim 미소모)과 `test_in_session_sweep_still_places_the_order`(장중 SUBMITTED)로 교체했다. 시각 의존으로 깨진 `test_one_owner_failure_does_not_abort_the_remaining_owners`, `test_promotion_bypass_executes_unpromoted_recommendation_with_evidence`는 정규장 시각으로 옮기고 실시간 시세 mock을 심었다.
  - 배포 후 실측: `_out_of_session_block_reason(KRX 추천, now=2026-09-02T15:40Z)` → `out_of_regular_session`.
- **AI veto 제거 후 첫 PAPER 자동주문이 실제로 체결됐다(이후 원복함).** 사용자 승인 아래 과거 `risk_preview_rejected:AI`로 실패한 추천 `rec-393431b4`(owner 4, KRX 055550 BUY 4주)의 execution 상태를 초기화하고 `valid_until`을 30분 연장한 뒤, 임계값은 그대로 두고 정규 자동 sweep이 집행하게 했다. 결과: `sweep done owners=1 outcomes=[{status: 'SUBMITTED', reason: 'submitted', promotion_bypass_reason: 'promotion_bypassed_by_owner'}]`, 주문 `b436d623-adf0-4ffb-80e2-29c69668c9b3` `client_order_id=ai-rec:rec-393431b4...` MARKET BUY 4주 `FILLED` @110,400, `paper.paper_trades` id 3 체결 기록, 추천 `paper_execution_status=SUCCEEDED`. 같은 추천의 원장 이력이 `REJECTED risk_preview_rejected:AI` → `SUBMITTED submitted`로 남아 AI veto가 유일한 차단 원인이었음이 증명됐다.
- 그 강제 체결은 **KR 정규장 밖이라 실제 시장에서 성립할 수 없는 체결**이었으므로 사용자 지시로 원복했다: `paper.paper_trades` id 3, `paper.paper_positions` 055550, `public.kasset_paper_position_states` 행, 주문 `b436d623` 삭제, 계좌 3 현금 441,666.24 환불(9,558,199.135 → 9,999,865.375 = 직전 값 정확 복원), 추천은 `FAILED reverted:out_of_session_forced_execution`으로 되돌렸다. 감사 원장 `review.kasset_paper_execution_events`의 REJECTED/SUBMITTED 이력은 증거로 보존했다.
- 수동 경로 `run_approved_recommendation_once`는 `required_mode=APPROVAL`을 요구하므로 owner 4(`AUTO_PAPER`)에서는 `owner_opt_in_disabled`로 막힌다. 강제 집행은 mode 변경 없이 자동 sweep 경로를 쓰는 것이 맞다.
- 2026-09-02 13:30 UTC 1회 발생한 `portfolio snapshot owner did not complete`는 개장 순간 단발 timeout이었다. 실계좌 스냅샷은 직접 호출 0.47초 정상(positions 0)이고 Toss US 1분봉도 정상 수신한다.
- PR #37 merge `8f965aaf92fd3993083fcc3360785b9ffa710cac`를 운영 배포했다. `.env.kasset`의 `CORE_IMAGE_TAG`가 이 full SHA이고 `api`, `worker`, `scheduler`, `mcp`, `ai-mcp` 5개 container의 `/app/.build-vcs-ref`가 모두 일치한다. 같은 날 PR #29(MCP-only), #31(Hard Risk), #33·#34·#35(기준가) 배포도 포함한다.
- API, analysis MCP, AI MCP sidecar의 container 내부 `/health`가 모두 `ok`다. `api`, `mcp`, `ai-mcp`, `worker`, `scheduler`, `db`, `redis`, `caddy`가 기동 중이다.
- migration head는 `20260902_screener_toss_source`다. `20260902_toss_report_scopes`와 Toss screener source migration은 additive하게 적용됐다.
- 배포 전 DB full custom archive는 `backups/pre-toss-cutover-20260901T191938Z/database.dump`에 저장했다. SHA-256은 `2d3af1b360b6e9fc4658af924dccb46bd16d0b7faa448572049ee80c97dddafe`이며 `pg_restore --list`와 checksum 검증을 통과했다.
- 활성 KR/US live 계좌·주문·정정·취소·체결 조회는 Toss다. 기존 KIS 요청은 Toss로 우회하지 않고 broker I/O·원장 mutation 전에 `provider_unsupported`로 fail-closed한다.
- KR/US live 시세는 Toss 공용 채널만 사용하고 실패 시 저장 PAPER 일봉으로 강등한다. legacy `broker=NH` 시세 요청도 Android 호환을 위해 수용하되 NH PLUG를 호출하지 않는다. NH PLUG native bridge는 KR mock 계좌·잔고·보유 read-only allowlist만 사용하며 주문 기능이나 MCP mutation surface는 없다.
- Toss 주문은 owner scope, 최신 sellable preflight, approval hash, idempotency, accepted-only ledger와 broker evidence 기반 fill booking을 유지한다. Toss fill poller는 공용 `AsyncSessionLocal`을 사용하며 KIS session factory 의존이 없다.
- Upbit accepted limit order reconcile은 `market=crypto`, `broker=upbit`로 고정 복구됐다. equity/KIS 입력은 kernel·broker I/O 전에 fail-closed한다.
- 미국 장중 OHLCV는 Toss 분봉·집계 경로를 사용한다. 2026-09-01 19:40/19:50 UTC 정규장 cycle은 후보 100, rank 84, evaluated/actionable 3까지 정상 진행했고 `intraday_trigger_not_satisfied`로 무추천 종료했다. 배포 전 반복되던 KIS token·US candle sync·intraday provider 오류는 배포 후 관찰 로그에 없었다.
- 20:00 UTC 장 마감부터 AI recommendation cycle이 실행되지 않아 정규장 gate가 작동했다. 배포 이후 Toss live order 0건, execution fill 0건이다.
- PR #26 GitHub Actions run `33565440250`에서 lint, security, PostgreSQL migration, TaskIQ worker/scheduler smoke, test shard 1~4, intraday, Alpaca, frontend, `ci-required`가 모두 성공했다. 최종 integration-risk 독립 검토는 `PASS`다.

- AI 호출은 전 lane이 MCP-only다. DB `kasset_ai_runtime_config` `revision=10`: `review_luna/terra/sol=["mcp_tool"]`, `summary_luna=["mcp_tool"]`, `compat_skill=["subscription_cli"]`. `mcp_tool`은 `ai-mcp` sidecar → `codex exec`(`/opt/kasset-codex/auth.json` `auth_mode=chatgpt`, API 키 없음)이라 per-token 과금이 없다.
- `.env.kasset`에서 `KASSET_AI_API_KEY`와 `KASSET_AI_OPENROUTER_API_KEY`를 주석 처리했다(원본은 `.env.kasset.bak-20260902-keys`, mode 600). `factory.py`가 키 없으면 direct/openrouter route를 조립하지 않으므로 정책을 되돌려도 유료 호출은 불가능하다. admin `/admin/ops/ai-routes`에서 해당 route는 `missing_api_key`로 표시된다.
- `KASSET_AI_MCP_TIMEOUT_SECONDS=120`(설정 상한), `KASSET_AI_SIDECAR_TIMEOUT_SECONDS=150`. 10건 배치 MCP 요약 실측 43초, 30초 기본값에서는 timeout이었다.
- 뉴스 요약 일일 상한 `KASSET_NEWS_SUMMARY_DAILY_CALL_LIMIT`(기본 100)은 `review.ai_call_events`의 UTC 당일 `kasset_news_summary` attempt 수를 센다. 2026-09-02는 중단 전 OpenRouter 2,724회가 이미 있어 자동 backfill은 `daily_limit`로 종료되며 2026-09-03 00:00 UTC부터 재개된다.
- PAPER 집행 Hard Risk `AI` 규칙(floor 0.50)은 이제 추천의 마지막 `kasset_ai_review` evidence를 본다. `status=agrees`이고 확신도 유한일 때만 그 값을 쓰고, 부재·`disagrees`·저확신·파싱 실패는 0으로 차단한다. Position Manager 결정론 청산(`position_manager`/`position_exit` evidence)은 생성 시점과 같은 `ai_confidence=1`로 재현돼 막히지 않는다. 추천 표시용 `confidence` 수식(`(ensemble.confidence+trigger_strength)/2`)과 admission은 불변이다.
- KR 시세 `previous_close`는 **직전 거래일 KRX 정규장 종가**(마감 동시호가 봉 포함)를 1순위로 쓴다. 토스 `/api/v1/candles interval=1d`의 `closePrice`는 NXT 야간거래(~20:00)까지 포함한 마지막 체결가라 기준가로 쓰면 토스 앱 등락률과 어긋난다. 정규장 종가 미확보 시 저장 1d → 토스 1d 순서로 강등한다.
- `public.kr_candles_1d`는 **2026-09-02부터** 최근 완료 거래일 행이 `research.kr_candles_1m_toss` 정규장 집계(`source='toss_regular'`)로 덮어써진다. 집계: 09:00<t≤15:30 `KRX_REGULAR` + 15:31 마감 동시호가 봉(`NXT_POST` 라벨), padding·volume 0 봉 배제. fail-closed gate: 체결봉 ≥60, 첫 체결 ≤09:10, 마지막 체결 ≥15:20 또는 15:31 봉 존재, value semantics 일치 — 미달 시 토스 1d(NXT 포함) 행 보존. 충돌 규칙: `toss_regular`는 `toss`/`yahoo_fallback`/`toss_regular`만 덮고, `toss`는 시가가 1% 초과 차이(액면분할·무상증자 adjusted 재적용)일 때만 `toss_regular`를 덮는다. 실행: `kasset_watchlist_candles.sync` 말미(09~16시 매시 05분 + 20:05 KST). 1분봉 적재는 20종목/분 로테이션(≈190분 주기)이라 16:05 run은 tail gate로 대부분 skip하고 20:05·다음날 09:05 run에서 확정된다. **2026-09-01 이전 행은 NXT 포함값 그대로**(forward-only 결정) — 상대거래량·거래대금 이동평균이 경계에서 계단형으로 어긋난다. `readiness._KR_ADJUSTED_SOURCES`/`_FALLBACK_SOURCES`, `promotion_evidence._FALLBACK_SOURCES`에 `toss_regular` 등록.
- 토스 1분봉 timestamp는 분의 끝 라벨이다. 15:30:00 마감 동시호가 체결은 15:31 봉에 실리므로 `_regular_close`는 `(window.start, window.end+1분]` 구간을 채택한다(US 16:00 ET도 동일 규칙).

## 이번 세션에서 한 일
### 2026-09-03 — 동시간대 RVOL SHADOW 관측 (PR #41 merge `acf093d81e62c4bb2e41b5c4d3889dfd32972321`, 운영 배포 완료)
- KR 개장 후 자동주문 0건을 다시 규명했다. 장애가 아니라 관문 결과다. 09:00~09:50 사이클 6건 전부 `intraday_trigger_not_satisfied`이고, 09:53 실측 완료 5분봉은 10개였다. `relative_volume`은 5m가 13봉(개장 후 65분), 20m가 16봉(80분)을 요구하므로 **매일 09:00~10:05는 구조적으로 주문이 불가능**하다. 10:10 사이클에서 예측대로 `unavailable` → `not_confirmed`로 전환됐다.
- 현행 RVOL은 baseline이 같은 세션 직전 12봉이라 오전에는 개장 러시가 분모에 들어간다. 10:12 실측 5m RVOL: 005930 0.615, 000660 0.912, 055550 0.542, 096770 2.567, 035420 0.454. 임계 1.5 자체는 달성 가능하고, 11시 이후 baseline이 개장 구간을 벗어나면 편향이 완화된다(9/2에 11:40 사이클만 통과한 것과 정합).
- 자동주문 체인은 `trigger 0건 → 추천 0건 → claimable 0건 → sweep owners=0`이다. sweep `owners=0`은 버그가 아니라 상류 결과다(`job.py:625-653`는 `review.ai_recommendations`의 claimable owner만 센다. 실측 `fully_claimable=0`). owner 4는 mode `AUTO_PAPER`, kill switch off, `promotion_bypass_enabled=true`, 현금 9,999,865.375원, 포지션 0, 오늘 주문 0건으로 **trigger 외의 관문은 전부 통과 상태**다.
- **동시간대(same-time) RVOL을 SHADOW 경로로 추가했다. 진입 정책과 same-session RVOL은 한 줄도 바꾸지 않았다.** 목적은 baseline 방식 A/B 비교 데이터 축적이며, 배포해도 주문 판정은 0% 달라지지 않는다.
  - Toss provider는 period 무관 약 197봉이 상한이라(5m=3거래일, 1m=2거래일) 과거 분봉 backfill이 불가능하다. 동시간대 baseline의 유일한 소스는 DB `research.kr_candles_1m_toss`다. 이 테이블은 2026-09-01 수집 시작, KRX 전 종목(3931개), `KRX_REGULAR` 기준 종목당 391행/일로 자동 축적된다. **20거래일이 차는 시점은 2026-09-28경**이고 그때까지 `insufficient_baseline_days`로 관측되는 것이 정상이다. 코드 변경 없이 자동으로 살아난다.
  - 신규: `app/services/research_candles/same_time_volume_profile.py`(종목별 bucket 조회), `rvol_shadow_repository.py`, `app/models/kasset_intraday_rvol_shadow.py`(`review.kasset_intraday_rvol_shadow`), alembic `20260903_kasset_rvol_shadow`(down_revision `20260902_screener_toss_source`).
  - `intraday_triggers.py`에 `same_time_relative_volume`과 공개 `same_time_baseline_median`을 추가했다. 집계는 median(짝수면 가운데 두 값 평균), 전 구간 Decimal, `lookback_days=20`·`minimum_days=10`, threshold는 기존 `Decimal("1.5")` 그대로다. 음수 baseline은 조용히 제외하지 않고 `ValueError`로 거절한다.
  - `vertical_slice.py`는 KRX 후보만 대상으로 window당 1회씩 최대 2회 배치 조회하고, 주문용과 분리된 세션에 기록한다. 전체를 `asyncio.timeout(25s)` + `SET LOCAL statement_timeout=20s`로 감싸 지연이 사이클을 막지 못하게 했다.
- 독립 checker 1회(`integration-risk`): CRITICAL 0, **주문 판정 불변성은 6가지 근거로 입증**(정책 객체 불변, 판정 입력 불변, 호출이 추천 커밋 이후, 세션 물리 분리, 출력 미소비, 예외 격리 적정). MAJOR 3건은 전부 수용·수정했다.
  - MAJOR-1: bucket을 종목 간 합집합으로 SUM해 baseline이 최대 3배 부풀려지는 결함(`INTRADAY_MAX_BAR_AGE=12분` 때문에 종목 간 마지막 봉이 최대 2 bucket 어긋남). 계약을 `requests: Mapping[str, Sequence[time]]`로 바꿔 종목별 bucket을 분리했다.
  - MAJOR-2: 거래일 DISTINCT CTE가 symbol 필터 없이 매 호출 풀스캔(20일 기준 약 2920만 행). CTE를 제거하고 `session_date_kst` 물리 범위 절단(`lookback_days*2+15`일) 후 종목별로 최근 N거래일을 잘라낸다.
  - MAJOR-3: 예외는 격리됐으나 지연은 격리되지 않아 사이클 blocking 가능. timeout 이중화로 해소.
  - MINOR 수용분: 죽은 분기 제거, padding-only 날짜 표본 제외(`bool_or(is_padding IS false)`), `exc_info=True` 로깅과 중복 날짜 `ValueError` fail-loud 유지, 부분 실패 시 summary 정합, `session_decision_status`/`reason`·baseline median 컬럼 추가, 모델명 `KAssetIntradayRvolShadow` 관습 일치, `(cycle_trace_id, symbol)` partial unique + `on_conflict_do_update`.


검증(로컬, 서버 test DB SSH 터널 경유):
- `uv run --group test pytest tests/extensions/kasset/automation/ tests/services/research_candles/ -q` → `546 passed in 921.62s`.
- `uv run --group test pytest tests/services/paper_cohort/test_migration.py -q` → `5 passed in 995.02s`(실 PostgreSQL upgrade→downgrade→upgrade 왕복, 단일 head). 이 테스트는 `Base.metadata.create_all` 후 boundary 이후 테이블을 명시적으로 DROP하는 구조라 신규 테이블을 목록에 1줄 등록해야 한다.
- `uv run ruff check app/ tests/` → `All checks passed!`, `ruff format --check` → `3571 files already formatted`.
- 운영 DB 읽기 전용 스모크: 종목마다 다른 마지막 bucket을 요청해 MAJOR-1 회귀를 실증했다. 005930은 10:10 → 274,293주, 055550은 10:00 → 25,973주(합집합 시절 10:10 값 13,400과 분리됨), 20m는 055550이 09:45~10:00 4-bucket 합 72,073주. `minimum_days=10`이면 전 종목 `insufficient_baseline_days`, `minimum_days=1`로 완화하면 값이 산출돼 쿼리·계산 경로가 정상임을 확인했다.

배포(2026-09-03 14:23~14:27 KST, KR 정규장 중):
- 운영 이미지는 `kasset-trader-core:acf093d81e62c4bb2e41b5c4d3889dfd32972321`다. `api`, `worker`, `scheduler`, `mcp`, `ai-mcp` 5개 container의 `/app/.build-vcs-ref`가 모두 이 SHA이고 `/health`는 `ok`다. **`ai-mcp`는 `profiles: ["ai-mcp"]`라 기본 `up -d`에 포함되지 않는다.** 이번에도 처음에 `d73a4e55`로 남아 `--profile ai-mcp up -d ai-mcp`를 따로 실행해야 했다.
- `.env.kasset`의 `CORE_IMAGE_TAG`가 배포 전 `8f965aaf`로 실행 중 이미지(`d73a4e55`)와 어긋나 있었다. 그대로 `up -d`했다면 롤백됐을 상태다. 새 SHA로 갱신했고 직전 파일은 `.env.kasset.bak-predeploy-20260903`에 남겼다.
- 배포 전 DB full custom archive: `backups/pre-rvol-shadow-20260903T052332Z/database.dump`(42,328,261 bytes, SHA-256 `376bd831223337a3217fe77e656edae1f4217bc8b6689ce890c6c61f2ca8dbab`, `pg_restore --list` 검증 통과).
- migration head가 `20260902_screener_toss_source` → `20260903_kasset_rvol_shadow`로 올라갔다. `review.kasset_intraday_rvol_shadow` 24개 컬럼과 partial unique index가 운영 DB에 생성된 것을 `\d`로 확인했다. 롤백 경로는 `alembic downgrade`이며 왕복은 CI에서 검증됐다.
- 배포 후 운영 컨테이너 안에서 신규 조회 경로를 직접 실행해 정상 동작을 확인했다: `load_same_time_bucket_volumes(requests={"005930":[14:10], "138040":[14:05,14:10]})` → 005930 표본 2일(09-01 261,675 / 09-02 430,313, median 345,994), 138040 표본 1일(8,685).
- owner cycle 로그에 `same_time_rvol_shadow=` 필드가 실제로 출력된다(배선 확인).
- **아직 `review.kasset_intraday_rvol_shadow` 행은 0건이다.** 배포 직전 14:20 KST에 추천이 생성돼 `_OWNER_COOLDOWN`(1시간)이 걸렸고, 15:20 사이클은 경계 포함이라 여전히 `recommendation_cooldown_active`, 15:30은 마감이었다. shadow는 KRX 후보만 대상이라 미국장에서도 쌓이지 않는다. **실제 행 기록 실증은 2026-09-04 KR 개장 후가 처음이다.**

배포 당일 자동주문(참고, 이번 변경과 무관):
- 2026-09-03 14:20 KST에 `rec-6835c14e`(owner 4, KRX 138040 BUY)가 생성되고 14:25에 `AUTO_PAPER SUBMITTED`로 집행됐다. 주문 `4dd8953f-2e82-4278-9f2e-220345836fc9`, 포지션 138040 3주 @135,100, 계좌 3 현금 9,999,865.375 → 9,594,504.58. **KR 정규장 안(14:25)의 정상 체결이므로 9월 2일 장외 건과 달리 원복 대상이 아니다.**
- 이 추천·집행은 배포 전 코드(`d73a4e55`) 판정이며 SHADOW 경로와 무관하다. 즉 이번 배포 전후로 주문 판정 경로는 동일하다.
### 이전 세션 (2026-09-02 ~ 09-03)
- 미국장 개장 후 주문 0건을 서버에서 직접 관측·규명했다. `load_completed_session_bars(symbol="AAPL", market="US")`가 `intraday_timestamp_unusable`을 돌려주는 것을 확인하고, `_NAIVE_TIMEZONE["US"]` 런타임 주입으로 `CompletedIntradayBars` 3봉(`data_as_of=13:45Z`)이 되는 것을 먼저 입증한 뒤 코드에 반영했다.
- Hard Risk `AI` 관문을 `AI_SHADOW`로 강등했다(`policy.py`). `job.py`는 AI 상태와 무관하게 파싱된 유한 confidence를 그대로 SHADOW 기록에 넘긴다. 임계값(RVOL 1.5, Daily Setup 조건)은 건드리지 않았다.
- 관문별 funnel 계측을 `vertical_slice.py`에 추가했다(`trigger_failures` Counter, `intradayTriggerFailures` 근거 키, owner summary 로그 확장).
- 신규 `tests/extensions/kasset/automation/test_intraday_data.py` 5건으로 회귀를 방어한다: ET-naive 수용, UTC 오해석 배제, 매핑 없는 시장 fail-closed, tz-aware 변환, stale 차단 유지.
- 기존 계약 테스트를 새 계약으로 재작성했다(`test_hard_risk_ai_review.py` AI 무 veto, `test_ai_trading_policy.py` 관문 우선순위 `AI_SHADOW`, `tests/routers/test_ai_recommendations.py` stale 규칙명).
- 사라진 veto를 살아있는 안전장치로 설명하던 주석 3곳(`job.py`, `decision_evidence.py`, `vertical_slice.py`)과 `minAiConfidence` 스키마 설명을 사실로 정정했다.
- 운영 감시용 스크립트를 서버에 두었다: `/root/kasset-order-watch.sh`(60초 간격 append, 로그 `/root/kasset-order-watch.log`), `/root/kasset-order-report.sh [분]`(구간 주문·funnel·ERROR 요약).

검증:
- focused pytest: `tests/extensions/kasset/automation` 476 passed, 신규 `test_intraday_data.py` 5 passed, MINOR 반영분 127 passed(`test_hard_risk_ai_review`/`test_ai_trading_policy`/`test_job`/`test_consumer`/`tests/routers/test_ai_recommendations`/`test_ai_trading_settings`).
- ruff check/format: 변경 전 파일 전부 clean.
- 배포 후 운영 관측: 14:40 cycle에서 `trigger_statuses={'not_triggered': 3}`, `relative_volume_not_confirmed` — 데이터 사유 소멸, 조건 판정 정상.
- 독립 checker 1회(`integration-risk`): MAJOR 2건 제기. MAJOR 2(회귀 테스트 0건)는 `test_intraday_data.py` 추가로 수용·해소. MAJOR 1(승격 fingerprint 회전)은 `review.kasset_strategy_promotions` 0건과 owner 4 bypass 확인으로 실 위험 없음을 근거로 종결, 다만 승격 승인 시 재확인 항목으로 남긴다. MINOR 3·4·6·7 수용·반영, MINOR 5(`failure_codes` 구조화) 미반영.

- 최근 7일 원장 분석: 유료 토큰 99.7%가 `kasset_news_summary`였다. direct `gpt-5.6-luna` 1,499회 성공, OpenRouter `z-ai/glm-5.3-flash` 성공 1,022회(출력 361만 tokens, 평균 78초·최대 22분) + 실패 5,890회(HTTP 403 5,860회). 종목 검토 lane은 40회 미만이었다.
- A: 운영 정책 revision 9로 review 3 lane을 `["mcp_tool"]` 단독으로 저장했다. 이전 `review_luna`는 `openrouter_flash`가 1순위였다. sidecar `run_skill` smoke는 7.2초에 `{"ok":true,"answer":"pong"}`.
- C: 유료 API 키 2개를 env에서 제거하고 api/worker/scheduler를 재기동했다. 이때 `.env.kasset`의 `CORE_IMAGE_TAG=8a698873`이 적용돼 세 서비스가 PR #26 이전 이미지로 내려갔던 것을 이번 배포에서 발견·정정했다(약 2.5시간 동안 api/worker가 `8a698873`, mcp/ai-mcp가 `1e502ebf`로 혼재).
- B: `summary_luna` allowlist/default에 `mcp_tool` 첫 순위 추가, `build_summary_json_client`가 MCP client를 조립, `model_router`의 MCP AnalysisKind 제한 제거. 뉴스 요약을 기사 10건 indexed batch 1호출로 전환(누락·중복 항목은 해당 기사만 6시간 backoff), UTC 일일 상한을 advisory lock으로 직렬화, Google News·Truth Social 수집 직후 인라인 요약 제거(5분 backfill 단일 진입점). 공시 요약은 MCP route만 자동 포함되고 배칭·상한은 없다.
- 검증: focused pytest 278 passed(PostgreSQL 15 임시 test DB), ruff check/format 통과, PR #29 GitHub Actions run `33587787453` 전체 통과·`ci-required` 성공. 배포 후 프로세스 한정 env(`KASSET_NEWS_SUMMARY_DAILY_CALL_LIMIT=3000`)로 backfill 1회 실행: MCP 1호출 43,288ms 성공, 10건 중 9건 한국어 요약 저장(예: article 22110 `POSITIVE/96`), 1건 항목 검증 실패로 backoff. 원장에 `provider=mcp`, token/cost NULL.
- 임시 test DB container `kasset-testpg`와 SSH 터널은 제거했다.
- 자동주문 0건 원인 규명: 30일간 `kasset-automation` 추천 4건 전부 `paper_execution_error=risk_preview_rejected:AI`. `job.py::_hard_risk`가 표시용 합성 confidence(≈0.28)를 `ai_confidence`로 넘겨 floor 0.50에 구조적으로 미달했다. 실제 사례 2건(09-01 003555, 09-02 11:40 KST 055550)의 AI(MCP sol)는 `disagrees, aiAction=HOLD, risk=HIGH, confidence 0.66/0.72`였으므로 수정 후에도 차단이 정당하다.
- PR #31: `decision_evidence.latest_ai_review_from_evidence`/`is_deterministic_position_exit` 추가, `job.py::_hard_risk` 입력 교체, `evaluate_hard_risk(ai_review_status=...)` 선택 인자로 detail에 `aiStatus` 표기. 신규 `tests/extensions/kasset/automation/test_hard_risk_ai_review.py` 12건(agrees 0.72 통과, disagrees·저확신·부재·NaN 차단, 청산 통과, 타 source 위장 차단). focused pytest 20 + 94 passed. 독립 checker 1회: major "청산 추천 전면 차단" finding을 수용해 수정, FINAL PASS. CI는 새 테스트 파일이 `ci_shards/shard-3.txt`에 없어 `taskiq-smoke` exact-cover 1회 실패 → 등록 후 전체 통과.
- 시세 등락률 불일치(사용자 보고: 토스 하이닉스 −4.7% vs 앱 −2.36%) 원인 규명·수정: 저장 1d close 1,652,000은 NXT 야간 마지막 체결가였고 KRX 정규장 종가는 1,693,000(15:31 동시호가 봉). PR #33(정규장 종가 1순위) → 마감 후 당일 종가가 잡혀 −0.12%로 붕괴 → PR #34(직전 거래일 window 고정, 당일 KST 0시 기준 조회) → 15:20 봉을 잡아 −4.83% → PR #35(동시호가 봉 포함) → 운영 실측 −4.67%(현재가 1,614,000 / 1,693,000)로 토스와 일치. focused pytest 49 passed×3회, ruff 통과, CI 전체 통과.
- 신한지주 15:10 KST `intraday_provider_unavailable`은 6시간 로그에서 단발 1건(`UpstreamUnavailableError`)이었고 1분봉 동기화는 매분 정상이다. 404 `stock-not-found`는 상장폐지·ETN 4종목만이다.
- KR 저장 일봉 정규장 보정(PR #37): 신규 `kr_regular_daily.py`, `converters.aggregate_kr_regular_daily_row`, `repository.fetch_kr_toss_minutes/upsert_kr_regular_rows`, `sync_service.override_kr_regular_daily`, CLI `scripts/override_kr_regular_daily.py --date [--symbols]`(완료 거래일만 허용). 독립 checker major 3건(세션 tail gate 부재로 16:05 run이 오후 중간 종가로 덮어쓰기, CLI 완료 세션 guard 부재, adjusted 재적용 차단) 수용·수정 후 PASS. focused pytest 89 passed/5 skipped, ruff·CI 통과. 배포 후 운영 수동 실행: 관심종목 4종목 completed, 1d 유니버스 703종목 중 556 upsert·147 skip(`regular_trade_rows_short` 84, `regular_first_trade_late` 36, 1분봉 부재 23, `regular_tail_missing` 4 — 저유동성·ETN). 하이닉스 9/2 정규장 행: O 1,630,000 / H 1,661,000 / L 1,612,000 / C 1,613,000 / V 3,227,484(토스 1d V 3,493,365).

## 다음 세션이 바로 할 일
00. **장부 분리 배포 직후 확인.** 배포 뒤 첫 미국장 sweep(5분 주기)에서 US 추천이 생기면 `review.kasset_paper_execution_events`와 `kasset_android_paper_orders`(`currency='USD'`)로 USD 장부 주문이 나가는지, Hard Risk detail이 `currency=USD`·`operatingBudget=10000`으로 찍히는지 본다. US BUY가 `POSITION`에서 막히면 `expectedMarket` 문구가 남아 있는 구 이미지가 도는 것이니 `/app/.build-vcs-ref`를 먼저 본다. `vertical_slice.py`가 바뀌어 승격 fingerprint가 다시 회전했다(레코드 0건이라 영향 없음).
00-1. Android 후속: AI픽 운용 설정에 USD 운용 예산 입력칸을 추가하고 PUT 확장(`operatingBudgetUsd` 등)을 그때 서버와 함께 정의한다. 지금은 응답의 `operatingBudgetKrw`/`operatingBudgetUsd`만 존재한다.
0. **P0 후속 (최우선).** PR #41은 merge·배포까지 끝났지만 `review.kasset_intraday_rvol_shadow`는 **아직 0행**이다. 배포 당일 KR장은 14:20 추천으로 쿨다운이 걸려 trigger 평가 사이클이 더 없었다. **2026-09-04 KR 개장 후 첫 사이클에서 행이 실제로 쌓이는지 반드시 확인한다.** 09:00~10:05는 `session_status_*`가 `unavailable:insufficient_completed_session_bars`, `same_time_status_*`는 표본 부족으로 `unavailable:insufficient_baseline_days`가 정상이다. 행 자체가 0이면 그때는 배선 문제이므로 `same_time_rvol_shadow=` 로그 요약과 `unavailable:shadow_timeout`/`shadow_write_failed` 여부를 본다.
0-0. 2026-09-28경 20거래일이 차면 `insufficient_baseline_days`가 사라지고 `same_time_rvol_5m/20m`에 실제 값이 들어가기 시작한다. 그 전까지 표본 부족은 정상이며 장애로 오판하지 않는다.
0-1. **P1은 데이터가 쌓인 뒤에 한다.** RS(`intraday_relative_strength`, 임계 `Decimal("0")`)를 hard AND에서 내리는 변경은 아직 하지 않았다. 10:10 실측에서 7종목 중 5건이 `intraday_relative_strength_disagrees`로 죽었지만, RVOL baseline 편향을 먼저 제거해야 원인이 분리된다. cohort 비교(A 현재 / B 동시간대+RS hard / C 동시간대+RS soft / D 동시간대+RS 없음)는 shadow 데이터로 한다.
0-2. ORB·VWAP(`directional`)은 이번에 건드리지 않았다. 10:10 실측에서 `no_directional_trigger` 6/7이지만 RVOL 편향 제거 후 별도로 본다.
1. 실제 주문 재현은 정규장 안에서만 한다. KR 09:00~15:30 KST, US 정규장 안에서 sweep 결과를 관찰하고, 장외 강제 집행은 하지 않는다. PAPER 보유·포지션은 현재 0이다.
2. owner 1·5로 자동주문을 내려면 승격이 필요하다(`strategy_promotion_required`). 승격 승인 시 런타임 fingerprint와 승격 레코드 fingerprint 일치를 반드시 대조한다 — `intraday_data.py`·`vertical_slice.py` 수정이 fingerprint를 바꾼다.
2. 관문 탈락률을 3~5 거래일 측정한 뒤에만 임계값을 논의한다. 지금 funnel은 `setup_rejections`(최다 `no_breakout_family_direction`)와 `trigger_failures`(`no_directional_trigger`, `relative_volume_not_confirmed`)를 분리해 남긴다. RVOL 1.5나 Daily Setup 조건을 측정 없이 낮추지 않는다.
3. RVOL은 개장 후 65~80분 동안 구조적으로 `unavailable`이다(5m: window 1 + baseline 12, 20m: 4 + 12). 개장 직후 무주문을 provider 실패로 오판하지 않는다.
4. US 분봉 저장 범위는 watchlist 7종목이다. automation 후보 100종목까지 저장하려면 Toss 분봉 호출량이 크게 늘어난다 — 필요해지면 후보 유니버스 저장을 별도로 설계한다(`us_symbol_universe` 전수는 대상에 넣지 않는다).
5. (완료) 서버 로컬 브랜치 `fix/us-intraday-and-ai-veto`(HEAD `1c2e9a0c`)를 PR #39로 올려 CI 통과·merge했다. `tests/extensions/kasset/automation/test_intraday_data.py`는 `ci_shards/shard-3.txt`에 등록돼 있다. 남은 GitHub 미반영 로컬 브랜치는 없다.
6. checker MINOR 5: `IntradayTriggerDecision`에 `failure_codes: tuple[str, ...]`를 노출해 funnel이 `blocked_reason` 문자열 재파싱에 의존하지 않게 한다.
7. 다음 장중 `agrees` + 확신도 ≥0.50 진입 추천이 나오면 `review.ai_recommendations.paper_execution_status`가 `SUCCEEDED`로 가고 PAPER 주문이 생성되는지 확인한다. `FAILED`이면 `paper_execution_error`와 Hard Risk detail의 `aiStatus`를 본다. 검증 목적 주문은 만들지 않는다.
2. 2026-09-03 09:05·20:05 KST `kasset_watchlist_candles.sync` 결과의 `regularOverride` 요약과 `kr_candles_1d` `source='toss_regular'` 행 수를 확인한다. `regular_tail_missing`이 20:05 run에서도 다수면 1분봉 로테이션 커버리지를 본다. 2026-09-01 이전 이력 재구축(토스 1분봉 API 대량 호출)은 사용자 결정 사항이다.
3. 2026-09-03 00:00 UTC 이후 첫 `news.articles.summarize`(5분 주기)에서 `daily_limit`가 아니라 MCP 호출이 발생하는지, 하루 호출 수가 100 이하로 멈추는지 원장(`provider=mcp`, `feature=kasset_news_summary`)으로 확인한다. 100회 × 10건 = 하루 최대 1,000기사다.
4. MCP 실패 시 fallback이 없다. `AiProviderUnavailable`이 반복되면 codex 구독 한도·`ai-mcp` health·timeout(120/150)을 먼저 본다. 유료 키를 되살리지 않는다.
5. `.env.kasset.bak-20260902-keys`는 유료 키 원본이다. 복구 결정이 없으면 폐기 대상이다.
6. 정상 미국장 cycle을 계속 관찰하되 `intraday_trigger_not_satisfied`를 provider 실패로 오판하지 않는다.
7. `app/services/filled_orders_service.py::_toss_fill_timestamp`의 파싱 불가 timestamp 1건이 해당 fetch window 전체를 실패시키는 low-severity fail-safe 동작을 per-order skip으로 좁힐지 별도 변경으로 검토한다.
8. startup의 passlib/bcrypt `__about__` 경고와 yfinance cache 경고는 health를 깨지 않지만 의존성·권한 정리 후보로 남아 있다.
9. 제외 종목 `0126Z0`, `SPCX`, `SCCO`, 성과 미달 candidate와 historical point-in-time cohort 근거는 실제 데이터·성과 조건을 채울 때만 복귀·승격한다.

## 세션 이력
- 2026-09-03: KRW/USD 정산 장부 분리(`settlement_book`, 장부별 usage·Hard Risk·sizing, PUT 반대 장부 보존, 응답 `operatingBudgetKrw/Usd`)를 구현·검수(PASS)했고 PR #43(자동주문 표시·FCM WIP 재검증 `225 passed`) 위에 후속 PR로 발행했다. 서버 배포는 PR merge 직후 진행.
- 2026-09-03: 동시간대 RVOL SHADOW 관측 경로를 PR #41로 merge하고 운영 배포(`acf093d8`). migration head `20260903_kasset_rvol_shadow`, 컨테이너 5개 정합. 같은 날 14:25 KST에 정규장 안 PAPER 자동주문(138040 3주 @135,100)이 정상 집행됐다.
- 2026-09-03: 서버 로컬에만 있던 `fix/us-intraday-and-ai-veto`를 GitHub에 발행하고 PR #39를 CI 통과 후 merge(`1c74f3f2`). 운영 코드와 `main`의 괴리를 해소했다.
- 2026-09-03: US 분봉 수집 대상을 watchlist까지 확장하고 Toss 스냅샷 실패를 격리(`d73a4e55`). 운영 실측 1분봉 2,520행·5분봉 504행.
- 2026-09-02: 정규장 밖 무인 PAPER 집행을 차단(`out_of_regular_session`)하고 장외 강제 체결을 원복(`2e939c27`).
- 2026-09-02: AI veto 제거 후 첫 PAPER 자동주문 체결(055550 BUY 4주 @110,400). 자동 sweep 경로 end-to-end 증명.
- 2026-09-02: US ET-naive 분봉 수용, AI Hard Risk veto 제거(SHADOW), 관문 funnel 계측 배포(`ee170aba`). 배포 후 미국장 전 관문이 데이터 결함 없이 판정, 주문 0건은 조건 미충족.
- 2026-09-02: KR 저장 일봉 최근 완료 거래일을 KRX 정규장 OHLCV로 보정하는 경로 배포, 9/2 유니버스 556종목 적용(PR #37 `8f965aaf`).
- 2026-09-02: KR 등락 기준가를 직전 거래일 KRX 정규장 종가(동시호가 봉 포함)로 교정해 토스 등락률과 일치(PR #33·#34·#35, `7f4924ef`).
- 2026-09-02: 자동주문 0건 원인(Hard Risk AI 규칙이 합성 confidence 판정)을 수정해 배포(PR #31 `3851a0ed`).
- 2026-09-02: 전 AI lane MCP-only 전환(revision 9→10), 유료 API 키 제거, 뉴스 요약 10건 배칭·일일 상한 100 배포(PR #29 `ac2de5a9`).
- 2026-09-02: OpenAI와 OpenRouter 로그에서 뉴스 요약 토큰 급증을 확인하고 운영 `summary_luna` route를 비활성화했다.
