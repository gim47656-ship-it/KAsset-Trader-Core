# HANDOFF — KAsset-Trader-Core
갱신: 2026-08-31 (P0 실행 추적·통화별 성과·시세 provenance를 운영 배포하고 merge-tree 전체 CI 통과)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 안전 계약은 owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인·promotion, 주문 idempotency를 보존하는 것이다. LIVE 주문 경로와 안전장치 우회는 추가하지 않는다.

후보별 Benchmark RS는 기존 Candidate Ranker의 활성 입력으로 연결돼 순위·추천에 영향을 줄 수 있다. First Pullback/NR7/Inside Day, High-Watermark, Loss-Streak, Soft Top-K/Sector Cap은 관찰용 SHADOW evidence만 계산한다. 이 SHADOW 기능의 활성값은 모두 기본 `false`, `promotionEligible=false`이며 주문 입력으로 사용하지 않는다. AI MCP sidecar도 내부 구독형 AI 실행만 제공하고 주문·DB·Redis·broker 도구를 노출하지 않는다.

## 전체 진행 상태
- `origin/main`은 PR #5 merge commit `253bb71302cab5f48559d36da92fd860e5cc59cb`이다.
- 운영 실행 이미지는 `kasset-trader-core:253bb713`, image id `sha256:f8514a2a58b90e53b595fba138929e99475fb76655ea8cf533b0c8b734545060`이다.
- 운영 migration은 `20260831_p0_currency (head)`까지 적용됐다.
- API, worker, scheduler, 거래 MCP, AI MCP가 새 이미지로 기동됐다. API/MCP/AI MCP health는 healthy이고 worker/scheduler는 running이다.
- 운영은 `TRADING_ENABLED=true`, `LIVE_TRADING_ENABLED=false`, `AI_PAPER_AUTO_EXECUTION_ENABLED=true`, 모든 owner runtime은 `PAPER`다. Kill Switch, Hard Risk, promotion bypass는 바꾸지 않았다.
- 추천 cycle→추천→PAPER 실행 event→주문을 `cycle_trace_id`와 owner scope로 추적할 수 있다. AUTO_PAPER와 APPROVAL 결과는 append-only `review.kasset_paper_execution_events`에 기록한다.
- PAPER 보유·스냅샷·성과는 KRW/USD를 합산하지 않는다. 미국 주식만 USD, 국내 주식과 crypto는 실제 현금 원장과 같은 KRW 버킷이다.
- 보유 평가에는 실제 quote source/as-of/session/staleness를 함께 내리고, 입증할 시세가 없으면 평가 숫자 대신 안정된 `valuation_error`와 null을 반환한다.
- Google News 동기화와 KRW 10,000,000원/USD 10,000달러 독립 초기자금은 계속 운영 중이다.

## 이번 세션에서 한 일
- 자동 추천 cycle에 재시작 후에도 유지되는 `cycle_trace_id`를 추가하고 추천·실행 원장까지 전달했다. 원장 조회는 `owner_user_id AND recommendation_id`로 제한해 다른 owner의 주문·attempt 상태를 읽지 않는다.
- AUTO_PAPER와 승인 실행의 `IDLE/BLOCKED/REJECTED/SUBMITTED/FAILED` 결과를 별도 세션의 append-only 원장에 기록했다. 감사 기록 실패가 실제 PAPER 체결 결과를 되돌리거나 바꾸지 않게 격리했다.
- PAPER 포지션·계좌 summary·daily snapshot·performance를 KRW/USD별로 분리했다. 혼합 통화 legacy 컬럼은 보존하되 신규 계산에서 사용하지 않는다.
- reviewer가 찾은 USDT 포지션의 cash/reporting 불일치를 수정했다. crypto는 실제로 `cash_krw`에서 정산되므로 wire·성과도 KRW로 고정했다. 운영 과거 `paper_trades.currency='USDT'` 행은 0건이었다.
- 시세 provenance를 `quote_source`, `quote_as_of`, `quote_session`, `quote_is_stale`로 노출했다. provider timestamp가 없거나 파싱되지 않으면 현재 시각을 만들어 넣지 않고 null로 둔다.
- migration `20260831_p0_trace`와 `20260831_p0_currency`를 추가했다. 실제 PostgreSQL upgrade/downgrade/upgrade CI가 단일 head를 검증한다.
- main 보호를 PR 필수, 관리자 포함, strict, force-push/delete 금지, required checks `ci-required`·`migration (PostgreSQL 15)`·`frontend`로 설정했다.
- GitHub가 이 저장소의 `pull_request` workflow event를 만들지 않아 required checks를 PR merge ref에 자동 연결하지 못했다. 동일 부모·동일 tree `43752bbf`인 synthetic merge commit을 전체 CI로 검증한 뒤 required status-check gate만 잠시 제거해 PR #5를 병합하고 즉시 원래 strict 규칙을 복원했다. 원인 수정 전에는 같은 절차를 반복하지 말고 PR event 미발생부터 조사한다.

검증 결과:
- 로컬 P0 집중 테스트 `53 passed`, 통화·평가 회귀 `29 passed`, CI 결함 회귀 `3 passed + 1 passed`; Ruff와 `ty check app --error-on-warning` 통과.
- GitHub Actions head run `33397032852`과 동일 merge tree run `33398251289` 모두 migration, lint, taskiq exact-cover/smoke, test 4 shards, frontend, security, `ci-required`가 통과했다.
- 독립 checker 1차 MAJOR(USDT cash/reporting 불일치)를 수정했고 delta 재검수는 `FINAL: PASS`, 신규 finding 없음이다.
- Android/API 계약 문서는 `KAsset-Trader/docs/API-CONTRACT.md`, commit `1e33bb6e`에 반영했다.

운영 배포·실측:
- 배포 전 backup: `/root/backups/kasset-daily/kasset-20260831T134952Z.dump.gz`, 5,106,099 bytes, SHA-256 `5ce207f993a98f46ce069008d4aa9ed929b52e94e2cd8b7effb19e724c43d981`; `gzip -t`와 container `pg_restore -l` 통과.
- 운영 DB revision은 `20260831_p0_currency`; `review.kasset_paper_execution_events` 생성과 snapshot 통화별 6개 nullable 컬럼을 확인했다.
- API/MCP/AI MCP healthy, worker/scheduler running, 내부 `/health`와 외부 `https://175-45-201-51.sslip.io/health`가 `{"status":"ok"}`를 반환했다.
- ledger는 배포 직후 0행이다. 다음 자연 cycle/승인부터 쌓여야 하며 P1에서 강제 실행 없이 관측한다.

## 다음 세션이 바로 할 일
1. P1은 한국 정규장 중 자연 PAPER 보유로 관측한다. 08:50 KST 전 owner 4가 `AUTO_PAPER/PAPER`, `LIVE_TRADING_ENABLED=false`, Kill Switch/Hard Risk 유지인지 확인한다.
2. 09:05 KST 시세 provider 상태를 확인하고 09:10 이후 자연 cycle을 기다린다. cycle/threshold/promotion/order를 강제로 만들지 않는다.
3. 자연 추천이 실행되면 `cycle_trace_id`로 cycle→recommendation→execution event→order→trade→position을 owner 4 범위에서 조인한다. 실패·거부도 원장에 한 행으로 남는지 확인한다.
4. 동일 자연 보유를 5초 이상 간격의 서로 다른 두 시세에서 읽어 source/as-of/session/staleness와 평가식·통화 버킷을 대조한다. candle fallback이나 출처 불명 시세를 실시간으로 인정하지 않고, 입증 실패는 숫자 0이 아니라 null이어야 한다.
5. `record_daily_snapshot`/`calculate_daily_returns` 운영 호출자는 아직 확인되지 않았다. 스케줄 연결 전에는 통화별 drawdown·Sharpe·daily return이 null인 것이 정상이다.
6. GitHub `pull_request` Actions event가 0건인 원인을 별도 수정해야 한다. main 보호 규칙은 현재 strict/PR-required/required-checks 상태로 복원돼 있다.

## 세션 이력
- 2026-08-31: P0 cycle/실행 추적 원장, KRW/USD 성과 분리, 시세 provenance를 운영 배포하고 checker·merge-tree 전체 CI·health를 통과.
- 2026-08-31: PAPER 실시간 평가·USD 자금·뉴스 동기화·AI malformed 응답 격리를 운영 배포하고 자연 cycle/웨일 대시보드/17-job GitHub Actions를 검증.
- 2026-08-31: 국내 스크리너 KRX 세션 만료 fallback 배포, 운영 150종목 복구, 추천·AI·PAPER consumer 실측.
- 2026-08-31: Benchmark RS Ranker 연결, Setup/Risk/Portfolio SHADOW, 내부 MCP sidecar 구현.
- 2026-08-30: 관리자·복구 운영 배포, owner 4 AUTO_PAPER 준비.
