# HANDOFF — KAsset Trader Core
갱신: 2026-08-30 (PAPER 자동화 신뢰경계·배포 lineage·benchmark window 보강 완료)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset Trader Core는 스크리너, 시세·뉴스·공시, 전략, AI 분석, PAPER/LIVE 주문 원장과 Android API를 제공한다. 현재 목표는 **PAPER에서 재현 가능한 추천·승격·주문·청산을 충분히 검증한 뒤 별도 승인으로 LIVE를 검토**하는 것이다. 일일 목표를 이유로 거래를 만들거나 AI가 Hard Risk를 우회하면 안 된다.

정본 운용 계약:

1. **APPROVAL**: 추천 → 사용자 승인 → PAPER 주문.
2. **AUTO_PAPER**: persisted backtest candidate, exact strategy/version `PAPER_APPROVED`, 동일 strategy artifact fingerprint, submit 직전 Hard Risk·Kill Switch·owner scope 재검증을 모두 통과한 PAPER 주문만 자동 실행한다.
3. AI는 후보 factor·수량·stop·exit·backtest metrics를 만들거나 덮어쓰지 않는다. 추천 설명·검토만 담당한다.
4. LIVE 주문 경로·운영 배포·운영 migration은 별도 사용자 승인 전까지 열지 않는다.

## 전체 진행 상태
- **코드 완료**: persisted promotion evidence CLI, immutable strategy artifact fingerprint, 실제 `PaperPosition`에 결합된 position cycle, claim lease/fencing과 불명확 submit reconciliation, 일봉 readiness/benchmark evidence를 구현했다.
- **Promotion fail-closed**: 기존 `ResearchStrategyExperiment → ResearchBacktestRun → ResearchPromotionCandidate` registry만 신뢰한다. CLI로 raw metrics를 주입할 수 없고 candidate ID와 운영자 사유만 받는다. evidence 부족·fallback-only benchmark·선택 시장/평가 window 불일치·fingerprint 불일치는 승격/주문을 막는다.
- **현재 데이터 readiness 미충족**: 마지막 read-only 운영 감사에서 KR은 100종목×60봉, US는 0종목이며 252봉 충족 종목은 0이었다. 기준을 낮추거나 가짜 backtest로 승인하지 않았다.
- **Position lifecycle 완료**: 신규 BUY 체결가·추천 ATR/stop으로 fresh cycle을 만들고 `paper_position_id`, market, opened/closed 시각, strategy identity/fingerprint를 보존한다. 전량 청산은 soft-close하고 같은 종목 재진입은 과거 trailing/partial 상태를 재사용하지 않는다.
- **Claim 복구 완료**: `CLAIMED|SUCCEEDED|FAILED`와 token/lease/attempt count를 사용한다. 만료 claim만 회수하고 stale worker 완료 쓰기를 거부한다. `ai-rec:{recommendation_id}`로 기존 주문을 먼저 조회하며, 불명확 submit은 즉시 재전송하지 않고 3회 attempt 초과 poison claim은 `FAILED`로 종결한다.
- **AI shadow 완료**: 최종 선택되어 저장된 recommendation에 exact provider/tier/model ID, normalized input hash, validated response, confidence, 선택 사유를 secret-free evidence로 남긴다. 통계 범위는 `persisted final selections only`다.
- **429 보강 완료**: direct/MCP provider의 429는 availability fallback으로 처리하고 나머지 4xx·refusal·schema·safety 오류는 fail-closed한다.
- **CI gate 완료**: 4개 고정 shard가 모든 non-live test 파일을 정확히 한 번 포함한다. `Test` workflow는 `workflow_dispatch`와 PostgreSQL 15에서 ROB-849 경계를 재구성한 뒤 후속 전체 migration을 downgrade/upgrade하는 전용 job을 가진다.
- **운영 미배포**: `20260830_kasset_position_cycles → 20260830_kasset_promotion_trust → 20260830_kasset_claim_lease` migration을 운영 DB에 적용하지 않았고 scheduler/LIVE 경로를 변경하지 않았다.
- **실기기 검증 보류**: Android 계약 회귀 unit test는 통과했지만 사용자가 아침에 할 실물기기 확인은 남아 있다.

## 이번 세션에서 한 일
- `scripts/kasset_paper_ops.py`에 `readiness`, `backtest-build`, `promotion-status`, `promotion-draft`, `promotion-approve`, `promotion-suspend`, `promotion-retire`를 추가했다. 실제 주문·migration·scheduler 활성화는 하지 않는다.
- strategy-influencing code와 유효 설정, evidence schema version을 canonical fingerprint로 묶었다. Git SHA는 source lineage로 별도 저장하며 문서·UI·테스트 변경은 fingerprint에서 제외한다.
- recommendation 생성, promotion 승인, AUTO_PAPER submit 경계의 fingerprint를 3중 비교한다. 변경된 코드에 과거 승격을 재사용할 수 없다.
- position 상태를 실제 PAPER 보유 cycle에 결합하고 부분 매도·전량 청산·재진입·재시작 reconcile·owner/account/market 격리를 추가했다.
- claim lease/token CAS, 만료 회수, owner-scoped client ID 조회, account별 correlation ID 유일성, 불명확 submit 복구를 추가했다. 별도 `PREVIEWED/SUBMITTING/UNKNOWN/RECONCILING` 상태와 heartbeat 열은 같은 사실의 중복 표현이라 만들지 않았다.
- DB 일봉 readiness에서 251/252봉, stale/future/duplicate/OHLC 이상, 거래일 누락, corporate action 상태, PIT/상장폐지 근거, KOSPI/SPY benchmark 범위를 계산한다.
- 선택된 recommendation의 AI route metadata와 validated verdict를 `ai_shadow` evidence에 보존하고 read-only 통계를 추가했다. 선택되지 않아 durable row가 없는 후보를 저장했다고 꾸미지 않는다.
- GitHub Actions에 PostgreSQL 15 migration round-trip job을 추가하고 stale fixture-only test manifest entry를 제거했다.
- Alembic 기본 `version_num VARCHAR(32)`에 맞게 미배포 KAsset revision ID 2개를 단축하고 실제 PostgreSQL round-trip에서 후속 migration 전체를 검증했다.
- 기존 CI formatter/type gate에서 드러난 KAsset 관련 포맷 drift와 DART receipt number 타입 narrowing을 정리했다.
- 배포 이미지의 source lineage는 유효 `GITHUB_SHA` → `/app/.build-vcs-ref` → 개발환경 `git rev-parse` 순으로 읽는다. 배포 이미지에 `git`/`.git`이 없어도 artifact를 만들며 lineage는 fingerprint에 섞지 않는다.
- promotion evidence schema를 v2로 올리고 baseline benchmark 시장 집합과 평가 시작/종료 window가 선택 시장·포트폴리오 기록 구간 전체를 덮지 않으면 승격을 차단한다.
- position exit 추천도 entry와 동일한 strategy key/version/artifact fingerprint를 보존한다. 한 번도 기록되지 않던 `entry_order_id` 컬럼/FK는 미배포 migration과 모델에서 제거했다.
- promotion 운영자 경로의 row lock 순서를 통일하고 PAPER claim 재시도를 3회로 제한했다.

검증:

- KAsset automation/AI/API/PostgreSQL 집중 스위트: **391 passed**.
- CI workflow·shard exact-cover 계약: **93 passed**.
- Promotion/fingerprint/CLI 집중 스위트: **40 passed**.
- DART content fetcher 회귀: **20 passed**.
- `ruff check app/ tests/ research/ scripts/` 및 `ruff format --check ...`: 통과.
- `ty check app/ --error-on-warning`: 통과.
- `alembic heads`: `20260830_kasset_claim_lease (head)` 단일 head.
- Android `:app:testDebugUnitTest`: `BUILD SUCCESSFUL`.
- 실제 PostgreSQL 15에서 ROB-849 경계 재구성 → 이전 revision downgrade → current head upgrade → 재다운그레이드 → 재업그레이드와 단일 head 확인이 통과했다. 과거 TimescaleDB 연속 집계 migration은 이 KAsset 전용 회귀 job의 검증 범위가 아니며 수정하지 않았다.
- 최종 reviewer finding 보강 범위(Promotion artifact/benchmark, position exit identity, claim cap, 실제 PostgreSQL migration round-trip): **70 passed**.

주요 커밋:

- `60825851` CI shard manifest
- `72093187` AI 429 fallback
- `92276eef` position cycle lifecycle
- `76d52b2f` data readiness/benchmark
- `1008d998` immutable strategy fingerprint
- `06f92737` promotion evidence/CLI
- `3b6383eb` claim lease/reconciliation
- `d0259e08` selected recommendation AI shadow
- `c38f7c15` PostgreSQL 15 migration CI gate
- `8706709c` repository format/type gate 정리
- `bb42b91a` deployment lineage·benchmark window promotion trust
- `bbb045b8` position exit strategy provenance·dead entry-order 제거
- `5a8adf26` PAPER claim attempt 상한

## 다음 세션이 바로 할 일
1. 아침에 S24+에서 기존 APPROVAL/AUTO_PAPER 화면과 설정 저장을 확인한다. Core 운영 미배포 상태의 422는 구 계약 차단이며 앱 오류 처리 확인용이다.
2. 운영 배포 승인을 받기 전에는 migration/backfill/scheduler를 실행하지 않는다. 승인 시 DB backup·현재 Alembic head를 확인하고, `review.ai_recommendations`의 고아 `(owner_user_id, paper_order_id)`와 `paper.paper_trades`의 중복 `(account_id, correlation_id)`를 read-only probe로 각각 0건 확인한 뒤 migration을 적용한다.
3. 운영 일봉을 KR/US 각각 252봉 이상과 PIT·상장폐지·benchmark 근거까지 보강한다. minimum을 낮추지 않는다.
4. readiness가 통과한 뒤 `backtest-build`로 persisted candidate를 만들고 evidence/hash를 검수한 다음에만 `promotion-approve`를 실행한다.
5. KRX 개장 중 APPROVAL 추천→승인→PAPER fill/reconcile을 검증한다. AUTO_PAPER는 승격 후 소액으로 duplicate submit, claim lease 회수, kill switch, partial/full exit, 재진입을 확인한다.
6. LIVE, 1m/5m, VWAP, ORB, 시간대 상대거래량, 섹터 최대노출, 계좌 high-watermark, 목표수익 위험축소, Meta Label/Factor Weight는 이번 범위 밖이며 별도 결정 전 변경하지 않는다.

## 세션 이력
- 2026-08-30: PAPER promotion evidence/CLI, artifact fingerprint, position cycle, claim lease, readiness/benchmark, AI shadow, migration CI gate 완료.
- 2026-08-29: 결정론적 PAPER 자동화·exact-version 승격 gate, 추천 시장·일일 횟수, AI 공급자·뉴스 경계 완료.
- 2026-08-29: DART 운영 수집·문서 fallback·일반 뉴스 AI 요약·5단계 PAPER 정책 완료.
- 2026-08-29: 기간별 candle/session cutover, 뉴스/공시 파이프라인과 Android 연동.
