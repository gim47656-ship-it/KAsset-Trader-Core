# HANDOFF — KAsset-Trader-Core
갱신: 2026-08-31 (미국장 10분 AI cycle·중복 방지·USD 원화 참고 평가를 운영 배포)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 안전 계약은 owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인·promotion, 주문 idempotency를 보존하는 것이다. LIVE 주문 경로와 안전장치 우회는 추가하지 않는다.

후보별 Benchmark RS는 기존 Candidate Ranker의 활성 입력으로 연결돼 순위·추천에 영향을 줄 수 있다. First Pullback/NR7/Inside Day, High-Watermark, Loss-Streak, Soft Top-K/Sector Cap은 관찰용 SHADOW evidence만 계산한다. 이 SHADOW 기능의 활성값은 모두 기본 `false`, `promotionEligible=false`이며 주문 입력으로 사용하지 않는다. AI MCP sidecar도 내부 구독형 AI 실행만 제공하고 주문·DB·Redis·broker 도구를 노출하지 않는다.

## 전체 진행 상태
- `origin/main`은 PR #7 merge commit `328d5ba838ef86d288758d9c58eabf80011c20db`이다.
- 운영 실행 이미지는 `kasset-trader-core:328d5ba8`, image id `sha256:a3954dc886071324a6fc1aefa02fc471a25051c9e249814dc9f56835f230978a`다.
- API, worker, scheduler, 거래 MCP, AI MCP가 새 이미지로 기동됐다. 내부·외부 API health는 `{"status":"ok"}`이고 API container는 healthy/restart 0이다.
- 운영은 `TRADING_ENABLED=true`, `LIVE_TRADING_ENABLED=false`, `AI_PAPER_AUTO_EXECUTION_ENABLED=true`, owner 4 runtime은 `PAPER`다. Kill Switch, Hard Risk, promotion bypass는 바꾸지 않았다.
- KR/US AI recommendation cycle은 각 거래소 현지시각 09:00~15:59 평일에 10분 주기다. 실제 정규장 gate가 KR 09:00, US 09:30부터 후보/AI를 허용한다.
- 미국 schedule은 `America/New_York` timezone이므로 EST/EDT를 자동 반영한다. 같은 cycle이 10분을 넘겨도 PostgreSQL session advisory lock이 다음 worker 진입을 차단한다.
- owner별 설정 시장과 현재 정규장 시장이 불일치하면 `no_configured_regular_market_open`, 전체 휴장이면 `no_regular_market_open`으로 감사 원장에 구분한다.
- PAPER USD 포지션은 native USD 값을 바꾸지 않는다. Toss 또는 open.er-api의 fresh USD→KRW Decimal quote와 유효구간이 완전할 때만 `market_value_krw_reference`를 내려준다.
- owner 4의 현재 PAPER 포지션은 0건이다. 자연 추천이 실제 보유를 만들기 전에는 USD 원화 참고값 두 시점 관측 대상이 없다.
- Core의 test/lint/type/build는 로컬 workstation에서 실행하지 않는 규칙을 이 저장소 `AGENTS.md`에 추가했다. 기본 검증은 GitHub Actions, 서버는 격리된 test 환경만 허용한다.

## 이번 세션에서 한 일
- scheduler를 KR `Asia/Seoul`, US `America/New_York` 기준 각각 `*/10 9-15 * * 1-5`로 바꾸고, cycle 진입점에 기존 거래소 calendar 기반 정규장 gate를 추가했다.
- 다중 TaskIQ worker/재시작 사이에서도 한 cycle만 돌도록 PostgreSQL `pg_try_advisory_lock`을 task 전체 수명 동안 유지한다. lock contention은 `cycle_already_running`으로 무작업 종료한다.
- owner 추천 시장과 열린 시장의 교집합만 candidate loader에 전달한다. 닫힌 시장은 AI policy/provider/router와 후보 수집을 건드리지 않는다.
- Toss USD/KRW quote를 우선 검증하고 실패하면 open.er-api로 fallback하는 상세 환율 snapshot을 추가했다. pair, Decimal 양수/finite, source, timezone, as-of/valid-until, stale을 모두 fail-closed 검증한다.
- positions 응답에 nullable KRW 참고값·환율·provider·유효구간·stale/error를 추가했다. 응답당 환율은 1회만 받고, market value가 없는 USD 행은 다른 행의 FX provenance를 빌리지 않는다.
- 독립 checker가 찾은 3개 MAJOR를 수정했다: Android 빈 그래프 30초 요청 폭주를 5분 주기/동시성 5로 제한, pause/resume timer 유지, 10분 Core cycle의 분산 single-flight 보장.
- checker의 8개 MINOR도 반영했다. source를 `Literal["toss","open_er_api"]`로 제한하고, 초 단위 직렬화에서 사라지는 FX 유효구간을 거부하며, owner-market skip을 분리하고 API candle 문서를 실제 range 계약으로 수정했다.
- 운영 SSH 기본 경로를 `~/.ssh/config`의 `kasset-prod` alias로 고정했다. Tailnet hostname을 쓰되 기존 공인 IP host key와 동일한 ED25519 key를 `HostKeyAlias`로 검증한다.

검증:
- GitHub Actions run `33406351976`: lint/formatter/ty, migration round-trip, TaskIQ worker/scheduler smoke, test 4 shards, frontend, security, `ci-required` 전부 통과.
- Android 전체 unit test와 debug APK build는 `BUILD SUCCESSFUL`(44 tasks). Samsung `SM-S926N`에서 미국 `TQQQ` 포함 관심목록 그래프를 실제 확인했다.
- 독립 checker 최초 판정은 `REWORK`였다. 모든 MAJOR/MINOR 조치와 통합 CI 후 Main 최종 판정은 `FINAL: PASS`, `OWNER: MAIN`이다.
- GitHub `pull_request` event 미발생 결함 때문에 workflow_dispatch의 실제 성공 run을 동일 head status에 연결했다. required app 제한만 병합 순간 `null`로 전환했고, 병합 직후 strict=true와 GitHub Actions app id `15368` required 3종을 정확히 복원했다.

운영 배포:
- server checkout은 `328d5ba8`로 fast-forward했고 `.env.kasset.pre-p1-328d5ba8`를 남겼다.
- 새 image를 server에서 빌드해 API/MCP/AI MCP/worker/scheduler만 교체했다. DB/Redis/Caddy는 재기동하지 않았다.
- 운영 scheduler container에서 schedule label 두 개(`Asia/Seoul`, `America/New_York`)를 직접 출력해 확인했다.
- 15:20:00 UTC(11:20 EDT) scheduler가 `kasset_market_events.run`을 자연 발행했다. owner 4는 열린 시장 `US`, 후보 7(전부 watchlist), rank 7, actionable 0, AI 검토 0, 추천 0으로 15:20:07에 정상 종료했다. 원장 trace는 `cyc-2b2e49a56cb64c3fb2361642d29ee23f`, skip은 `no_dynamic_ensemble_signal`이다.

## 다음 세션이 바로 할 일
1. 미국 정규장 자연 10분 cycle은 15:20 UTC에 검증됐다. 이후 actionable/추천이 생길 때까지 owner 4 원장의 `candidate_markets.US`, AI 검토 수와 안정된 skip 사유를 관측하되 강제 cycle/주문/임계값 완화는 금지다.
2. 자연 PAPER 보유가 생기면 5초 이상 간격의 두 시점에서 native USD 평가, KRW 참고값, FX source/as-of/valid-until/stale을 대조한다.
3. 추천이 자동 실행되면 `cycle_trace_id`로 cycle→recommendation→execution event→order→trade→position을 owner 4 범위에서 조인한다. `LIVE_TRADING_ENABLED=false`는 유지한다.
4. GitHub `pull_request` Actions event가 0건인 원인을 수정해야 한다. 현재 branch protection은 strict, required contexts `ci-required`·`migration (PostgreSQL 15)`·`frontend`, app id `15368`로 복원돼 있다.
5. `record_daily_snapshot`/`calculate_daily_returns` 운영 호출자는 아직 확인되지 않았다. 스케줄 연결 전 통화별 drawdown·Sharpe·daily return null은 정상이다.

## 세션 이력
- 2026-08-31: 미국장 10분 AI cycle, 분산 single-flight, 검증된 USD 원화 참고 평가를 운영 image `328d5ba8`로 배포.
- 2026-08-31: P0 cycle/실행 추적 원장, KRW/USD 성과 분리, 시세 provenance를 운영 배포하고 전체 CI 통과.
- 2026-08-31: PAPER 실시간 평가·USD 자금·뉴스 동기화·AI malformed 응답 격리를 운영 배포.
- 2026-08-31: 국내 스크리너 KRX 세션 만료 fallback 배포, 운영 150종목 복구.
- 2026-08-31: Benchmark RS Ranker 연결, Setup/Risk/Portfolio SHADOW, 내부 MCP sidecar 구현.
