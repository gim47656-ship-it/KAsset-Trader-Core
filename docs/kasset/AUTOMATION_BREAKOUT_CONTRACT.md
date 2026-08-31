# KAsset PAPER Breakout Automation Contract

갱신: 2026-08-29

## 목적과 경계

현재 `KAsset-Trader-Core`의 APPROVAL/AUTO_PAPER 흐름에 결정론적 후보 순위, ATR 위험 수량, 보유 포지션 관리, 실전 모듈 재사용 포트폴리오 백테스트를 추가한다. 외부 저장소를 포크하거나 런타임에 yfinance를 추가하지 않는다. LIVE 주문 경로는 만들지 않는다.

## 현재 구조 감사

- `vertical_slice.py`는 관심종목을 먼저 넣고 최신 `InvestScreenerSnapshot`을 거래대금·거래량 순으로 채운 뒤, KRX 후보가 50개 미만일 때 실시간 Screener로 보완한다. PAPER 보유종목은 후보군에 강제 포함되지 않는다.
- 후보별 최근 60개 일봉을 `DailyCandlesRepository`에서 읽고 4개 기존 전략과 `assess_market_regime`/`compose_weighted_ensemble`을 실행한다. 별도 cross-sectional factor rank는 없다.
- `policy.py::portfolio_plan`은 운영예산과 종목 최대비중만으로 수량을 정한다. ATR, 손절폭, 유동성, Regime 위험배율은 수량 계산에 쓰지 않는다.
- `consumer.py::PaperAutomationConsumer`는 APPROVED recommendation만 claim하고, 주문 직전 PAPER preview와 owner policy/Kill Switch를 다시 읽은 뒤 `PAPER` facade에 idempotency key `ai-rec:{recommendation_id}`로 제출한다. LIVE facade는 없다.
- `backtest.py`는 단일 종목·단일 전략 long-only next-bar backtest다. Candidate Ranker, ensemble, portfolio budget, Position Manager를 재사용하지 않는다.
- `AIRecommendation.evidence` JSONB는 후보 factor, position sizing, exit 상태, promotion 상태를 추가 저장할 수 있다. 기존 action 제약은 BUY/SELL/HOLD/WATCH이므로 50% 분할익절도 action은 SELL, evidence의 `exitKind=PARTIAL_SELL`로 표현한다.

## 재사용과 최소 변경

| 요구 | 기존 재사용 | 추가 경계 |
|---|---|---|
| 후보 데이터 | `DailyCandlesRepository`, `InvestScreenerSnapshot`, `SymbolMaster`, watchlist, KRX Screener provider | `candidate_ranker.py` |
| 전략/시장 | `STRATEGIES`, `assess_market_regime`, `compose_weighted_ensemble` | 기존 구현 유지 |
| 수량/위험 | `AITradingLimits`, `AITradingUsage`, `evaluate_hard_risk` | `position_sizing.py`; `policy.py`는 owner DB 조회와 Hard Risk 유지 |
| 보유 관리 | `PaperPosition`, `AIRecommendationService`, `PaperAutomationConsumer` | `position_manager.py`와 owner-scoped 상태 모델 |
| 주문 | 기존 PAPER preview/submit facade | 변경 없음; Position Manager는 직접 호출 금지 |
| 백테스트 | `PriceBar`, 동일 Ranker/Strategy/Regime/Ensemble/Sizer/Manager | `portfolio_backtest.py` |
| 활성화 | 기존 strategy version | owner-independent promotion 상태 모델 |

## 결정론 계약

### Candidate Ranker

입력은 후보 metadata, 일봉, 시장 또는 cross-sectional benchmark 수익률, Screener 유동성, `as_of`, 중앙 config다. 출력은 다음 불변 필드를 가진다.

- `symbol`, `total_score`, `factor_scores`, `penalties`
- `data_as_of`, `valid_until`, `exclusion_reason`, `evidence`
- 후보 출처와 `is_held`, `is_watchlisted`

Factor는 유동성, benchmark 대비 relative strength, 20/60/120일 모멘텀, 20일/52주 신고가 거리, HH/HL, ATR 수축, 돌파 전 거래량 감소, 최근 거래량 증가, 주봉 추세를 포함한다. 돌파선 이격 과열과 비정상 거래량 폭발 뒤 미진정 상태는 감점한다. 미래 bar, stale bar, 비정상 OHLCV, 최소 가격·거래대금·데이터 길이 미달은 점수 보정이 아니라 `exclusion_reason`으로 fail-closed한다. 동일 입력의 정렬 tie-breaker는 symbol이다.

후보군은 관심종목 → 현재 PAPER 보유종목 → 최신 Screener 유동성 상위 → 필요 시 KRX 실시간 Screener 순으로 합치되, 보유종목은 candidate limit 밖이어도 평가한다. Ranker 상위만 기존 전략·AI 검토로 전달하고 전체 factor evidence를 recommendation에 보존한다.

### Position Sizer

BUY 수량은 다음 상한의 최솟값이다.

1. `(운영예산 × 거래당 위험률 × Regime 배율) / abs(진입가 - 손절가)`
2. 종목 최대비중에서 기존 투자액을 뺀 추가 수량
3. 남은 운용예산 수량
4. 시장 최소 주문단위로 내림한 수량
5. 중앙 config의 평균 거래량/거래대금 참여율 상한

손절가 없음·역전, ATR 없음/비정상, stale/future 가격, 비정상 가격/수량, 예산 없음은 수량 0과 구조화 사유를 반환한다. AI 출력은 수량·손절가를 입력하거나 덮어쓸 수 없다. SELL은 실제 PAPER 보유수량까지만 허용한다.

### Position Manager와 position cycle

owner/account/market/symbol별 활성 상태는 실제 `PaperPosition.id`와 immutable `position_cycle_id`에 결합한다. 신규 BUY 체결 시 체결가·추천 ATR/stop·entry order·strategy identity/fingerprint로 새 cycle을 만들고, 재시작 시 PAPER 보유량과 reconcile한다. 수량 0이 되면 상태를 삭제하지 않고 `closed_at`으로 닫아 감사 이력을 보존한다.

동일 종목 재진입은 과거 `highest_close`, trailing stop, partial-exit 상태를 재사용하지 않는다. 부분 매도는 잔여 수량과 stop 상태를 유지하고 전량 청산은 같은 cycle의 미처리 청산 신호를 무효화한다. 청산 idempotency key에는 cycle이 포함된다. 결과는 recommendation만 생성하며 Broker/PAPER facade를 직접 호출하지 않는다.

백테스트의 stop gap은 stop 가격 체결로 소급하지 않는다. 다음 거래 가능 봉 시가와 설정된 보수적 slippage를 적용한다.

### Portfolio Backtest, readiness와 Promotion

백테스트는 Candidate Ranker, 기존 Strategy/Regime/Ensemble, Position Sizer, Position Manager의 같은 pure 계산 함수를 호출한다. 신호 bar까지의 데이터만 전달하고 다음 거래 가능 bar에서 체결한다. KRX/US별 수수료·slippage, 1x/2x/3x stress, walk-forward, 기간·Regime 성과, 거래수·승률·기대값·MDD·회전율·benchmark 초과성과, 종목 제거와 1-bar 지연 민감도를 계산한다.

승격 evidence는 새 백테스트 체계를 만들지 않고 기존 `ResearchStrategyExperiment → ResearchBacktestRun → ResearchPromotionCandidate` registry를 사용한다. DB 일봉에서 종목 수, 251/252봉, stale/future/duplicate/OHLC 이상, 거래일 누락, corporate-action 상태, point-in-time·상장폐지 포함 가능 여부, KOSPI/KOSDAQ/SPY benchmark 범위를 계산한다. evidence가 부족하거나 fallback benchmark뿐이면 승격을 fail-closed한다.

전략 상태는 `DRAFT`, `BACKTESTED`, `PAPER_APPROVED`, `PAPER_SUSPENDED`, `RETIRED`다. 운영자는 persisted candidate ID와 사유만 넘길 수 있고 raw metrics를 CLI로 주입할 수 없다. 승인·추천 생성·AUTO_PAPER submit 직전의 strategy artifact fingerprint가 모두 같아야 하며, Ranker/Regime/Ensemble/Sizer/Manager/Backtest/비용 설정과 schema evidence version 변경은 새 backtest/promotion을 요구한다. Git SHA는 source lineage로 별도 저장하고 문서·UI·테스트 변경은 artifact fingerprint에서 제외한다.

### Claim lease와 submit 복구

추천 claim은 `CLAIMED|SUCCEEDED|FAILED` 상태와 opaque token, claimed/lease 시각, attempt count를 사용한다. 만료된 `CLAIMED`만 새 token으로 회수할 수 있고 이전 worker의 token은 완료 상태를 쓸 수 없다. 주문 identity는 기존 `ai-rec:{recommendation_id}`를 재사용하며 account별 correlation ID가 유일하다.

submit 결과가 불명확하면 즉시 실패나 재전송으로 단정하지 않고 `CLAIMED` lease를 남긴다. 재실행은 owner-scoped client ID 조회로 기존 PAPER 주문을 먼저 reconcile하고, 같은 owner·예상 client ID의 주문만 `SUCCEEDED`로 확정한다. 결정적 preview/submit 거절만 `FAILED`다. 별도 `PREVIEWED`, `SUBMITTING`, `UNKNOWN`, `RECONCILING` persisted 상태와 heartbeat 열은 같은 복구 사실을 중복 표현하므로 추가하지 않는다.

### 운영 CLI

운영 진입점은 `python scripts/kasset_paper_ops.py` 하나다.

1. `readiness [--as-of ISO-8601]`로 일봉·benchmark·PIT readiness를 확인한다.
2. `backtest-build [--as-of ISO-8601]`로 DB 일봉을 사용한 diagnostics/walk-forward를 실행하고 기존 registry에 저장한다.
3. `promotion-status`로 현재 승격과 artifact fingerprint를 확인한다.
4. `promotion-draft|promotion-approve --candidate-id ID --reason TEXT`는 persisted candidate만 받는다.
5. `promotion-suspend|promotion-retire --strategy-key KEY --version VERSION --reason TEXT`로 운영 상태를 닫는다.

운영 DB migration, backfill, scheduler 활성화, 실제 주문은 이 CLI가 자동 실행하지 않는다. 현재 데이터가 252봉/PIT/benchmark 조건을 충족하지 못하면 `PAPER_APPROVED`를 만들지 않는다.

## AI 공급자 역할

- 후보 factor, 수량, stop, exit와 deterministic backtest/promotion metrics는 AI를 호출하지 않는다.
- 추천의 설명·검토만 AI provider를 사용한다.
- 복잡한 후보/거래 검토는 MCP 직결을 우선하고 direct OpenAI-compatible API, OpenRouter 순으로 availability fallback한다.
- 뉴스·공시 요약은 direct API 담당으로 두고 OpenRouter fallback을 사용한다. OpenRouter fallback 모델은 공식 slug `z-ai/glm-5.3-flash`다.
- 일반 뉴스 structured output은 `summary`(한국어 2~4문장)와 `translated_title`, `translated_excerpt`를 분리한다. 번역 필드는 각 원문이 영문 우세일 때만 생성하며, 본문 앞부분 4,000자만 입력하고 번역 발췌는 6,000자 이하로 저장한다. 한국어 title/body의 대응 번역 필드와 본문이 없을 때의 `translated_excerpt`, 기존 분석 행의 두 필드는 `null`을 허용하고 원문 URL은 `/market/news`와 daily routine alert에 그대로 제공한다.
- provider 429는 availability failure로 다음 configured provider에 넘긴다. 나머지 4xx·refusal·schema·safety 오류는 fail-closed한다.
- 최종 선택되어 `AIRecommendation`으로 저장된 건은 provider/tier/exact model ID, normalized input hash, 허용된 validated response, confidence, 선택 사유를 `ai_shadow` evidence로 남긴다. raw prompt·secret·provider envelope는 저장하지 않는다. 통계 범위는 `persisted final selections only`이며 선택되지 않은 후보를 저장했다고 간주하지 않는다.

## 참고 출처와 라이선스

알고리즘 아이디어는 MIT License의 [VladPetrariu/Qullamaggie-breakout-scanner](https://github.com/VladPetrariu/Qullamaggie-breakout-scanner)를 참고했다. 재사용 아이디어는 multi-factor breakout ranking, relative strength, HH/HL, ATR compression, volume contraction/expansion, weekly confluence, ATR risk sizing, partial profit/trailing/time stop, walk-forward·stress·counterfactual 검증이다. 소스 파일을 복사하지 않고 KAsset의 Decimal·timezone·owner scope·PAPER safety 계약에 맞게 독립 구현한다.

## 2026-08-31 SHADOW 확장 상태

| 기능 | 구현 | 기본 활성 | 실제 주문 영향 |
|---|---|---:|---|
| 후보별 Benchmark RS | KRX는 KOSPI/KOSDAQ, 미국은 SPY 60-session 수익률 | 사용 중 | 기존 Ranker evidence 확장 |
| First Pullback / NR7 / Inside Day | 완결봉 공통 detector와 상위후보 evidence | `false` | 없음 |
| Account High-Watermark | owner/account/market/date별 영속 SHADOW 상태 | `false` | 없음 |
| Loss-Streak | 종료 PAPER 손실 거래의 scope·expiry·dedupe 관찰 | `false` | 없음 |
| Soft Top-K / Sector Cap | 순수 목표비중 비교 계산 | `false` | 없음 |

`ShadowActivation`의 모든 필드는 기본값이 `false`다. 활성 설정 fingerprint는
기존 strategy artifact fingerprint와 분리돼 있고 `promotionEligible=false`다.
현재 런타임에는 이 값을 켜는 환경변수나 관리자 API가 없다. 따라서 SHADOW 기능을
주문 입력으로 승격하려면 별도 코드 변경, 동일 데이터셋 backtest/walk-forward,
새 artifact fingerprint와 PAPER 승격 승인이 모두 필요하다.

### 활성화 전 절차

1. 신규 migration `20260831_kasset_shadow_hwm`,
   `20260831_kasset_shadow_loss_lock`을 별도 비운영 DB에서 먼저 검증한다.
2. KRX 활성화·승격 전에
   `python scripts/backfill_daily_candles.py --market kr --benchmark-only --horizon-bars 400`
   으로 KOSPI와 KOSDAQ을 모두 적재하고 각각 최소 61봉인지 확인한다.
3. `ShadowAutomationManifest`와 활성 설정 fingerprint를 증거에 고정한다.
4. 비용 포함 portfolio backtest, walk-forward, 기존 주문결과 동일성 회귀를 실행한다.
5. owner scope, Kill Switch, Hard Risk, Position Manager SELL 우선 계약을 재검증한다.
6. SHADOW 관찰 기간을 거친 뒤에도 실제 주문 연결은 별도 변경과 승인을 거친다.

### 롤백

- 주문 연결 전: activation을 모두 `false`로 되돌린다. 기존 APPROVAL/AUTO_PAPER
  경로와 주문 결과는 바뀌지 않는다.
- 주문 연결 후: 새 연결 코드를 되돌리고 새 promotion을 `PAPER_SUSPENDED`로 닫는다.
  Kill Switch나 기존 Hard Risk를 우회해 복구하지 않는다.
- HWM/Loss-Streak 이력은 감사용으로 보존한다. 스키마 downgrade나 운영 데이터 삭제는
  별도 파괴적 변경 승인 없이는 실행하지 않는다.

### Clean-room 참고

- `xang1234/stock-screener@22f96f6f11b03e54037e2937a58bdb6530e67bbe`
  (Apache-2.0): First Pullback, NR7/Inside Day의 추상 판정 개념.
- `QuantConnect/Lean@b692bf4788e8b54fc23bdcb5659666bf055ce89f`
  (Apache-2.0): portfolio high-watermark와 drawdown predicate 개념.
- `freqtrade/freqtrade@5fc5faeae7033ed5a83c1eecc8160828f5ee0d2e`
  (GPL-3.0): 최근 stop-loss 군집의 lookback/scope/expiry 개념만 조사.
- `microsoft/qlib@79633dd9506ea689e5400dea0197717b5b3d74b7`
  (MIT): top-k 목표비중과 step별 변화 상한 개념.

어느 저장소에서도 코드, 테스트, 상수, reason 문자열, fixture, 모듈 구조를 복사하지
않았다. 특히 GPL-3.0 Freqtrade는 조사 보고서의 추상 behavior spec만 사용해
프로젝트 고유 `Decimal`·timezone·owner scope 계약으로 독립 구현했다.
