# 2026-09-30 — 흐름 청산 최소 평가익 0.3%·초기 손절 −2 ATR

RECORD:
DATE: 2026-09-30
SCOPE: REALTIME_TREND_EXIT 최소 평가익, PositionManagerConfig.initial_stop_atr
PATHS: app/extensions/kasset/automation/realtime_tape.py, app/extensions/kasset/automation/position_manager.py
STATUS: open

## 관측

장 마감 뒤 2026-09-22 이후 KR SELL 체결을 청산 종류별로 묶었다(운영 DB 읽기 전용, 수수료·세금 반영 실현손익).

| 청산 종류 | 건수 | 이익 | 평균 수익률 | 평균 보유 | 합계 손익(원) |
|---|---|---|---|---|---|
| PARTIAL_SELL | 9 | 9 | +2.66% | 2.1일 | +258,404 |
| 수동·미연결 | 9 | 6 | +1.18% | 2.4일 | +214,235 |
| REALTIME_TREND_EXIT | 6 | 4 | +0.69% | 1.0일 | +95,821 |
| TIME_STOP | 1 | 0 | −1.45% | 6.8일 | −40,571 |
| TREND_BROKEN | 1 | 0 | −2.46% | 6.8일 | −124,867 |

- 흐름 청산 6건 가운데 2건은 진입가 바로 위에서 나가 수수료·세금 뒤 소폭 손실이 됐다(2026-09-30 owner 4 010955 −632원, owner 7 373220 −2,071원). KRX PAPER 비용은 `paper_trading_service.FEE_RATES["equity_kr"]` 기준 매수 0.015%, 매도 0.015%, 매도세 0.18%로 왕복 0.21%다.
- 이 기간 `진입가 − 3 ATR` 초기 손절은 한 번도 닿지 않았다. 손실 2건은 모두 3 ATR 손절 전에 TIME_STOP·TREND_BROKEN으로 끝났다.

## 판단

- 사용자가 제안한 두 가지를 모두 적용하라고 요청했다.
- ① 흐름 청산은 현재가 ≥ 진입가 × (1 + 0.003)일 때만 낸다. 0.3%는 왕복 비용 0.21%에 약 1틱 여유를 더한 값이다. 그 아래에서는 사유 `below_min_profit`으로 막고 기존 사다리(ATR 손절·보호선·TIME_STOP)가 계속 관리한다. 방향 조건 네 개와 데이터 품질 기준은 바꾸지 않았다.
- ② 초기 손절은 `진입가 − 2 ATR`로 좁힌다. 새 position cycle의 state를 만들 때만 적용된다. 이미 저장된 사이클의 `initial_stop`·`current_stop`은 그대로 두고 소급하지 않는다.
- ②는 2026-09-22 결정 「초기 손절 −3 ATR 유지」와 2026-09-28 일봉 백테스트의 −2 ATR 미채택([28-scalp-exit-research](../28-scalp-exit-research/main.md))을 뒤집는다. 일봉 기준으로는 −2 ATR이 나빴으므로, 장중 단타에서의 효과는 운영 관찰로 판단한다.
- `position_manager.py`·`realtime_tape.py`는 strategy artifact fingerprint에 들어간다. owner 4·7은 `promotion_bypass_enabled`라 주문이 막히지 않는다.

## 남은 경계

- 표본이 작다. 8거래일, SELL 26건, 손실 2건이다. 두 변경 모두 수익성을 입증한 값이 아니다.
- 손절을 좁히면 손절 체결이 늘고, 3 ATR에서는 버텼을 되돌림 매매가 손실로 확정될 수 있다. 1차 익절(+0.5 ATR) 전에 −2 ATR이 먼저 닿는 비율을 관찰해야 한다.
- 최소 평가익 때문에 +0.3% 미만 구간에서 흐름이 꺾이면 청산이 늦어진다. 그 구간은 기존 사다리가 맡는다.

## 검증

- 단위 테스트: `test_realtime_tape.py`에 +0.2%(막힘, `below_min_profit`)와 +0.34%(청산) 경계를 추가했다. `test_position_manager.py`의 기본값 기대치를 2 ATR 기준(120−2×5=110, 100−2×0.5=99, 100−2×4=92)으로 고쳤다. 실행은 GitHub Actions `Test`에서 한다.
- **다음 행동**: 배포 뒤 첫 정규장에서 새 사이클의 `initial_stop`이 `entry − 2×initial_atr`인지와 +0.3% 미만 흐름 청산이 0건인지 읽기 전용으로 확인하고 STATUS를 닫는다.
