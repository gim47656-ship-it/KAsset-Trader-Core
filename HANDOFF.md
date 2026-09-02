# HANDOFF — KAsset-Trader-Core
갱신: 2026-09-02 (KR 저장 일봉 정규장 기준 보정 배포; 등락 기준가 교정·Hard Risk AI 규칙·전 AI lane MCP-only·유료 키 제거 유지)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 운영 broker 범위는 KR/US 실계좌·주문·체결의 Toss와 KR mock read-only 조회의 NH PLUG이며, KIS 미설정은 의도된 상태다. 역사 KIS ledger/read model은 보존하되 production runtime에는 연결하지 않는다. owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인 hash, 주문 idempotency, accepted-only ledger와 broker evidence fill을 보존하고 검증 목적으로 주문을 만들지 않는다.

## 전체 진행 상태
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
1. 다음 장중 `agrees` + 확신도 ≥0.50 진입 추천이 나오면 `review.ai_recommendations.paper_execution_status`가 `SUCCEEDED`로 가고 PAPER 주문이 생성되는지 확인한다. `FAILED`이면 `paper_execution_error`와 Hard Risk detail의 `aiStatus`를 본다. 검증 목적 주문은 만들지 않는다.
2. 2026-09-03 09:05·20:05 KST `kasset_watchlist_candles.sync` 결과의 `regularOverride` 요약과 `kr_candles_1d` `source='toss_regular'` 행 수를 확인한다. `regular_tail_missing`이 20:05 run에서도 다수면 1분봉 로테이션 커버리지를 본다. 2026-09-01 이전 이력 재구축(토스 1분봉 API 대량 호출)은 사용자 결정 사항이다.
3. 2026-09-03 00:00 UTC 이후 첫 `news.articles.summarize`(5분 주기)에서 `daily_limit`가 아니라 MCP 호출이 발생하는지, 하루 호출 수가 100 이하로 멈추는지 원장(`provider=mcp`, `feature=kasset_news_summary`)으로 확인한다. 100회 × 10건 = 하루 최대 1,000기사다.
4. MCP 실패 시 fallback이 없다. `AiProviderUnavailable`이 반복되면 codex 구독 한도·`ai-mcp` health·timeout(120/150)을 먼저 본다. 유료 키를 되살리지 않는다.
5. `.env.kasset.bak-20260902-keys`는 유료 키 원본이다. 복구 결정이 없으면 폐기 대상이다.
6. 정상 미국장 cycle을 계속 관찰하되 `intraday_trigger_not_satisfied`를 provider 실패로 오판하지 않는다.
7. `app/services/filled_orders_service.py::_toss_fill_timestamp`의 파싱 불가 timestamp 1건이 해당 fetch window 전체를 실패시키는 low-severity fail-safe 동작을 per-order skip으로 좁힐지 별도 변경으로 검토한다.
8. startup의 passlib/bcrypt `__about__` 경고와 yfinance cache 경고는 health를 깨지 않지만 의존성·권한 정리 후보로 남아 있다.
9. 제외 종목 `0126Z0`, `SPCX`, `SCCO`, 성과 미달 candidate와 historical point-in-time cohort 근거는 실제 데이터·성과 조건을 채울 때만 복귀·승격한다.

## 세션 이력
- 2026-09-02: KR 저장 일봉 최근 완료 거래일을 KRX 정규장 OHLCV로 보정하는 경로 배포, 9/2 유니버스 556종목 적용(PR #37 `8f965aaf`).
- 2026-09-02: KR 등락 기준가를 직전 거래일 KRX 정규장 종가(동시호가 봉 포함)로 교정해 토스 등락률과 일치(PR #33·#34·#35, `7f4924ef`).
- 2026-09-02: 자동주문 0건 원인(Hard Risk AI 규칙이 합성 confidence 판정)을 수정해 배포(PR #31 `3851a0ed`).
- 2026-09-02: 전 AI lane MCP-only 전환(revision 9→10), 유료 API 키 제거, 뉴스 요약 10건 배칭·일일 상한 100 배포(PR #29 `ac2de5a9`).
- 2026-09-02: OpenAI와 OpenRouter 로그에서 뉴스 요약 토큰 급증을 확인하고 운영 `summary_luna` route를 비활성화했다.
