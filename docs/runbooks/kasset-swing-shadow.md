# KRX 스윙 SHADOW 관측·성과 조회

주문·추천·승격과 연결되지 않은 KRX 스윙 후보 3종의 전향 관측 경로다. 결과는
가상 성과이며 실제 체결·계좌 수익이 아니고 수익성 입증도 아니다.

## 후보(v1, `kasset.swing-shadow.v1`)

| 후보 | 신호 | anchor(중복 기준) |
|---|---|---|
| `weekly_compression_breakout` | 완료 주봉만. 직전 6주 base 폭 ≤15%, 최근 3주 평균 주봉폭 < 그 앞 3주, 이번 주 종가 > base 고점, 직전 주는 그 앞 6주 고점을 종가로 넘지 않음, 주 거래량 ≥ base 평균 ×1.5, 종가 > 10주 종가 평균 | 그 주 마지막 세션 |
| `uptrend_first_pullback` | `shadow_setups` First Pullback `confirmed` 그대로 + 첫 접촉(`first`) + SMA20 > SMA50, SMA50 > 10세션 전 SMA50, 종가 > SMA50. 접촉 묶음이 evaluator 접촉 lookback(40봉) 시작에서 gap(1봉) 이내로 시작하면 앞부분이 잘렸을 수 있어 `pullback_cluster_truncated`로 뺀다 | 접촉 묶음 첫 세션 |
| `box_breakout_retest` | 40세션 박스(폭 ≤20%)를 거래량(직전 20세션 평균 ×1.5) 동반 종가 돌파한 날 B가 신호일 2~10세션 전, 이후 저가가 박스 상단 +2% 안으로 재접촉, 이후 종가가 상단 −3% 아래로 안 내려감, 신호일 양봉·박스 위·전일 종가 위 | 돌파일 B |

공통 대상은 `kr_symbol_universe` 현재 보통주(일봉 대량 백필과 같은 SQL), 종가 1,000원
이상, 20세션 평균 거래대금 10억 원 이상이다. 최근 80세션에 봉 누락·calendar 밖 봉·
세션 간 35% 초과 변동·정지 의심(`halt_detection`)이 있으면 제외하고 사유별 개수를 run에
남긴다. 설정 전체는 `SwingShadowConfig.fingerprint`로 run·신호에 함께 저장된다.

## 시간 계약

- 신호 세션 S = 실제 현재 시각의 `last_final_session_kr`(15:35 KST 컷오프).
- 판정 시각은 S 정규장 종료 시각, 기록 시각 `observed_at`은 실제 시계로 따로 저장한다.
- S 다음 세션 정규장 시작 이후 실행은 `rejected`/`late_after_next_session_open`으로만
  남는다. 과거 날짜 재생 옵션은 없다.
- 주봉 후보는 S가 calendar상 그 ISO 주의 마지막 거래일일 때만 판정한다(아니면
  `not_applicable: week_incomplete`).
- 같은 후보·설정·종목·anchor는 한 번만 저장된다(`ON CONFLICT DO NOTHING`). 재실행은
  run 행만 추가하고 `duplicates`로 센다.

## 실행

자동 경로는 기존 `candles.daily.kr.sync`(평일 16:30 KST)가 `status=ok`로 끝난 뒤
`KASSET_SWING_SHADOW_ENABLED=true`일 때만 한 번 이어 돈다(새 예약 없음, 기본 false).
observer 실패는 일봉 결과를 바꾸지 않고 결과의 `swing_shadow` 항목과 `failed` run에 남는다.
운영 활성화는 마이그레이션 적용·배포와 함께 별도 사용자 승인 대상이다.

수동 실행(운영 컨테이너 기준, 마이그레이션 적용 뒤):

```bash
python -m scripts.kasset_swing_shadow observe            # exit 0=completed, 2=rejected/failed
python -m scripts.kasset_swing_shadow report --since 2026-10-05 --signals
```

## 성과 리포트

- 진입: S 다음 유효 거래일 시가 가상 체결. 청산: 진입일을 1일째로 센 h번째 세션 종가
  (h = 1/3/5/10 거래일, 달력일 아님).
- 비용: 매수 0.015%, 매도 0.015% + 거래세 0.18%, 편도 슬리피지 0.1%(왕복 약 0.41%).
- MFE/MAE: 진입일~h번째 세션 고가·저가 / 진입 시가 − 1.
- 상태: `mature`, `pending`(h번째 세션 미완료), `entry_missing`, `bar_missing`,
  `invalid_bar`(진입~청산 봉의 가격 0 이하·OHLC 역전 — `kr_candles_1d`는 NOT NULL만 보장),
  `entry_untradable`(진입 세션 거래량 0), `exit_untradable`(청산 세션 거래량 0),
  `price_discontinuity`, `calendar_unavailable`. 통계는 `mature`만 쓴다. 중간 보유일
  거래량 0은 보유 평가로 두고 `zeroVolumeHoldingSessions`·`matureWithZeroVolumeHolding`으로 센다.
- `sampleAdvisory=insufficient_sample`(mature < 30)은 연구 advisory일 뿐 gate가 아니다.
- `coverage`는 run·신호가 0이어도 현재 설정 cohort(`isCurrentConfig`)에 항상 나온다.
  `coverage.sessions[].state`: `not_observed`(run 없음), `observation_failed`(rejected/failed만),
  `not_evaluated`(completed지만 평가 0종목), `evaluated_no_signal`, `evaluated_with_signals`.
  같은 날 재실행은 합산하지 않고 마지막 completed run 하나의 `universeCount`·`evaluatedCount`·
  `exclusions`·후보별 `noSignalReasons`를 보여 준다. `missingSessions`는 앞의 두 상태다.
- `signalBarRevised`는 관측 시점에 저장한 신호봉 OHLCV·source와 현재 봉이 다른 신호 수다.
  저장 값은 바꾸지 않고, 있으면 `horizonsExcludingRevised`를 함께 낸다. 적재 시각만
  바뀐 경우는 세지 않는다.

## 한계

KRX 수정주가 계수가 없어 액면분할 등은 35% 초과 변동 제외로만 걸러진다. 시가 상·하한가
잠김 같은 체결 불가 상황을 구분하지 못한다. 유니버스는 관측 시점 정의다.
