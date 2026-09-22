# 장중 단타용 청산·익절 전략 개선 (ExitStrategy)

RECORD:
DATE: 2026-09-22
SCOPE: position-manager, exit-ladder, partial-profit, trailing-stop, time-stop, atr, paper-automation, portfolio-backtest
PATHS: app/extensions/kasset/automation/position_manager.py, tests/extensions/kasset/automation/test_position_manager.py, tests/extensions/kasset/automation/test_portfolio_backtest.py
STATUS: accepted

## 한 줄 요약

익절선이 하루 안에 닿을 수 없어 매도가 아예 안 나오던 구조를 고쳤다. 1차 익절을 장중 평가가
실제로 판정하는 +0.5 ATR로 내려 보유수량의 30%만 털고, 부분익절 뒤 잔여 수량은 본전 아래로
다시 노출되지 않게 했다. 초기 손절선 `진입가 − 3 ATR`은 손대지 않았다.

## 1. 원인 — 버그가 아니라 계약이 그랬다

2026-09-22 보유 6종목 전부 그날 SELL 추천이 0건이었다. 워커는 정상 실행됐다. 원인은
익절선의 크기다.

KR 후보의 ATR은 진입가의 3~5%다. 그래서 `+3 ATR` 부분익절선은 종목에 따라 **+7.9% ~ +15.9%**가
된다. 그날 6종목의 장중 최대 상승폭은 **+1.16% ~ +3.81%**, ATR로는 **+0.35 ~ +1.17 ATR**이었다.
손절선 `−3 ATR`에도, 익절선 `+3 ATR`에도 구조적으로 닿을 수 없는 거리였다.

운영 DB read-only 조회로 6행 모두 `partial_exit_completed=false`,
`current_stop == initial_stop == 진입가 − 3*ATR`, `highest_close == 진입가`임을 확인했다.
즉 진입 이후 어떤 상태 전이도 일어난 적이 없다.

재현 원문: [`evidence/exitstrategy-session-replay-20260922.txt`](evidence/exitstrategy-session-replay-20260922.txt)

## 2. 고친 것

`app/extensions/kasset/automation/position_manager.py` 한 파일이다. 소스 diff는 +62 / −14.

### 2.1 청산 사다리를 다섯 단으로 정리

`_protective_stop()`을 새로 두고 `evaluate_position`의 보호선 상향을 그 한 곳에서 계산한다.

| 단 | 수준 | 설정 |
|---|---|---|
| 초기 손절 | `진입가 − 3 ATR` | `initial_stop_atr` (**변경 없음**) |
| 조기 보호선 | 진전폭 ≥ `+1 ATR` 뒤 `최고종가 − 2 ATR` | `early_trailing_activation_atr`, `early_trailing_stop_atr` (신설) |
| 1차 익절 | `+0.5 ATR`에서 보유수량의 30% | `partial_profit_atr` 3 → **0.5**, `partial_fraction` 0.5 → **0.3** |
| 잔여 보호선 | `max(진입가, 최고종가 − 3 ATR)` | `post_partial_floor_atr` (신설, 0 = 본전) |
| TIME_STOP | 10봉째 **현재 종가** 진전폭 < `0.5 ATR` | `no_progress_atr` (기준만 변경) |

보호선은 기존 `_raise_trailing_stop`을 그대로 통과하므로 여전히 **단조 상승만** 한다.
어느 단도 초기 손절선을 넓히거나 좁히지 않는다.

### 2.2 TIME_STOP 진전폭을 현재 종가 기준으로

```
progress = max(state.highest_close, bar.close) - state.entry_price   # 이전
progress = bar.close - state.entry_price                             # 이후
```

이전 식은 한 번이라도 올라간 포지션의 진전폭을 영구히 latch해서, 이후 평단 밑에서 계속
기어도 TIME_STOP을 회피할 수 있었다.

## 3. 수치를 고른 근거

`run_portfolio_backtest`로 KR 100종목 × 417 거래일(2025-01-07 ~ 2026-09-22)을 같은 입력으로
놓고 arm을 비교했다. 전체 표와 방법은
[`evidence/exitstrategy-backtest-arms.md`](evidence/exitstrategy-backtest-arms.md)에 있다.

| 항목 | 기준선 | 채택 | 변화 |
|---|---|---|---|
| 총수익 | +3.85% | **+12.49%** | +8.64%p |
| **손익비** | 1.696 | **1.764** | +0.068 |
| 승률 | 42.6% | 50.0% | +7.4%p |
| 사이클 기대값 | +1.92% | +2.94% | +1.02%p |
| MDD | 6.76% | 7.28% | +0.52%p (악화) |
| 포지션 사이클 | 47 | 98 | +51 (회전율 2.1배) |

손익비는 Main이 정한 하한 0.5:1을 크게 웃돌고 기준선보다도 높다.

핵심적으로 배운 것 세 가지다.

1. **어느 단도 단독으로는 기준선을 못 이긴다.** TIME_STOP만 바꾸면 −0.18%/손익비 1.131,
   본전 바닥만 넣으면 −1.72%/1.056, 조기 보호선까지 넣어도 −2.74%/1.222다.
2. **1차 익절만 앞당기면 오히려 나빠진다.** 본전 바닥 없이 `+0.5 ATR` 익절만 넣으면
   +1.64%/1.446으로 기준선보다 못하다. 상승만 잘리고 잔여 수량의 위험은 −3 ATR 그대로이기
   때문이다. 그래서 이 둘은 반드시 함께 간다.
3. **수량은 30%가 50%보다 낫다.** 50%는 승률이 57.1%까지 오르지만 손익비가 1.251로 내려간다.

임계값 0.75 / 1.0 ATR은 총수익·손익비가 함께 내려갔다. 0.5 ATR은 오늘 실제로 움직인
000155(+0.72 ATR)와 005940(+1.17 ATR) 둘 다에서 1차 익절이 나오는 값이기도 하다.

## 4. 오늘 등락을 넣었을 때 수정 전후

운영 state 6행과 `kr_candles_1d`의 2026-09-22 봉을 완료 bucket 하나로 넣고
`evaluate_position_intraday`로 판정했다.

| 종목 | 장중 고가 | ATR 환산 | 수정 전 | 수정 후 |
|---|---|---|---|---|
| 000155 | +3.81% | +0.721 ATR | NONE | **PARTIAL_SELL 30% @ 489,500** |
| 005940 | +3.07% | +1.167 ATR | NONE | **PARTIAL_SELL 30% @ 26,450** |
| 000157 | +1.89% | +0.393 ATR | NONE | NONE |
| 138040 | +1.79% | +0.365 ATR | NONE | NONE |
| 005935 | +1.41% | +0.373 ATR | NONE | NONE |
| 005930 | +1.16% | +0.347 ATR | NONE | NONE |

수정 전 6종목 전부 NONE은 운영 관측(SELL 추천 0건)과 정확히 일치한다. 수정 후에는 실제로
움직인 두 종목에서 1차 익절이 나온다. 두 종목 모두 시가가 이미 익절선 위에서 열려 체결
기준가가 시가가 된다(기존 `open >= target` 규약 그대로).

**조기 보호선과 TIME_STOP 변경은 오늘 데이터에서 한 건도 발동하지 않는다.** 둘 다 완료
일봉 종가 기준의 상태 전이이고, 6종목의 그날 종가는 모두 진입가 ±0.7 ATR 안이며
`highest_close`는 진입가 그대로였기 때문이다.

## 5. 조기 보호선은 백테스트에서 비활성이다 (중요)

ablation을 돌린 결과 `early_trailing_activation_atr`을 켠 것과 끈 것이 **모든 자릿수까지
동일**했다(+12.49% / MDD 7.28% / 98 사이클 / 손익비 1.7635). 1차 익절선이 +0.5 ATR인데
활성화 진전폭이 +1 ATR이라, 활성화에 닿기 전에 부분익절이 먼저 성립하고 부분익절 분기는
보호선 상향 앞에서 early-return하기 때문이다.

그런데도 이 단을 남겼다. `_persistable_state`는 PAPER 체결이 확정되기 전까지
`partial_exit_completed`를 False로 되돌린다. 부분익절 추천이 거절·만료·집행실패로 체결되지
않은 채 가격만 달아나면 포지션은 "부분익절 전" 상태로 전량 노출된 채 남는다. 백테스트는
항상 체결되므로 이 경로를 만들지 못한다. 조기 보호선은 그 경우에만 발동하는 안전망이다.

## 6. 보존한 계약

- **초기 손절 `진입가 − 3 ATR`**: `initialize_position`과 `initial_stop_atr`은 건드리지 않았다.
  회귀 테스트가 `initial_stop == 70`(진입가 100 / ATR 10)을 계속 확인한다.
- **고정 퍼센트 바닥 없음**: 새 수준 전부 `initial_atr` 배수다. 2026-09-07의 −3% 바닥 같은
  절대비율은 도입하지 않았다.
- **시간 소급 방지**: 새 보호선도 `_raise_trailing_stop`을 통과해 정확한 `effective_at`부터
  적용된다. `exit_level_history` 계약과 기존 temporal 회귀 11건 모두 통과한다.
- **우선순위**: `open<=stop`(GAP) → `low<=stop` → PARTIAL → TREND_BROKEN → TIME_STOP 순서와
  장중 전량 손절이 일봉 부분익절보다 우선하는 계약을 그대로 뒀다.
- **장중은 상태를 바꾸지 않는다**: `evaluate_position_intraday`는 한 줄도 바꾸지 않았다.
  저장된 수준만 읽는다. 분봉 종가로 손절선을 올리지 않는다.
- **일봉 백테스트와 라이브의 의미 동일**: 상태 전이는 `evaluate_position` 한 곳에만 있고
  `portfolio_backtest`가 같은 함수를 호출한다. 한쪽에만 적용한 것은 없다.
- **DB 스키마·migration 0건**: 새 상태 필드를 만들지 않았다. 열려 있는 운영 state 행
  6개가 그대로 읽히고, 새 설정은 전부 코드 기본값이다.
- 실주문·강제 sweep·배포를 만들지 않았다. 운영 DB는 조회만 했다.

### 2단 부분익절을 하지 않은 이유

Main의 지시는 "1차 익절 + 잔여는 기존 +3 ATR과 trailing"이었다. 부분익절을 **두 번** 내려면
"1차는 끝났고 2차는 아직"을 구분할 상태가 필요한데, `kasset_paper_position_states`에는
`partial_exit_completed` 불리언 하나뿐이고 새 컬럼은 이번 범위에서 금지다.
`highest_close`에 2차 도달선을 써서 latch하는 우회는 장중 스파이크가 trailing을 끌어올리는
결과가 되어 계약을 깬다. 그래서 **1차 익절 + 본전 바닥 + trailing runner** 구조로 갔다.
잔여 70%는 여전히 `최고종가 − 3 ATR`로 큰 추세를 탄다.

## 7. 계약 변경으로 고친 기존 테스트

| 테스트 | 방어하던 계약 | 고친 이유 |
|---|---|---|
| `test_partial_profit_sells_half_once_at_three_atr` → `..._sells_the_configured_fraction_once` | 부분익절은 한 번만, 도달선 = 진입가 + 배수 ATR, 체결가 = max(시가, 도달선) | 계약은 그대로다. 배수만 출하 기본값에서 명시 config로 옮겨 기본값 변경과 분리했다 |
| `test_close_based_trailing_stop_only_applies_from_next_bar` | 종가 기준 상향은 다음 봉부터 적용 | 계약 유지. 종가를 140으로 올려 trailing(110)이 본전 바닥(100)보다 높은 구간에서 확인하도록 입력만 바꿨다 |
| `test_intraday_partial_target_uses_stored_entry_and_atr` | 장중 익절선은 저장된 진입가·ATR로 계산 | 위와 같은 이유로 명시 config를 넘겼다 |
| `test_partial_fill_keeps_same_cycle_and_marks_remaining_state` | 체결 확정 뒤에야 잔여 상태가 갱신된다 | 잔여 보호선 기대값 98 → 100. 본전 바닥이 trailing(128−3 ATR=98)을 이기는 새 계약이다 |
| `test_three_arms_are_independent_under_the_same_risk_cost_and_exit_rules` (`test_portfolio_backtest.py`) | 세 진입 arm이 같은 위험·비용·청산 규칙을 쓴다 | `trades[0].exit_reason == "TIME_STOP"`은 설정값이 정하는 부수 결과였다. 고정 문자열 대신 "청산은 공용 Position Manager의 ExitKind에서 나온다"로 바꿨다 |

### 추가한 회귀 테스트 7건

`test_position_manager.py`에 넣었다. 전부 수정 전 소스에서 실패하고 수정 후 통과한다.

- `test_intraday_partial_fires_inside_the_observed_session_excursion` — 2026-09-22 000155의
  실제 운영 state와 정규장 구간으로 1차 익절이 나오는지. 출하 기본 수량 30%도 함께 고정한다.
- `test_unreachable_partial_target_leaves_the_session_without_any_exit` — 같은 구간에서
  익절선이 +3 ATR이면 신호가 0건임을 보존한다(이번 결함의 재현).
- `test_early_trailing_protects_profit_before_any_partial_exit` — 부분익절 전 보호선 상향과
  다음 봉 TRAILING_STOP.
- `test_early_trailing_stays_off_until_the_activation_progress` — 활성화 전에는 초기
  −3 ATR 손절선 그대로.
- `test_partial_exit_keeps_trailing_without_a_second_activation` — 부분익절 뒤에는 활성화
  진전폭을 다시 요구하지 않는다.
- `test_partial_exit_lifts_the_runner_stop_to_the_atr_floor` — 잔여 수량이 본전 아래로
  노출되지 않는다.
- `test_time_stop_is_not_evaded_by_an_earlier_high_close` — 과거 최고 종가 latch로 TIME_STOP을
  회피하지 못한다.

## 8. 검증

저장소 규약 14에 따라 로컬 Windows에서는 아무것도 실행하지 않았다. 서버
`root@100.73.186.78`의 전용 checkout `/tmp/kasset-exit-20260922`와 일회성 container
(`--network none`, 1~2 CPU, 3 GiB)에서 실행했다. 운영 checkout `/opt/kasset-trader-core`,
`.env.kasset`, 운영 DB/볼륨은 쓰지 않았다.

```text
python -m pytest tests/extensions/kasset/automation/test_position_manager.py \
  tests/extensions/kasset/automation/test_portfolio_backtest.py \
  -q --tb=short -p no:cacheprovider \
  --deselect ...::test_closed_cycle_survives_position_delete_as_audit
```

| 명령 | 결과 |
|---|---|
| 위 집중 pytest (`test_position_manager.py` + `test_portfolio_backtest.py`) | **141 passed / 1 deselected, exit 0** |
| `test_portfolio_backtest.py` + `test_strategy_promotion.py` + `test_shadow_manifest.py` | **113 passed, exit 0** |
| `ruff check --no-cache` (변경 3파일) | All checks passed, **exit 0** |
| `ruff format --check --no-cache` (변경 3파일) | 3 files already formatted, **exit 0** |
| `ty check --error-on-warning` (`position_manager.py`) | All checks passed, **exit 0** |

- 이미지에 `uv`가 없어 `uv run ruff` / `uv run ty` 대신 프로젝트 잠금 venv의
  `/app/.venv/bin/ruff`, `/app/.venv/bin/ty`를 직접 호출했다. 같은 도구·같은 버전이다.
- `ruff`는 `/w`가 root 소유라 캐시 생성에 실패한다. `--no-cache`를 붙여야 한다.
- 운영 이미지에는 dev 의존성이 없어 `pytest-asyncio` 등을 넣은 일회성 이미지
  `kasset-exit-test:local`을 만들어 썼다.

### 실행하지 못한 것

- `test_job.py`는 이 container에서 140 errors로 수집 단계에서 멈춘다. **base(HEAD) checkout에서
  같은 명령이 똑같이 140 errors**이므로 이번 변경과 무관한 환경 제약이다(run-owned DB
  bootstrap이 붙지 않는다). 권한 있는 후속 통과가 실행해야 할 정확한 명령:
  `python -m pytest tests/extensions/kasset/automation/test_job.py -q --tb=short -p no:cacheprovider`
- `test_closed_cycle_survives_position_delete_as_audit`은 실 DB가 필요해 deselect했다.
- `run_walk_forward` fold별 안정성은 확인하지 못했다. 채택값은 전 구간 단일 실행 비교다.

## 9. 남은 경계 (Main 판단 필요)

1. **회전율이 2.1배로 는다** (사이클 47 → 98). 청산이 빨라져 자금이 일찍 풀리고 재진입이
   늘기 때문이다. 사용자가 제기한 "매수가 너무 많이 나간다"와 같은 방향이므로 진입 조각과
   함께 볼 필요가 있다. 진입 임계값은 지시대로 손대지 않았다.
2. **`promotion_evidence.py:1134-1141`이 설정 필드를 손으로 나열한다.** 신설 3필드
   (`earlyTrailingActivationAtr`, `earlyTrailingStopAtr`, `postPartialFloorAtr`)가 승격 evidence에
   빠진다. 소유 경로 밖이라 고치지 않았다. 단, `strategy_artifact.py`는
   `PositionManagerConfig()` 객체를 통째로 정규화하므로 **fingerprint 자체는 새 필드를 반영한다.**
3. **`docs/kasset/AUTOMATION_BREAKOUT_CONTRACT.md:90`**이 "부분익절선은 진입가 + 3 ATR"로
   적혀 있다. `docs/`는 아무도 건드리지 않기로 했으므로 갱신 대상만 남긴다.
4. **strategy fingerprint가 바뀐다.** `position_manager.py`는 `STRATEGY_CODE_PATHS`에 있어
   원래 이 파일을 고치면 언제나 바뀌지만, 배포 후 기존 promotion 승인 행과 런타임
   fingerprint가 어긋나면 `promotion_runtime_fingerprint_mismatch`로 자동 PAPER가 막힌다.
   배포 절차에서 확인이 필요하다.
5. **소유 경로 밖 수정 1건**: `tests/extensions/kasset/automation/test_portfolio_backtest.py`.
   `OWNED_PATHS`에 없지만 브리프의 "기존 테스트가 계약 변경으로 깨지면 고친 이유를 보고한다"에
   따라 고쳤다. 다른 maker가 소유하지 않은 파일이다.
6. **MDD를 더 중시한다면 대안값이 있다.** `post_partial_floor_atr = 0.5`(잔여 보호선을
   1차 익절선에 고정)는 총수익 +12.47%로 사실상 같고 MDD가 6.16%로 가장 낮다. 대신 손익비가
   1.380으로 내려간다. 채택값은 손익비를 우선해 0(본전)으로 했다.
