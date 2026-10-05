# KRX 장기 추세·재무성장 SHADOW 관측·성과 조회

주문·추천·승격·알림과 연결되지 않은 국내 보통주 장기 후보 2종의 전향 관측 경로다.
결과는 가상 성과이며 실제 체결·계좌 수익이 아니고 수익성 입증도 아니다. 스윙 SHADOW
(`docs/runbooks/kasset-swing-shadow.md`)와 같은 원칙·같은 대상·품질 기준·같은 시간 계약을
쓰지만 테이블·설정 지문·플래그는 따로다.

## 후보(v1, `kasset.longterm-shadow.v1`)

규칙 값은 모두 `LongtermShadowConfig`에 있고 `LongtermShadowConfig.fingerprint`로 run·신호에
함께 저장된다. 아래 값이 바뀌면 지문이 달라져 리포트가 별도 cohort로 나눈다.

| 단계 | 규칙 |
|---|---|
| 대상 | `kr_symbol_universe` 현재 보통주(일봉 대량 백필·스윙과 같은 SQL), S 종가 ≥ 1,000원, 20세션 평균 거래대금 ≥ 10억 원 |
| 품질 제외 | lookback 260세션 안에서 봉 누락(`data_gap`)·calendar 밖 봉·세션 간 35% 초과 변동(`price_discontinuity`)·정지 의심(`halted_suspect`) 등 스윙과 같은 사유. 완료 세션 253개 미만은 `insufficient_history`. 사유별 개수는 run `exclusions`에 남는다 |
| 공통 추세 필터 | ① 종가 > SMA200 ② SMA200(S) > SMA200(S−20세션) ③ SMA50 > SMA200 ④ 12-1 모멘텀 > 0. 위에서부터 처음 어긋나는 것이 탈락 사유(`below_sma200`, `sma200_not_rising`, `sma50_not_above_sma200`, `momentum_not_positive`) |
| 12-1 모멘텀 | `close(S−21세션) / close(S−252세션) − 1`. 같은 세션 평가 종목 안의 순위는 모멘텀 내림차순, 동률은 symbol 오름차순 |
| `trend_momentum` | 공통 추세 필터 통과 종목 중 모멘텀 상위 20개 |
| `quality_growth_trend` | 공통 추세 필터 통과 종목에 재무 조건을 더하고 같은 순위로 상위 20개 |

통과 종목이 20개 미만이면 있는 만큼 저장한다. `passedFilter`(상위 N 자르기 전 통과 수)·
`signals`(저장 대상)·`noSignalReasons`(탈락 사유별 개수, 상위 N 밖은 `outside_top_n`)가
run `candidate_counts`에 남는다.

### 재무 조건(`quality_growth_trend`)

`financial_fundamentals_snapshots`에서 `market='kr'`, `period_type='quarterly'`,
`data_state='fresh'`, `filing_date IS NOT NULL AND filing_date <= S`인 행만 읽는다(관측
시점 공시분만, 미래 공시 제외). 같은 `period_end_date`가 여러 행이면 공시일이 가장 늦은
행을 쓴다. 재무는 추세·모멘텀을 통과한 종목에 대해서만 읽고 판정하므로 재무 탈락
사유 개수는 그 종목들 안의 분포다.

1. 보이는 분기 중 `period_end_date` 기준 최근 8개를 잡는다. 8개 미만이면
   `fundamentals_insufficient_quarters`(보이는 행이 0이면 `fundamentals_missing`).
2. 8개가 3개월 간격으로 빈틈없이 이어져야 한다. 아니면 `fundamentals_quarter_gap`.
3. 최신 분기 `period_end_date`가 S보다 200일 넘게 이전이면 `fundamentals_stale`
   (`fundamentals_max_staleness_days`). 수집이 밀려 오래된 값이 "현재 성장"으로 읽히는 것을
   막는다.
4. 8개 모두 `discrete_revenue`·`discrete_net_income`이 있어야 한다. 아니면
   `fundamentals_discrete_missing`.
5. TTM = 최근 4분기 합, 전년 TTM = 그 앞 4분기 합. TTM 순이익 > 0
   (`fundamentals_ttm_net_income_not_positive`), 전년 TTM 순이익 > 0
   (`fundamentals_prior_ttm_net_income_not_positive`), 전년 TTM 매출 > 0
   (`fundamentals_prior_ttm_revenue_not_positive`).
6. TTM 순이익 성장률 ≥ 10%(`fundamentals_net_income_growth_below_minimum`)와 TTM 매출
   성장률 ≥ 10%(`fundamentals_revenue_growth_below_minimum`). 경계값 10%는 통과다.

신호 `evidence.fundamentals`에는 사용한 분기별 fiscal_period·period_end·filing_date·source·
단일 분기 매출/순이익, TTM·전년 TTM 합계와 성장률을 관측 당시 값 그대로 저장한다. 밸류에이션
(PER/PBR/ROE)은 쓰지 않는다.

## 시간 계약

- 신호 세션 S = 실제 현재 시각의 `last_final_session_kr`(15:35 KST 컷오프).
- 판정 시각은 S 정규장 종료 시각, 기록 시각 `observed_at`은 실제 시계로 따로 저장한다.
- S 다음 세션 정규장 시작 이후 실행은 `rejected`/`late_after_next_session_open`으로만
  남는다. 과거 날짜 재생 옵션은 없다.
- 코호트 주기: S가 calendar상 그 ISO 주의 마지막 거래일일 때만 두 후보를 판정한다. 그 외
  세션은 run만 `status=not_applicable`/`reason=week_incomplete`로 남는다(저장 신호 없음,
  종목 평가·재무 조회 없음). 보유 20거래일을 매주 새로 시작하는 격자형 포트폴리오로 표본을
  늘리려는 설계다.
- 같은 후보·설정·종목·코호트 세션은 한 번만 저장된다(`ON CONFLICT DO NOTHING`). 재실행은
  run 행만 추가하고 `duplicates`로 센다.
- 코호트 세션 completed run은 벤치마크 분모인 `evaluated_symbols`(대상·품질 필터를 통과해
  평가된 전 종목)를 담는다. 같은 세션 재실행이 여러 개면 가장 먼저 완료된 run의 목록을 쓴다.

## 실행

자동 경로는 기존 `candles.daily.kr.sync`(평일 16:30 KST)가 `status=ok`로 끝난 뒤
`KASSET_LONGTERM_SHADOW_ENABLED=true`일 때만, 스윙 SHADOW 다음에 한 번 이어 돈다(새 예약
없음, 기본 false). 결과는 `result["longterm_shadow"]`이고, observer 실패는 일봉·스윙 결과를
바꾸지 않고 이 항목과 `failed` run에 남는다. 운영 활성화는 마이그레이션 적용·배포와 함께
별도 사용자 승인 대상이다.

수동 실행(운영 컨테이너 기준, 마이그레이션 `20261005_kasset_longterm_shadow` 적용 뒤):

```bash
python -m scripts.kasset_longterm_shadow observe   # exit 0=completed/not_applicable, 2=rejected/failed
python -m scripts.kasset_longterm_shadow report --since 2026-10-02 --cohorts
python -m scripts.kasset_longterm_shadow report --since 2026-10-02 --signals
```

## 성과 리포트

- 진입: 코호트 세션 S 다음 유효 거래일 시가 가상 체결. 청산: 진입일을 1일째로 센 h번째 세션
  종가(h = 20/60/120 거래일, 달력일 아님).
- 비용·MFE/MAE·상태 분류(`mature`, `pending`, `entry_missing`, `bar_missing`, `invalid_bar`,
  `entry_untradable`, `exit_untradable`, `price_discontinuity`, `calendar_unavailable`)와
  `signalBarRevised`는 스윙과 같은 계산이다(`swing_shadow.evaluate_outcome`). 비용은 매수
  0.015%, 매도 0.015% + 거래세 0.18%, 편도 슬리피지 0.1%.
- **코호트**(후보 × S × h): mature 구성종목의 동일가중 순수익 평균과 상태별 구성원 수.
  구성원 중 `pending`(또는 `calendar_unavailable`)이 있으면 그 코호트·h는 `pending`
  으로 미성숙 표시하고 통계에서 뺀다. `state`: `mature`, `pending`, `calendar_unavailable`,
  `no_mature_members`, `empty`.
- **벤치마크**: 같은 S의 `evaluated_symbols` 전체에 같은 진입·청산·비용을 적용한 동일가중
  mature 평균. **초과수익** = 코호트 평균 − 벤치마크 평균(둘 다 `mature`일 때만).
- 후보별 horizon 통계(`candidates.<후보>.horizons.<h>`): 코호트 수·상태 분포,
  `matureCohorts`, 코호트 평균·중앙 순수익, 벤치마크 평균, 초과수익 평균·중앙, 초과수익
  양수 비율. `sampleAdvisory=insufficient_sample`(성숙 코호트 < 12)은 연구 안내일 뿐 gate가
  아니다.
- `--cohorts`는 후보×세션×h 코호트·벤치마크 상세, `--signals`는 신호별 순위·모멘텀·근거와 h별
  성과를 함께 낸다. 신호봉 정정이 있으면 `horizonsExcludingRevised`도 낸다.
- `coverage`는 스윙과 같은 상태 정의이며 현재 설정 cohort(`isCurrentConfig`)에 run·신호가
  0이어도 항상 나온다. 기대 세션은 주 마지막 거래일뿐이고 주 중간 세션의 `not_applicable`
  run은 `sessions[].state=not_applicable`로 보이되 기대 세션·실패 사유에 세지 않는다.

## 한계

- 수정주가·액면분할 계수가 없다. 260세션 안의 35% 초과 변동은 종목 제외로만 걸러지고 그보다
  작은 기업행동은 12-1 모멘텀과 성과를 왜곡할 수 있다.
- 재무 테이블은 정정 이력을 보존하지 않는다. 신호에는 관측 당시 값을 저장하지만 과거
  시점을 재현해 검증한 것이 아니다. `filing_date <= S` 기준이라 공시 시각(장중/장후)을
  구분하지 않는다. S 장 마감 뒤 공시도 다음 세션 시가 진입 전에 알 수 있어 진입 기준
  lookahead는 아니지만, 장 마감 후 공시가 S 판정에 포함될 수 있다.
- 유니버스는 관측 시점 현재 상장 보통주라 생존 편향이 있다. 이후 상장폐지·거래정지 종목은
  성과 봉이 없으면 entry_missing/bar_missing으로 통계에서 빠진다.
- 매주 시작하는 코호트는 20거래일 보유 기간이 서로 겹쳐 독립 표본이 아니다. 성숙 코호트
  수가 늘어도 초과수익의 신뢰 구간처럼 읽으면 안 된다. 벤치마크도 코호트 종목을 포함한다.
- 재무 최신 분기 수집이 순환 갱신이라 최신 분기가 늦은 종목은 `fundamentals_stale`·
  `fundamentals_insufficient_quarters`로 대량 탈락할 수 있다. 개수는 run에 남으니 해석 때
  함께 본다.
- 시가 상·하한가 잠김처럼 거래가 있었어도 체결되지 않았을 수 있는 경우는 구분하지 못한다.
