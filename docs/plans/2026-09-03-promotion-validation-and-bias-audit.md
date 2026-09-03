# 승격 검증 파이프라인 정비 — forward 트랙, 비용/체결 규약 감사, 분포 지표

작성: 2026-09-03

외부 저장소 두 곳의 **방법론만** 이식했다. 포크나 의존성 추가는 없다.

- `github.com/giovannibrusco/nq-intraday-breakout` (MIT) — `SimFlags` 규약 플래그와 `tools/bias_attribution.py`의 누적 Δ 표.
- `github.com/zachisit/july-backtester` (MIT) — `helpers/monte_carlo.py`의 block bootstrap 분포.

## 왜 필요했나

세 가지가 동시에 성립하고 있었다.

### 1. 승격이 구조적으로 불가능했다

- `promotion_evidence._require_readiness`는 코호트 `evidence_scope == "historical_pit"`을 요구했다.
- `app/models/kasset_research_cohorts.py`의 CHECK 제약은 `evidence_scope = 'forward_paper'` 단일 값만 허용했다.
- `kasset_research_cohort_service`도 현재 universe 기반이라 historical PIT를 주장하지 않고 `forward_paper`로만 저장한다.

즉 저장소 코드로 생성 가능한 정상 코호트로는 `backtest-build`가 절대 성공할 수 없었다. 성과 임계값을
낮춰도 승격은 되지 않고 낮춘 임계값만 남는 상태였다.

### 2. 백테스트 비용/체결 모델이 실거래와 달랐다

|항목|백테스트(변경 전)|실거래 PAPER|
|---|---|---|
|체결|신호 다음 봉 open, `execution_delay_bars=1`|submit 시점 quote로 즉시|
|슬리피지|KR 0.0010 / US 0.0005|0|
|수수료|KR 0.0015 / US 0.0010|KR 0.00015 + 매도세 0.0018, US 0.0007(최소 $1)|

KR 왕복은 백테스트 50bp vs 실거래 21bp다. 방향은 보수적이지만 **구성이 틀렸다.** 실거래 최대 비용
항목인 매도세 18bp가 모델에 아예 없었고 US의 최소 수수료도 없었다. 승격은 후보 간 순위 비교로
결정되므로 회전율·시장 간 순위가 왜곡된다.

### 3. risk-adjusted 지표와 분포 검정이 없었다

Sharpe·Calmar가 bias-audit/backtest 결과에 없어 위 규약 차이를 비교할 축이 없었고, 분포 기반
robustness 관측도 없었다.

## 무엇을 했나

### P0 — `evidence_track` 도입

- `strategy_promotion.PROMOTION_EVIDENCE_TRACKS = ("historical_pit", "forward_paper")` 단일 정의.
  (`promotion_evidence`에 두면 순환 import가 되어 여기에 둔다. `promotion_evidence`가 재노출한다.)
- `promotion_thresholds_for_track()` — `forward_paper`만 `require_survivorship_evidence=False`.
  **성과 임계값은 두 트랙 동일하다.** MDD 0.20, win_rate 0.40, trade_count 30, folds 3,
  pass_rate 0.67 전부 그대로다.
- `readiness.py`에 `HISTORICAL_PIT_ONLY_BLOCKERS` 단일 정의와 keyword-only `evidence_track`.
  기본 `promotion_ready`/`blockers`는 공통 운영 readiness 계약을 보존한다. historical PIT 전용
  blocker는 `historical_evidence_ready`/`historical_evidence_blockers`에만 남고,
  `promotion_evidence._require_readiness`가 historical 증거 생성 시 이를 엄격하게 검사한다.
- forward 트랙의 생존편향 방어는 **다른 방식으로 낸다**: 시그널/주문 생성 시작이 코호트
  `effective_date` 이후여야 한다. 그 이전 봉은 지표 warm-up으로만 쓰인다.
  `_forward_walk_forward_bars`가 입력 bars를 사전 슬라이스해 첫 fold `train_end`가 첫 forward 봉에
  오도록 맞춘다(`run_walk_forward`에는 외부 `signal_start_at` 인자가 없다).
- fold를 3개 만들 수 없으면 임계값을 낮추지 않고 `forward_window_insufficient_bars`로 실패한다.
  코호트 확정 후 forward 봉 61개(1 + 20 + 2×20)가 쌓여야 fold 3개가 생긴다.
- forward 증거는 `survivorship_evidence=False`로 저장한다. **PIT survivorship을 거짓 주장하지 않는다.**
  payload에 `evidenceTrack`, `validation.forwardSignalStartAt`, `validation.historicalPitChecksWaived`를
  남겨 사후 감사가 트랙을 구분할 수 있게 한다.
- 알 수 없는 트랙 문자열은 어느 경로에서도 fail-closed다.
- DB CHECK 제약은 **건드리지 않았다.** forward 트랙은 기존에 허용된 `forward_paper` scope를 그대로
  쓰므로 확장이 필요 없고, 확장하면 "코호트가 PIT 출처를 자칭할 수 없다"는 DB backstop 한 겹만
  사라진다(독립 검수 M-1). `historical_pit` 코호트 writer는 PIT builder와 함께 별도 범위에서 만든다.
- `strategy_promotion_service`의 candidate 재평가와 기본 승인이 저장된 트랙의 임계값을 쓴다.
  이것 없이는 forward 후보가 `candidate_evaluation_mismatch`로 승인 단계에서 막혔다.

### P1 — 비용 프로파일과 체결 규약 플래그

- `MarketExecutionCost`에 `sell_tax_rate`, `min_fee_absolute` 추가. 기본값 0이라 기존 결과 불변.
  수수료는 `max(notional * fee_rate, min_fee_absolute)`, 매도세는 매도 notional에만 부과하고
  `fees_paid`에 섞지 않고 `taxes_paid`로 분리한다(구성을 섞은 것이 문제의 반이었으므로).
- `CONSERVATIVE_COST_PROFILE`(현행), `LIVE_MATCHED_COST_PROFILE`(실거래 일치). 후자는 숫자를
  복사하지 않고 `paper_trading_service.FEE_RATES`에서 파생한다.
- `entry_fill ∈ {next_open, signal_close}`, `slippage_mode ∈ {adverse_rate, none}`. 기본값은 현행.
  `signal_close`는 lookahead를 뒤따라오는 가정이므로 결과에 `LOOKAHEAD_RISK=signal_close_counterfactual`
  evidence를 남긴다.
- `scripts/kasset_bias_audit.py` — 같은 봉·같은 후보에 규약을 하나씩 누적 적용한 4행 표.
  각 Δ는 위의 모든 변경이 적용된 상태에서의 한계 효과이며, position sizing이 running equity를
  따라가므로 경로 의존이다. 이 각주는 항상 출력한다.
- 저장 payload에 `entryFill`, `slippageMode`, `costSlippage.sellTaxRate`, `costSlippage.minFeeAbsolute`,
  `taxesPaid`를 기록한다. `costSlippage`는 `_experiment_identity`의 구성요소이므로 매도세만 다른 두
  실행이 같은 identity를 갖는 일이 없다.
- **승격 저장 증거는 `entryFill == "next_open"`, `slippageMode == "adverse_rate"`여야 한다.**
  위반은 `promotion_entry_fill_invalid` / `promotion_slippage_mode_invalid`로 거절된다. lookahead
  반사실(`signal_close`) 빌드가 승격 증거로 저장되는 경로를 구조적으로 닫는 것이다. 키가 없는 과거
  payload는 기본값으로 해석해 계속 통과한다.

### P3 — Sharpe / Calmar

`equity_curve`의 **거래일 격자만** 사용한다. 캘린더일 padding/ffill 금지. 연율화 계수는 실제 격자
봉 수에서 유도하고 252를 상수로 박지 않는다. 무위험 수익률은 0.

Sharpe·Calmar는 bias-audit와 backtest 결과의 감사 지표이며, 저장된 `PromotionMetrics` 필드나
승격 임계값은 아니다.

이 제약은 nq 저장소가 실증한 결함을 피하기 위한 것이다. 그쪽에서는 equity를 365 캘린더일로 채우고
252로 연율화해 벤치마크 Sharpe가 0.6375 → 0.7683으로 움직였고(비율 1.2052 ≈ √(365/252) = 1.2036)
그것이 전체 Sharpe 변화의 최대 단일 원인이었다. **회귀 테스트로 이 비율을 pin해 재발을 막는다.**

### P2 — block bootstrap advisory

`app/extensions/kasset/automation/trade_bootstrap.py`. 고정 seed, 순환 block(기본 크기 `isqrt(N)`),
1,000 path. 출력은 `pnl_p5`, `pnl_p50`, `historical_pnl_percentile`, `max_drawdown_p50/p95`.
거래 30건 미만이면 `None`(`PromotionThresholds.min_trade_count` 재사용).

july의 `mc_score`/`mc_verdict` 점수화(`score += 2`, 임계 0.50/0.80)는 근거 없는 임의 가중치라
가져오지 않았다. 기본 표집도 july는 iid지만 여기서는 연승·연패 군집 보존을 위해 block을 기본으로 한다.

`tradeBootstrap`은 **어떤 승인·거절 판정에도 연결하지 않는다.** `evaluate_thresholds`는 손대지 않았고
`determinism_hash` 입력에도 들어가지 않는다.

## 가져오지 않은 것과 이유

- **july를 의존성/포크로 편입**: `helpers/*`가 전역 `config.CONFIG`를 모듈 내부에서 직접 import하는
  구조라 서비스에 넣을 수 없다. KR 시장·NXT/KRX venue 개념도 없다.
- **july의 `evaluate_wfa` 판정 로직**: `helpers/wfa_rolling.py:get_fold_dates`가 fold 0의 OOS 시작을
  `actual_start`로 잡아 fold 0의 IS가 빈 리스트가 된다. 그러면 `evaluate_wfa`의 두 조건(부호 반전,
  75% 열화)이 `is_pnl_pct is None` / `is_ann is None`에서 모두 건너뛰어져 **OOS 거래 5건만 있으면
  무조건 `"Pass"`**를 반환한다. rolling pass rate가 fold 하나만큼 구조적으로 부풀려진다.
  KAsset의 기존 fold-pass 정의가 더 낫다.
- **july의 PIT universe 데이터 경로**: 미국 지수 전용 외부 YAML 저장소 2개에 의존한다. KRX/KOSDAQ
  대응물이 없다.
- **nq의 전략 규칙과 수치 전부**: NQ 선물 전용이다.

## 다음 작업과 남은 위험

1. **forward 트랙 실제 실행은 아직 없다.** 코호트 확정 후 forward 봉 61개가 쌓이기 전에는
   `forward_window_insufficient_bars`로 실패한다. 임계값을 낮추는 방식으로 우회하지 않는다.
2. **어느 cost profile을 승격 기준으로 삼을지 미결정.** `bias-audit` 표를 실데이터로 본 뒤 정한다.
   `live_matched`가 자동으로 옳은 것은 아니다. 슬리피지 0%는 실거래 PAPER 쪽의 비현실적인 부분이고
   백테스트를 그쪽에 맞추면 낙관 방향으로 이동한다.
3. **DB 마이그레이션은 없다.** 이번 변경은 스키마를 바꾸지 않는다. `historical_pit` 코호트를 실제로
   만들려면 CHECK 확장과 PIT builder를 같은 범위에서 함께 만들어야 한다.
4. **forward 트랙은 `effective_date` 당일 봉(`>=`)부터 시그널을 허용한다.** readiness의 기존
   `membership_period_usable`(`>=`)과 맞췄다. selection이 그 날 종가를 반영한다면 엄격히는 `>`가
   맞으므로, 코호트 확정 시각 규약을 확인한 뒤 재검토할 여지가 있다.
5. **intraday trigger 임계값 sweep은 착수 불가.** `intraday_data.py`, funnel 계측이 운영 서버 로컬
   브랜치 `fix/us-intraday-and-ai-veto`에만 있고 버전관리 밖이다. 그리고 승격 엔진은 1m/5m 봉을 전혀
   읽지 않는다. **조합 탐색을 시작하면 다중검정이 되어 pass_rate 0.67을 탐색만으로 만족시킬 수 있다.**
   그때는 july의 sensitivity sweep이 아니라 이미 저장소에 있는
   `research_contracts/honest_offline_gate.py`(sealed OOS, DSR, CSCV PBO, BH-FDR)에 KAsset payload를
   연결해야 한다.
