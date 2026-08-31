# HANDOFF — KAsset-Trader-Core
갱신: 2026-08-31 (운영 PAPER·뉴스·USD·AI 응답 격리 배포와 전체 GitHub Actions 회귀 통과)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 안전 계약은 owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인·promotion, 주문 idempotency를 보존하는 것이다. LIVE 주문 경로와 안전장치 우회는 추가하지 않는다.

후보별 Benchmark RS는 기존 Candidate Ranker의 활성 입력으로 연결돼 순위·추천에 영향을 줄 수 있다. First Pullback/NR7/Inside Day, High-Watermark, Loss-Streak, Soft Top-K/Sector Cap은 관찰용 SHADOW evidence만 계산한다. 이 SHADOW 기능의 활성값은 모두 기본 `false`, `promotionEligible=false`이며 주문 입력으로 사용하지 않는다. AI MCP sidecar도 내부 구독형 AI 실행만 제공하고 주문·DB·Redis·broker 도구를 노출하지 않는다.

## 전체 진행 상태
- `origin/main`은 `fd3377f3f28603791b7bb49152745be504fb2cf8`(이 HANDOFF 커밋 전)다. runtime 변경은 `db201542`까지이며 이후 커밋은 migration/회귀 테스트 계약만 갱신한다.
- 운영 실행 이미지는 같은 runtime 코드의 `kasset-trader-core:db201542`, image id `sha256:fd1a744ddd7a7619185dac01293699fdf2209ae54ed4b9c798101bdade0aa2fe`다.
- 운영 migration은 `20260831_paper_initial_usd (head)`까지 적용됐다.
- API, worker, scheduler, 거래 MCP, AI MCP는 `db201542` 이미지로 기동됐고 API/MCP/AI MCP health가 정상이다.
- 운영은 `TRADING_ENABLED=true`, `LIVE_TRADING_ENABLED=false`, `AI_PAPER_AUTO_EXECUTION_ENABLED=true`, 모든 owner runtime은 `PAPER`다. Kill Switch, Hard Risk, promotion bypass는 바꾸지 않았다.
- Google News 동기화가 운영 활성 상태다. 마지막 수동 수집 뒤 Google News 저장량은 KR 284건, US 701건이다.
- PAPER 국내·미국 계좌는 KRW 10,000,000원과 USD 10,000달러를 독립 초기자금·현금으로 관리한다.

## 이번 세션에서 한 일
- 관리자 운영 대시보드에 후보 수집→랭킹→전략 평가→AI 검토→추천→PAPER 주문 funnel, 후보별 AI 판정, provider/model/tier, 거절 사유, 뉴스 수집 상태, AI 사용량과 runtime 안전 설정을 실제 집계값으로 표시했다.
- AI 제공자 우선순위를 기능별로 저장하고 MCP→직접 API→OpenRouter availability fallback을 유지했다. validation/policy 오류는 provider fallback 대상이 아니다.
- PAPER 시세·평가 경로를 주문과 같은 `quote_for_market`으로 통일했다. 국내는 Toss→NH→저장 candle, 미국은 사용 가능한 미국 시세 provider→저장 candle 순서이며, 시세 실패를 평가금액 0으로 위장하지 않고 nullable로 반환한다.
- Android가 보유 화면 진입 직후와 표시 중 5초마다 평가를 새로 읽을 수 있도록 API 계약을 맞췄고, 전체 매도 뒤에도 주문·체결 내역을 다시 조회할 수 있다.
- 미국 PAPER 초기자금·현금 `initial_capital_usd`/`cash_usd`와 migration `20260831_paper_initial_usd`를 추가했다. 기존 계좌는 USD 거래·보유 이력이 없고 USD 잔고가 0인 경우만 10,000달러로 안전 보정했다.
- Google News KR/US 동기화와 스케줄 flag를 운영 활성화했다. 수동 실측은 KR inserted 184, US inserted 488/updated 1이었다.
- 자연 AI 추천 cycle에서 한 model 응답의 문장형 `rationale_tags`가 `ValidationError`로 owner 전체 cycle을 중단하던 결함을 고쳤다. provider schema에 짧은 비문장 태그 제약을 설명하고, malformed 후보 하나만 `invalid_ai_response`로 격리해 다음 후보를 계속 처리한다. 원문 model payload는 로그·감사 원장에 남기지 않는다.
- 최신 `Base.metadata`에서 과거 revision을 재구성하는 PostgreSQL migration-chain 테스트의 최신 table/column 제거 경계를 보완하고, 현재 API/정책 계약과 달라진 뉴스 요약·AI 정책·PAPER fake session·스크리너·DB guard fixture를 실제 계약에 맞췄다.

주요 커밋:
- `3ee9749`: 운영 관리자 관측성과 AI 정책 화면.
- `e016aae6`: PAPER risk/USD/news 계약.
- `0cb2d94c`: PAPER 포트폴리오 실시간 평가.
- `38b38c86`: system-status migration 테스트 격리.
- `db201542`: malformed AI 후보 응답 격리.
- `6c94063d`: 최신 migration-chain 테스트 경계 재구성.
- `fd3377f3`: 현재 API·정책·migration head에 맞춘 CI 회귀 fixture.

검증 결과:
- PAPER 평가 집중 테스트 `7 passed`, AI router/vertical-slice 전체 `25 passed`, system-status `6 passed`.
- 전체 formatter `4426 files already formatted`; 변경 범위 Ruff와 `ty check` 통과.
- 실제 PostgreSQL에서 migration `upgrade → downgrade → upgrade` 단일 테스트 `1 passed in 28.57s`.
- GitHub Actions `Test` run `33382656707`은 head `fd3377f3`에서 17 jobs 전체 통과했다: https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/33382656707
- Android `:app:testDebugUnitTest :app:assembleDebug`: `BUILD SUCCESSFUL`, 44 tasks.
- PAPER refresh 독립 검수: `FINAL: PASS`, blocker/major 없음.
- malformed AI 응답 독립 검수는 blocker/major 없음. reviewer에게 final diff/raw output packet이 전달되지 않아 판정은 `INCONCLUSIVE`; Main은 실제 raw test output과 운영 cycle 실측으로 finding을 닫았다.

운영 배포·실측:
- 배포 전 backup: `/root/backups/kasset-daily/kasset-20260831T082517Z.dump.gz`, 4,653,297 bytes.
- DB head는 `20260831_paper_initial_usd`; owner 계좌 중 안전 조건을 만족한 account 1, 3만 USD 10,000달러로 초기화했다.
- 사용자 수동 삼성전자 BUY 3주와 SELL 3주가 모두 `FILLED`와 trade로 저장됐고, 실현손익은 수수료 포함 `-18.075`였다. 별도의 미체결 지정가 SELL은 그대로 보존했다.
- 18:28 KST 자연 추천 cycle: candidates 100(KR 94/US 6), ranked 99, strategy actionable 5, AI review 5, failure 0, 합의 2, action mismatch 3. 이전 `owner_cycle_failed`는 재발하지 않았고 관리자 화면에 AI 추천 2건이 `PENDING`으로 표시된다.
- 웨일 운영 대시보드에서 최신 완료 cycle, 후보 100→AI 검토 5, 후보별 MCP/sol 판정과 근거 태그, AI 요청/성공률, 뉴스 상태를 실제 화면으로 확인했다.

## 다음 세션이 바로 할 일
1. 다음 PAPER 보유가 자연스럽게 생겼을 때 평가금액·수익률이 실제 시세 변화에 따라 5초 polling으로 바뀌는지 확인한다. SM-S926N 최신 APK 설치, polling 깜빡임 제거, 주문/체결 이름·한국어 상태 표시는 이미 실제 화면에서 검증했다.
2. 현재 운영 브로커·시세 범위는 Toss와 NH PLUG다. KIS는 의도적으로 설정하지 않았으며 사용자가 범위를 바꾸기 전에는 KIS OAuth/account 복구 작업을 하지 않는다.
3. 추천 2건은 아직 `PENDING`이다. 승인·자동실행 결과를 관측하되 강제 BUY, 임계값 완화, Kill Switch/Hard Risk/promotion 우회는 금지다.
4. SHADOW 기능 활성화나 promotion은 포함하지 않았다. 동일 데이터셋 backtest/walk-forward, 새 artifact fingerprint와 별도 PAPER promotion 승인을 거쳐야 한다.
5. 운영 rollback은 runtime image를 `38b38c86`으로 되돌리는 방식이다. DB downgrade나 backup 삭제는 별도 승인 없이는 하지 않는다.

## 세션 이력
- 2026-08-31: PAPER 실시간 평가·USD 자금·뉴스 동기화·AI malformed 응답 격리를 운영 배포하고 자연 cycle/웨일 대시보드/17-job GitHub Actions를 검증.
- 2026-08-31: 국내 스크리너 KRX 세션 만료 fallback 배포, 운영 150종목 복구, 추천·AI·PAPER consumer 실측.
- 2026-08-31: Benchmark RS Ranker 연결, Setup/Risk/Portfolio SHADOW, 내부 MCP sidecar 구현.
- 2026-08-30: 관리자·복구 운영 배포, owner 4 AUTO_PAPER 준비.
