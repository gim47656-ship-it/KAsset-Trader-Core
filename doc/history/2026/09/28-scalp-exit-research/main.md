# 단타 청산 보유기간 단축 — 2026-09-28

RECORD:
DATE: 2026-09-28
SCOPE: KR PAPER 청산 사다리, `PositionManagerConfig.max_holding_bars`
PATHS: app/extensions/kasset/automation/position_manager.py, doc/history/2026/09/28-scalp-exit-research/
STATUS: 채택(10 → 2), 사용자 승인

## 결정

사용자가 "전략이 단타이니 손절·익절·전량매도를 단타에 맞추라"고 요청하고, 후보(손절 −2 ATR, 보유 2~3일,
2차 익절, 장중 고점 추격선)를 고른 뒤 백테스트 결과로 확정하기로 했다. 결과는 사용자가 고른 조합이 아니라
**보유기간만 3거래일 조건부로 줄이는 안**이 가장 좋았고, 사용자가 이를 채택해 배포를 승인했다.

- `max_holding_bars` 10 → **2**. `bars_held`는 진입일 다음 완료 일봉부터 세므로 진입일 포함 3거래일째
  종가에 진전폭이 `+0.5 ATR` 미만이면 `TIME_STOP`. 진전이 있으면 청산하지 않는다(조건부).
- 초기 손절 `−3 ATR`, 1차 익절 `+0.5 ATR`·30%, 본전 바닥, 잔량 `최고종가 − 3 ATR`은 그대로다.

## 근거 (일봉 417일, 코호트 `67f1059a…` 100종목, 같은 입력 내 비교)

| 규칙 | 총수익 | MDD | 손익비 | 승률 | 기대값 |
|---|---:|---:|---:|---:|---:|
| 기존 (10일 조건부) | +12.57% | 7.28% | 1.77 | 49% | +2.76% |
| **채택 (3일 조건부)** | **+20.27%** | 7.56% | **2.56** | 42% | **+2.94%** |
| 2일 조건부 | +15.63% | 9.85% | 2.67 | 37% | +2.01% |
| 3일 조건부 + 부분익절 뒤 장중 고점 −1 ATR 추격 | +8.78% | 8.86% | 1.54 | 44% | +0.85% |
| 3일 조건부 + −1.5 ATR 추격 | +7.21% | 8.84% | 1.51 | 44% | +0.66% |
| 10일 + −1 ATR 추격 | +15.73% | 4.84% | 1.43 | 58% | +3.05% |
| 손절 −2 ATR만 | +13.71% | 6.20% | 1.59 | 49% | +2.43% |
| 2차 익절 +1 ATR(잔량 30%)만 | +10.09% | 6.80% | 1.37 | 53% | +2.17% |
| −2 ATR · 3일 조건부 · 2차 +1 · 추격 −1 | +13.25% | 7.91% | 1.42 | 47% | +0.89% |
| 2~3일째 무조건 전량 | +6.3~14.0% | 9.1~12.5% | 1.27~1.44 | — | +0.25~1.14% |

- 무조건 N일 청산은 크게 오르는 종목까지 잘라 모두 나빴다. 2차 익절은 넣을 때마다 수익이 줄었다.
- 추격선은 10일 구조에서는 MDD를 낮췄지만 3일 조건부와 합치면 청산 223건으로 회전만 늘고 성과가 떨어졌다.
- 3일 조건부 수치는 26-arm 실행과 4-arm 재실행에서 소수점까지 같았다.
- 원문: [v3-results.jsonl](evidence/v3-results.jsonl)(26 arm), [r2-results.jsonl](evidence/r2-results.jsonl)(4 arm).
  실행기 [research_backtest.py](evidence/research_backtest.py), 방법·입력 감사는 [maker-ScalpExitBacktest.md](maker-ScalpExitBacktest.md)
  (그 문서의 빈 표는 중단 전 초안이며 수치는 이 문서가 정본).

## 한계

- 장중 5분봉 재생은 유효 사이클 5개(09/02 이전 진입 93건·결손 3건 제외)라 판단 근거로 쓰지 않았다.
- HEAD 기준선이 09/22 기록(+12.49%/1.764)과 +12.57%/1.773으로 달라 이번 표는 이번 실행 내부 비교만 허용한다.
- 단일 구간 실행이며 walk-forward는 하지 않았다. 매도세(`sell_tax_rate`)는 반영되지 않는다.
- 조건부 `TIME_STOP`은 일봉 종가 판정이라 실제 체결은 다음 평가 tick이다.

## 검증

- 격리 서버 pytest(`docs/runbooks/server-pytest-runner.md` 경로, 운영 이미지 `9af66f554`, `kasset-test-db`)로
  position_manager·portfolio_backtest·promotion·job·vertical_slice 등 관련 13개 파일 **393 passed, exit 0**. 테스트 변경 없음.
- 연구 실행은 `--network none` 격리 컨테이너, 운영 DB는 `default_transaction_read_only=on` COPY만 사용했다.

## 운영 영향

- `position_manager.py`는 `STRATEGY_CODE_PATHS`에 있어 전략 fingerprint가 바뀐다. `promotion_bypass_enabled=true`인
  owner 4·7은 영향이 없고, bypass가 꺼진 owner 1·5는 재승격 전까지 PAPER 자동 주문이 막힌다.
- 기존 보유분도 다음 일봉 평가부터 새 상한이 적용된다. 이미 3거래일을 넘기고 진전폭이 `+0.5 ATR` 미만인
  보유는 다음 평가에서 `TIME_STOP`이 나온다.
