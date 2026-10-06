# 일일 목표 잠금 백테스트 · STALE_PRICE 세션 수 기준 · 제외 증거 키 통일 — Maker DailyTargetReduction
RECORD: maker-DailyTargetReduction
DATE: 2026-10-06
SCOPE: 일일 목표 수익률 잠금(EXIT_ONLY) 백테스트, STALE_PRICE 연휴 오탐, 감사 원장 제외 증거 키 exclusionReason
PATHS: app/extensions/kasset/automation/market_session.py, app/extensions/kasset/automation/position_sizing.py, app/extensions/kasset/automation/promotion_evidence.py, app/extensions/kasset/automation/vertical_slice.py, tests/extensions/kasset/automation/test_market_session.py, tests/extensions/kasset/automation/test_position_sizing.py, tests/extensions/kasset/automation/test_vertical_slice.py, doc/history/2026/10/06-daily-target-reduction/
STATUS: accepted

## 결정

- **목표 잠금(③→①)**: 사용자 결정(2026-10-06)에 따라 ③ 백테스트 근거를 먼저 만든 뒤 ①로 가기로 했다. 이 백테스트는 ①(목표 도달 시 신규 BUY 정지 → 0.5배 축소)을 지지하지 않았다(아래 「③ 결과」). 그 숫자를 사용자에게 그대로 보여 준 뒤 **사용자가 현행 잠금 유지를 선택했다(2026-10-06). ①은 취소**됐다. `account_state_gate.py`와 `docs/API-CONTRACT.md`의 잠금 설명은 바꾸지 않았다.
- **STALE_PRICE**: Main이 허용 세션 수 N=3 계획을 승인했다(2026-10-06). 세 번째 항목 「제외 증거 키」는 체크포인트 없이 진행하도록 Main이 지정한 표시 수정이다.

## ③ 결과: 일일 목표 잠금 vs 0.5배 축소 vs 관문 없음

### 방법과 근사

- 엔진: `portfolio_backtest.py::run_portfolio_backtest`(수정 없음, 같은 입력·같은 기간). 현행 `PortfolioBacktestConfig()` 기본(청산 설정 `PositionManagerConfig()`, KR 수수료 0.15%·불리한 슬리피지 0.10%, 다음 봉 시가 체결)을 쓰고, 계좌 관문은 연구 스크립트가 한 실행 동안 모듈 함수 4개(`_execute_pending_exits`, `_queue_entries`, `_execute_pending_entries`, `calculate_position_size`)를 감싸 얹었다. 반복 실행을 빠르게 하려고 이력에만 의존하는 `CandidateRanker.rank`·`_assess_regimes`·`_evaluate_entry_path`를 메모했다. **관문 없는 실행 C의 `determinism_hash`가 엔진을 그대로 돌린 첫 실행과 일치함(모든 입력에서 `equals_unpatched_hash=true`)**으로 감싸기·메모가 결과를 바꾸지 않음을 확인했다.
- 일중 평가자산의 근사(일봉에는 일중 경로가 없다): 기준 자산 = 그날 시가 체결 직전 보유를 시가로 평가한 자산(운영 HWM의 "그 거래일 첫 가치평가"에 대응). 판정 자산 = `close` 근사는 현금 + Σ수량×당일 종가(운영 마지막 15:20 사이클의 상태에 가깝다), `high` 근사는 현금 + Σ수량×당일 고가(종목 고가는 동시에 닿을 수 없어 잠금이 가장 자주 걸리는 **상한**). 판정 비율 = 판정 자산 / 기준 자산 − 1. 당일 종가에 큐잉되는 신규 진입(다음 거래일 시가 체결, 엔진 관례)이 그날 비율의 상태를 받는다. 운영 `account_state_from_shadow`처럼 비래칭이다. 일중 진입 시각 분포는 모른다. 진입이 하루에 걸쳐 흩어지는 운영은 close 근사(하루 중 가장 늦은 시점의 상태를 모든 진입에 적용)와 "잠금이 하루 종일 안 걸림" 사이에 있다.
- 변형: C 관문 없음 / A 목표 이상이면 신규 BUY 0(브리프 (a), 현행 잠금) / A+ 현행 근사(목표의 절반 이상 ×0.75, 목표 이상 0; 고점 대비 하락 단계는 일중 고점을 몰라 생략) / B75 목표 이상 ×0.75 / B50 목표 이상 ×0.5(브리프 (b)) / T 절반 이상 ×0.75·목표 이상 ×0.5 / S75 절반 이상 ×0.75 하나 / S50 절반 이상 ×0.5 하나.
- 목표율: `policy.py` 프리셋 0.8%(risk 3)·1.2%(risk 4)·2.0%(risk 5). 계좌 7은 0.8%, 계좌 4는 9/28 이후 2.0%다(라이브 행 evidence).
- 입력: 서버 읽기 전용 SQL(`SET default_transaction_read_only=on`), 코호트 A `67f1059a…`(KR 시총 상위 100, 9/28 연구와 같은 코호트) 일봉 42,070행(2025-01-07~2026-10-02), 코호트 B(`toss_openapi` 2026-08-29 시총 순위 101~200 중 일봉 400봉 이상, 코호트 A 제외) 100종목 40,500행. 둘 다 "현재 시총 상위" 선정이라 생존편향이 있다. 네트워크 없는 컨테이너 `kasset-trader-core:f1d8bf4b…` + 같은 커밋 checkout, `--cpus` 0.7~1.0, 15:33~15:56 KST(장 마감 뒤).
- 구성(슬롯 수와 사이징): 엔진 기본 5슬롯, 10슬롯, 운영 3~5단계(보유 제한 없음)에 가깝게 12슬롯 + 프리셋 사이징(risk3 0.75%·종목 20%, risk5 1.5%·종목 30%).

### 결과 (총수익% (MDD%), close 근사, 변형 열은 위 정의)

| 구성 | 목표 | C 무관문 | A 잠금 | A+ 현행근사 | B75 | B50 | T | S75 | S50 | C 완료 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 5슬롯 코호트A | 0.8% | 17.8 (9.8) | 13.2 (10.4) | 13.5 (9.8) | 17.3 (9.8) | 16.9 (9.8) | 17.2 (9.6) | 17.7 (9.6) | 18.0 (9.3) | 162 |
| 5슬롯 코호트A | 1.2% | 17.8 (9.8) | 16.3 (9.9) | 15.9 (10.0) | 17.8 (9.8) | 17.8 (9.8) | 17.3 (9.9) | 17.3 (9.9) | 16.7 (10.1) | 162 |
| 5슬롯 코호트A | 2.0% | 17.8 (9.8) | 11.8 (10.8) | 11.9 (10.8) | 17.7 (9.8) | 17.7 (9.8) | 17.7 (9.8) | 17.8 (9.8) | 18.0 (9.8) | 162 |
| 5슬롯 코호트B | 0.8% | -3.9 (9.6) | -7.9 (11.9) | -11.4 (15.1) | -3.9 (9.7) | -3.9 (9.7) | -5.0 (10.3) | -4.8 (10.1) | -5.9 (10.6) | 141 |
| 5슬롯 코호트B | 1.2% | -3.9 (9.6) | -8.7 (14.3) | -8.4 (14.0) | -3.9 (9.7) | -4.0 (9.9) | -4.0 (9.8) | -3.9 (9.7) | -3.9 (9.8) | 141 |
| 5슬롯 코호트B | 2.0% | -3.9 (9.6) | -4.1 (10.1) | -4.4 (10.2) | -3.8 (9.5) | -3.8 (9.5) | -3.8 (9.7) | -3.9 (9.7) | -4.0 (9.9) | 141 |
| 10슬롯 코호트A | 0.8% | 6.9 (14.8) | 22.4 (14.8) | 22.0 (14.0) | 6.5 (14.8) | 6.2 (14.7) | 6.6 (14.4) | 6.9 (14.5) | 7.0 (14.0) | 284 |
| 10슬롯 코호트A | 2.0% | 6.9 (14.8) | 21.7 (13.0) | 21.2 (13.0) | 7.5 (14.7) | 8.1 (14.7) | 7.4 (14.6) | 7.1 (14.6) | 7.1 (14.2) | 284 |
| 12슬롯·risk3 코호트A | 0.8% | 12.5 (11.0) | 21.1 (10.0) | 20.5 (9.9) | 12.7 (10.9) | 12.8 (10.9) | 11.2 (10.8) | 11.5 (10.9) | 11.1 (10.7) | 306 |
| 12슬롯·risk3 코호트B | 0.8% | -0.6 (11.0) | 2.1 (9.1) | 1.8 (9.0) | -0.6 (10.8) | -0.4 (10.5) | -0.3 (10.4) | -0.5 (10.7) | -0.3 (10.2) | 276 |
| 12슬롯·risk5 코호트A | 2.0% | 21.0 (19.6) | 35.6 (18.7) | 33.7 (18.6) | 21.2 (19.5) | 22.0 (19.2) | 20.1 (19.4) | 19.3 (19.8) | 17.9 (19.9) | 310 |
| 12슬롯·risk5 코호트B | 2.0% | -1.0 (21.0) | 3.4 (18.0) | 4.5 (16.7) | -1.0 (20.8) | -1.2 (20.7) | -1.2 (20.1) | -1.1 (20.3) | -1.4 (19.3) | 276 |

Sharpe·승률·손익비·기대값·잠금이 막은 진입 수·축소 체결 수는 원본 JSONL(`evidence/lock-backtest-*.jsonl`, 변형마다 한 줄)에 있다. 대표값: 코호트A 5슬롯 0.8% C 17.84% / MDD 9.78% / Sharpe 1.10 / 완료 162건(미청산 5) / 승률 38.9%, A 13.20% / 10.44% / 0.85 / 163건 / 38.0%, 잠금이 막은 신호 101건(재큐잉 포함). B50은 같은 구성에서 16.89% / 9.85% / 1.07, 축소 체결 9건.

- **잠금(A) 대 무관문(C)**: 총수익 차이가 5슬롯에서 −4.6·−1.5·−6.0%p(코호트A), −4.0·−4.8·−0.2%p(코호트B)로 6칸 중 6칸 음수(close 근사)이고, 10~12슬롯에서 +15.6·+14.8·+8.6·+2.7·+14.7·+4.4%p로 6칸 중 6칸 양수다. **부호가 슬롯 수에 따라 뒤집힌다.** 운영(3~5단계 보유 제한 없음)에 가까운 구성은 후자다.
- **0.5배·0.75배 축소(B50·B75·T·S75·S50) 대 무관문**: 12개 close 구성에서 총수익 차이가 대부분 ±1.3%p 안(가장 큰 값 코호트B 5슬롯 0.8% S50 −2.0%p, 12슬롯 risk5 S50 −3.1%p)이고 MDD도 거의 같다. 즉 목표 도달 뒤 축소는 무관문과 비슷하며 잠금이 10~12슬롯에서 냈던 이득도 못 가져오고, 5슬롯에서 잠금이 냈던 손실도 피한다.
- **잠금일(close 근사 비율 ≥ 목표)에 큐잉된 진입의 평균 수익(C 실행)**: 5슬롯 코호트A 0.8% 9건 +3.12% vs 나머지 153건 +2.38%(차이 없음), 코호트B 9건 −1.68% vs −1.33%. 10슬롯 41건 +0.31% vs 243건 +0.95%, 12슬롯 risk3 A 34건 +0.33% vs 272건 +1.45%, B 25건 −1.05% vs −0.38%, risk5 A 26건 −0.54% vs +1.20%. 운영에 가까운 구성에서 잠금일 진입이 평균적으로 더 나빴다.
- **high 근사**(잠금이 가장 자주 걸리는 상한; 위 표의 high 판은 원본 JSONL): 부호가 더 섞인다(5슬롯 A 잠금 −19.8%p, 12슬롯 코호트B 잠금 +5.7%p, 10슬롯 +12.5%p). 슬롯 수가 많은 구성에서 축소가 MDD를 줄이는 경향이 보이지만 수익은 구성마다 엇갈린다.
- 잠금일 수(코호트A 5슬롯, 포지션이 있던 171일 중, 목표 0.8%): close 근사 17일(10%), high 근사 69일. 목표 2.0%는 close 2일, high 8일뿐이다. 라이브(`evidence/live-hwm-days.txt`)는 25 owner-일 중 4일(계좌 7: 10/1·10/2·10/6, 계좌 4: 10/6)이 목표에 닿았다.

### 표본 한계와 읽는 법

- 일봉 엔진이라 일중 진입 시각·일중 경로가 없고 close/high 근사가 이를 대신한다. HANDOFF의 "기존 일봉 백테스트는 새 장중 집행 전략의 성과 검증이 아니다"가 그대로 적용된다. 5분봉 재생(장중 보정)은 `research.kr_candles_5m_toss`가 1분봉 위의 뷰라 15:30 직후 전체 조회가 DB 컨테이너 CPU를 70%까지 올려 3분 만에 `pg_cancel_backend`로 취소하고 시도를 접었다. 근사의 일중 오차는 측정하지 못했다.
- 잠금일 진입 표본이 9~41건, 목표 2.0%는 잠금일이 1~14일이라 통계적 확정을 못 한다. 같은 설정에서도 슬롯 수만 바꿔 부호가 뒤집혔으므로 총수익 차이를 정책 근거로 쓰는 데에는 경로 의존이 너무 크다. 두 코호트는 서로 겹치지 않지만 같은 기간·같은 시장 국면(코호트B는 모든 변형이 손실)이라 독립 표본이 아니다.
- 운영 PAPER(계좌 4·7)는 일중 돌파 단타(NH 실시간 관문, 10분 후보·5분 집행)라 일봉 엔진의 다음 시가 체결과 청산 사다리는 대리 모형이다.

### 라이브 사실 (읽기 전용 DB, 모두 2026-10-06 15:1x KST)

- `kasset_shadow_daily_high_watermarks`: 행 evidence의 목표율 기준으로 계좌 4는 9/7~10일 3%, 9/11~23일 5%, 9/28 이후 2%이고 계좌 7은 0.8%다. 목표 도달일은 위 4일. 10/06 두 계좌는 15:10 기준 +2.649%(목표 2.0%), +1.414%(목표 0.8%)다.
- 10/06 10:50 사이클부터 두 계좌 모두 사이클당 11~12개 후보가 감사 원장에서 사라지고 추천이 0건이었다(`evidence/live-lock-signature.txt`). **원인은 감사 원장 투영이 `exclusionReason`이 없는 행을 버리는 것이다**(아래 「제외 증거 키」). 이 때문에 잠금으로 막힌 후보의 종목·가격은 저장되지 않아 막힌 진입의 사후 성과는 계산하지 못했다.
- 구현 사실: 코드의 `STAGED_REDUCTION_MULTIPLIER`는 0.5가 아니라 **0.75**이다(`account_state_gate.py:36`, 앱 UI·HANDOFF도 0.75). 목표의 절반·고점 대비 하락 단계가 이미 ×0.75이고 목표 이상은 ×0이다. 앱은 `hardRisk.accountState.state/multiplier`를 읽어 EXIT_ONLY·STAGED_REDUCTION 문장을 만든다. 이 사실들은 ①이 취소돼 적용되지 않지만 다시 논의될 때를 위해 남긴다.

## STALE_PRICE: 가격 나이를 정규장 세션 수로 센다

- 원인: `position_sizing.py`의 `max_price_age=timedelta(days=4)`와 `>=` 비교. 가격은 `vertical_slice.py`의 `price_as_of=ranking.data_as_of`(마지막 완료 일봉, KRX 세션일 09:00 KST)다. 금요일 봉이 월요일 휴장 뒤 화요일 09:00:17에 평가되면 4일 + 17초라 하루 종일 STALE이었다. 운영 감사 원장에서 9/28에 302건, 10/06에 298건 `presizing_zero_quantity:STALE_PRICE`가 나왔고 평상시는 0건이다. 4일은 8/30 첫 커밋(`393197293`)에 근거 없이 들어온 값이다.
- 변경: `PositionSizingConfig.max_price_age`(4일)를 `max_price_age_sessions=3`으로 교체했다. 가격 timestamp가 속한 정규장은 **그 timestamp보다 뒤에 닫히는 첫 정규장**이다(일봉 timestamp 규약이 공급자마다 달라 날짜 산술 대신 쓴다: KIS는 UTC 자정, Toss는 현지 자정). 그 세션 이후 평가 시각까지 **이미 열린** 정규장 세션 수가 3을 넘으면 STALE이다. 주말·휴장일은 세지 않는다. `market_session.py::regular_sessions_opened_since`(공용 달력 `session_calendar`만 사용, KRX=XKRX·US=XNYS)를 추가했다. 달력이 세션을 확정하지 못하거나(조회 창 ±12일 밖의 오래된 가격 포함) 시장 라벨이 달력에 없으면 STALE(fail-closed). FUTURE_PRICE(가격 > 평가 시각)와 INVALID_PRICE_TIMESTAMP(시간대 없음)의 판정과 순서는 그대로다. 가격과 평가 시각이 같은 일봉 백테스트 엔진은 달력을 부르지 않고 0세션이라 결과가 바뀌지 않는다. `promotion_evidence.py`의 `sharedExecutionConfig.positionSizing.maxPriceAgeSeconds`는 `maxPriceAgeSessions`로 바뀌었다(이 키를 읽는 소비자는 코드에 없다).
- N=3 근거: 현행 4일 규칙이 평일 구간에서 통과시키는 최대 격차가 3세션(월→목)이고 월→금(4세션)부터 STALE이다. 열린 세션 수는 경과 일수 이하라서 N=3이면 **지금 통과하는 경우가 새로 STALE이 되는 일이 없고** 주말·휴장만 세션에서 빠진다. 평상시 직전 완료 봉은 1세션이다. 느슨해지는 쪽은 주말을 낀 수·목요일 봉을 월요일에 평가하는 경우(실제로 2~3세션 늦은 가격)다.
- 전후 스모크(`evidence/stale-price-smoke.txt`, 실제 달력): 금 10/02 봉 → 화 10/06 09:00:17 KST는 quantity 0(STALE) → 489. 수 9/23 봉 → 월 9/28 09:00:17(추석)도 0 → 489. 월 9/28 봉 → 금 10/02(4세션)와 월 9/21 봉(오래됨)은 전후 모두 STALE. US 금 10/02 봉 → 월 10/05 15:00Z는 전후 모두 통과.
- **fingerprint**: `position_sizing.py`·`market_session.py`·`vertical_slice.py`는 `STRATEGY_CODE_PATHS`라 배포하면 전략 fingerprint가 바뀐다. 막히지 않는 근거(Main 확인): 9/29 이후 계좌 4·7의 SUBMITTED 94건이 모두 `promotion_bypass_reason=promotion_bypassed_by_owner`(10/06 09:38·09:52 포함)이고 `job.py:330` `promotion_bypassed = self._automatic and snapshot.promotion_bypass`다.

## 제외 증거 키: 감사 원장에 남기기

- 원인: `vertical_slice.py`의 same-symbol pending·reentry exhausted·account_state_gate 제외 dict가 `reason` 키를 썼고, 감사 투영 `app/services/kasset_automation_audit.py::_candidate_exclusions`는 `exclusionReason`이 없는 행을 버린다(다른 생산자 `candidate_ranker.py:262`, `vertical_slice.py` 트리거·사이징 제외는 `exclusionReason`). 그래서 10/06 사이클의 `candidate_exclusion_count`는 15인데 저장 행은 4개였고, `source='account_state_gate'` 직접 조회는 0행이었다.
- 변경: 세 dict의 키만 `exclusionReason`으로 바꿨다(`vertical_slice.py` 954·970·1013행). 관문 판정·카운터(`preAiExclusions`)·`continue` 흐름은 그대로다. 감사 투영 코드는 소유 경로 밖이라 그대로 둔다. 그 키를 읽던 소비자는 테스트 두 곳뿐이었다.
- 회귀 테스트: `test_vertical_slice.py`에 `_audit_exclusions`(= `build_automation_cycle_event(...).candidate_exclusions`)를 두고 `exit_only`(`test_exit_only_is_counted_before_sizing`), `same_symbol_reentry_exhausted`, `same_symbol_pending_recommendation` 세 사이클 결과가 투영 출력에 `symbol`·`market`·`reason`·`source`와 함께 남는지 단언한다. 키를 바꾸기 전 소스에서는 세 테스트가 모두 실패한다(아래 「검증」).

## 변경 파일

- `app/extensions/kasset/automation/market_session.py`: `regular_sessions_opened_since` 추가, `__all__`.
- `app/extensions/kasset/automation/position_sizing.py`: `max_price_age_sessions`, `_stale_price_reason`.
- `app/extensions/kasset/automation/promotion_evidence.py`: `maxPriceAgeSessions` 키.
- `app/extensions/kasset/automation/vertical_slice.py`: 제외 evidence 키 3곳(관문 판정 변경 없음).
- `tests/extensions/kasset/automation/test_market_session.py`, `test_position_sizing.py`, `test_vertical_slice.py`.
- 변경 없음(취소·비목표): `account_state_gate.py`, `docs/API-CONTRACT.md`, `kasset_automation_audit.py`, 스케줄러·주문·레저, 하드 인바리언트.
- 일회성 연구 스크립트 `daily-target-lock-backtest.py`는 저장소 밖(`local://daily-target-lock-backtest.py`, 서버 사본 `/tmp/kasset-daily-target-20261006/lock_backtest.py`)에 두었다. 최종본 SHA-256 `d58ed167df4c27d2fe7fa74e872d7089273d28e3efe0bcdce2adc712387258d7`는 12슬롯 preset 실행에 썼고, 5슬롯 코호트 A·B와 10슬롯 실행은 `--top-n`·`--risk-per-trade`·`--max-symbol-allocation` 인자를 추가하기 전 `af9457cefd376360673f54aa97b8d77d5e44306e350a4d85207b6a4acf64a525`를 썼다(계산 경로는 같고 CLI 인자만 다르다). 입력 CSV와 검증용 checkout은 서버 `/tmp`에만 있고 커밋하지 않으며, 작업 마감 때 CSV·checkout은 지우고 스크립트·SQL·산출 JSONL만 남긴다.

## 재현

- 입력: `extract-daily-A.sql`(코호트 A)·`extract-daily-B.sql`(코호트 B)을 `docker exec -i kasset-trader-db-1 psql`에 stdin으로 넣어 `COPY … TO STDOUT`을 받는다(서버 `/tmp/kasset-daily-target-20261006/`). 입력 SHA-256: A `1c409d33…`, B `49efd9d8…`(재추출 시 일봉 갱신으로 달라질 수 있다).
- 실행: 서버 `/tmp/kasset-daily-target-20261006/lock_backtest.py <csv> /dev/stdout --label <라벨> --targets 0.8,1.2,2.0 [--max-positions N --top-n N --risk-per-trade R --max-symbol-allocation S]`를 `docker run --rm --network none --cpus=0.7~1.0 --memory=3g`로 돌린다(위 구성별 인자). 산출 원본: `evidence/lock-backtest-{A,B,A-maxpos10,preset3-A,preset3-B,preset5-A,preset5-B}.jsonl`.
- 각 JSONL의 `kind`: `meta`(설정·변형 정의), `unpatched`(엔진 그대로 첫 실행), `run`(변형별 지표), `lock_day_entries`(잠금일 진입 통계), `daily_series`(일별 close/high 비율), `calibration_days`는 5분봉 시도를 접어 없다.
- 실행 명령 원문과 CPU·시각은 최종 보고의 명령 목록과 서버 `err-*.log`에 있다.

## 검증

서버 `kasset-server`, cwd `/tmp/kasset-dtr-src`(배포 커밋 `f1d8bf4b` clone + 이 작업 변경 7개 파일 overlay), 이미지 `kasset-trader-core:f1d8bf4ba1046001717e8d5fd3562491f31a3dc0`, 볼륨 `kasset-pytest-deps-04d62828-swing`(uv.lock 핀과 동일 확인, ruff 0.15.9·ty 0.0.29 포함), 컨테이너 `kasset-pytest-dtr`(`--cpus=1.0 --network container:kasset-test-db`, 운영 DB·운영 컨테이너와 분리, run-owned DB `test_db_pytest_*`). 2026-10-06 16:20~16:30 KST 실행. [server-pytest-runner](../../../../docs/runbooks/server-pytest-runner.md) 절차.

| 명령 | 종료 코드 | 결과 |
|---|---:|---|
| `ruff check --no-cache` 변경 7개 파일 | 0 | All checks passed! |
| `ruff format --no-cache --check` 같은 파일 | 0 | 7 files already formatted |
| `ty check --error-on-warning` 같은 파일 | 0 | All checks passed! |
| `python -m pytest -q --tb=short -p no:cacheprovider tests/extensions/kasset/automation tests/services/test_kasset_automation_audit.py tests/schemas/test_ai_recommendations_schema.py` | 0 | `880 passed, 12 warnings in 579.48s`, `test schema bootstrap: databases=1 applied=1`, `ROB-1296 … 0 blocked requests`, `ROB-1880 … blocked_attempts=0` |
| 수정 전 소스(`vertical_slice.py`만 배포본으로 되돌린 복사본)에서 `pytest -q --tb=line tests/extensions/kasset/automation/test_vertical_slice.py -k "exit_only_is_counted or exhausted_reentry or unlimited_reentry"` | 기록 안 함(출력을 `tail`로 파이프해 쉘 코드는 `tail`의 0) | `3 failed, 47 deselected`, `KeyError: 'exclusionReason'` — 키를 바꾸기 전에는 같은 세 테스트가 실패한다 |

- 전체 pytest 로그 원문은 서버 `/tmp/kasset-dtr-run.log`에 남겨 두었다. 서버 `kasset-pytest-deps-4e6329d1` 볼륨에는 ruff·ty가 없어(TossMinuteGapRepair도 같은 확인) 핀이 같은 `-swing` 볼륨을 읽기 전용으로 썼다.
- 운영 격리 확인(runbook 8절): 실행 직후 `information_schema.tables` public 테이블 수는 운영 DB에서 110이다(runbook 기록 2026-09-23의 109보다 줄지 않았다. 늘어난 원인은 확인하지 않았다). 운영 컨테이너 `db·redis·api·worker·scheduler·mcp·ai-mcp·caddy·nh-stream`은 모두 Up이고 테스트·백테스트 컨테이너는 남지 않았다.
- 일봉 백테스트 엔진 회귀: 같은 880건에 `tests/extensions/kasset/automation/test_portfolio_backtest.py`·`test_exit_rule_comparison.py`가 포함되고 통과했다(`price_as_of == evaluated_at`이라 달력을 부르지 않는 경로).
- 전후 스모크(실제 달력): `evidence/stale-price-smoke.txt`. 격리 스크립트 실행 exit 0 두 번.
- 미실행: 5분봉 일중 보정(DB 부하로 취소), 별도 앱 저장소 쪽 수정·검증(범위 밖).

## 남은 위험

- 일봉 근사가 일중 잠금 시점과 다르다. 일중 5분 재생은 하지 못했다.
- 사용자가 현행 잠금 유지를 선택했으므로 코드 변경은 없다. 다만 앱 문구와 `account_state_gate.py:36` 상수 0.75 불일치 같은 사실은 ①이 다시 열리면 먼저 확인해야 한다.
- STALE 변경은 전략 fingerprint를 바꾼다(bypass 계정은 영향 없음). 세션 수 판정은 요청마다 ±12일 창으로 달력을 조회한다(후보당 수 ms). 호출은 `_pre_ai_sizing` 후보 수만큼이다.
- 제외 키 통일로 이제부터 `candidate_exclusions`에 same-symbol·account_state 제외 행이 저장되어 사이클 행이 커진다(상한은 기존 `_MAX_EXCLUSIONS=50`이 그대로 적용, 앞쪽에 넣는 기존 순서 유지).
