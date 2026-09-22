# 2026-09-22 — 매도 미발생·매수 과다·앱 표면 오류 조사와 수정

```text
RECORD:
DATE: 2026-09-22
SCOPE: 매도 미발생, 익절 사다리, 매수 추천 과다, 추천 유효기간, 종목명 누락, 추천 조회 500, HANDOFF 문서 분리
PATHS: app/extensions/kasset/automation/position_manager.py, app/extensions/kasset/automation/vertical_slice.py, app/extensions/kasset/automation/policy.py, app/extensions/kasset/automation/position_sizing.py, app/schemas/ai_recommendations.py, HANDOFF.md, doc/README.md
STATUS: accepted
```

## 사용자 요구 (원문)

- "오늘 실제 서버 주문상황을 분석하고 매수가 많이된건좋은데.. 매도가 제대로 이뤄지지않은것같아.. 목표익절치에 못해서그런건지.. 지금 매수조건도 존나많이나감."
- "일단 종목코드가 그대로노출된것도있었고 예전에 닫았던항목같은데. 실제 왜 주문했는지 주문근거가 또 볼라하면오류남."
- "손절쪽말고 어느정도 익절쪽도 열려있는부분이있으면좀 단타치기편할텐데."
- "3번까지 다 해두셈." / "매도 규칙은 니가알아서 전권을 줄테니까."
- "이거 다하면 라이브 배포까지해서 서버에반영시켜놔라."

## 조사 결과 — 전부 코어 서버, 앱 수정 불필요

운영 서버 `root@100.73.186.78`, 운영 SHA `a2d0b1517`, 읽기 전용 조회로 확인했다.

### 1. 매도 미발생 — 버그 아님, 익절선이 구조적으로 안 닿음

2026-09-22 보유 6종목의 장중 실제 등락(`research.kr_candles_5m_toss`, 09:00~15:30 KST):

| 종목 | 진입가 | 익절선(+3ATR) | 손절선(-3ATR) | 장중 고가 | 장중 저가 |
|---|---|---|---|---|---|
| 000155 | 472,500 | +15.8% | -15.8% | +3.81% | -0.11% |
| 005940 | 26,050 | +7.9% | -7.9% | +3.07% | -0.38% |
| 000157 | 396,000 | +14.4% | -14.4% | +1.89% | -1.39% |
| 138040 | 128,400 | +14.7% | -14.7% | +1.79% | -0.86% |
| 005935 | 213,000 | +11.3% | -11.3% | +1.41% | -1.41% |
| 005930 | 280,250 | +10.0% | -10.0% | +1.16% | -2.23% |

어느 종목도 익절선·손절선 근처에 못 갔고 그래서 SELL 추천이 0건이었다. 워커 sweep은 장중 정상 실행됐다. 진입은 장중 돌파 단타인데 청산은 스윙 기준이라 생긴 구조적 불일치다.

**Main 초기 오진 정정**: 조사 초기에 `sweep done: owners=0`을 보고 "청산 평가가 아예 안 돌았다"고 판단했으나 틀렸다. 워커 로그는 UTC이고 그 sweep들은 장 마감 후(KST 23시대) 것이었다. `last_evaluated_at`이 NULL인 것도 일봉 커서라 당일 매수분에는 정상이다. 장중 구간에는 owner>0 sweep이 다수 있었다.

### 2. 매수 과다 — 쿨다운 구조와 유효기간 불일치

- 추천 24건이 전부 BUY, 진입 경로가 전부 `first-pullback`/`nr7-inside-day`, `breakout-baseline`은 0건.
- `_OWNER_BUY_COOLDOWN`이 종목 단위가 아니라 **owner 단위 1시간**. 10분 producer와 겹쳐 정확히 70분 간격 배치 6회.
- **추천 유효기간 60분 < 배치 간격 70분**이라 모든 추천이 다음 배치 전에 만료된다. 그래서 같은 종목이 매 배치 새로 추천됐다(000157·005935 각 5회, 02826K·005940 각 4회).
- 체결은 9건이고 `same_symbol_reentry_limit`=2가 주문 단계에서 이미 작동 중이었다(000155·005930·000157 각 2회).

### 3. 주문 근거 조회 500 — 응답 스키마 결함

```
GET /api/v1/ai/recommendations?status=PENDING&limit=50  →  500
portfolio.positionSizing.caps.0.quantity
  input_value='2.5E+2'  →  pattern '^-?[0-9]+(?:\.[0-9]+)?$' 불일치
```

`PositionSizeCap.as_evidence()`가 `str(Decimal)`을 써서 지수 표기가 evidence JSONB에 저장됐다. 저가주 005940(26,100원) 추천에서 수량 250이 나온 11:20 KST부터 500이 시작됐다. 같은 필드의 `"33.33333333333333333333333333"`은 통과한다.

### 4. 종목코드 노출 — 실시간 후보 경로가 마스터를 안 봄

`symbol_master` KRX 3,622행 중 이름 누락 0건인데 추천 24건 중 23건이 `name` NULL이었다. snapshot 경로는 `SymbolMaster`를 조회하지만 `_load_live_kr_candidates`는 스크리너가 준 `row.name`만 썼다. **앱 렌더링은 정상** — 같은 화면에서 이름이 있는 005930만 이름으로, NULL인 나머지는 코드로 떴다.

### 5. 만료 항목 노출 — 서버에서 재현되지 않음 (발주 가정 오류)

`app/services/ai_recommendations/repository.py:68-74`에 `or_(valid_until IS NULL, valid_until > now)` 필터가 이미 있고 운영 컨테이너 소스도 동일하다. 운영 DB 조회 결과 `pending_all=12, pending_visible=0`. 앱에 옛 항목이 보인 것은 목록이 통째로 500이었던 것 또는 앱 캐시 쪽이며, 서버에서는 판별 불가로 남긴다.

## 확정한 계약 변경

사용자가 매도 규칙 전권을 위임했다. **초기 손절 `entry - 3*ATR`은 유지**하고 익절만 계단으로 나누는 것을 Main이 확정했다. 단일 익절선을 통째로 내리면 손절은 그대로인데 익절만 짧아져 손익비가 무너지며, 이는 2026-09-07 -3% 고정 손절 바닥 실패와 같은 구조다.

채택 기준으로 **손익비 0.5:1 하한**을 걸었다.

### 새 청산 사다리 (5단)

| 단 | 내용 | 변경 |
|---|---|---|
| 1 | 초기 손절 `진입가 - 3 ATR` | 불변 |
| 2 | 진전폭 +1 ATR 뒤 `최고종가 - 2 ATR` 조기 보호선 | 신설 |
| 3 | `+0.5 ATR` 1차 익절 30% | +3 ATR 50%에서 변경 |
| 4 | 부분익절 뒤 `max(진입가, 최고종가 - 3 ATR)` 바닥 | 신설 |
| 5 | TIME_STOP 진전폭을 현재 종가 기준으로 | 계산 수정 |

## Maker 배정과 판정

| Maker | 조각 | 모델 | 판정 |
|---|---|---|---|
| [ApiSurface](maker-ApiSurface.md) | 추천 조회 500, 만료 회귀 | `anthropic/claude-opus-5:high` | ACCEPTED |
| [CandidateFlow](maker-CandidateFlow.md) | 종목명 fallback, 재추천 억제, 유효기간 | `anthropic/claude-opus-5:high` | ACCEPTED (F-CF-1 rework 1회) |
| [ExitStrategy](maker-ExitStrategy.md) | 청산 사다리 | `anthropic/claude-opus-5:max` | ACCEPTED |
| HandoffSplit | HANDOFF 이력 7건 분리 | `b-ai/deepseek-v4.1-flash:high` | ACCEPTED |

Jev 추천은 앞 세 조각 모두 HARD/CODE_SYSTEM이었으나 그 후보인 `openai-codex`가 quota `limitReached`로 차단돼 가용한 HARD 후보 `anthropic/claude-opus-5`로 발주했다.

### Main의 발주 오류와 정정 2건

1. **ExitStrategy 범위**: 처음에 조기 trailing·TIME_STOP·본전 stop 셋으로 좁혀 발주했다. ExitStrategy가 편집 전 조향에서 "그 셋은 전부 일봉 종가 전이라 오늘 6종목 종가가 ±0.7 ATR 안이면 어떤 임계값도 발동하지 않는다"고 지적했고 그것이 맞았다. `partial_profit_atr`을 범위에 넣어 재지시했다.
2. **CandidateFlow 유효기간**: `valid_until` 60분 대 배치 70분 불일치를 "이번 범위 밖"으로 뺐는데, 그 결과 1차 구현이 추천 24→23으로 거의 효과가 없었다. CandidateFlow가 처음 체크포인트에서 이미 짚었던 사안이라 F-CF-1 rework로 되돌렸다.

두 건 모두 Maker의 편집 전 조향이 Main의 잘못된 범위 설정을 잡아낸 사례다.

## 검증 증거

| 조각 | 명령 | 결과 |
|---|---|---|
| ApiSurface | `pytest tests/schemas/test_ai_recommendations_schema.py tests/routers/test_ai_recommendations.py` | 36 passed, exit 0 |
| ApiSurface | 수정 전 소스 + 신규 테스트 (역증명) | 3 failed, exit 1 — 운영 500과 같은 locator·메시지·입력 |
| CandidateFlow | `pytest tests/extensions/kasset` | 1320 passed / 652.66s, exit 0 |
| CandidateFlow | 수정 전 소스 + 신규 테스트 (역증명) | 3 failed, exit 1 |
| ExitStrategy | `pytest test_position_manager.py test_portfolio_backtest.py` | 141 passed / 1 deselected, exit 0 |
| ExitStrategy | 인접 (`test_strategy_promotion`, `test_shadow_manifest`) | 113 passed, exit 0 |
| 전 조각 | `ruff check`, `ruff format --check`, `ty check --error-on-warning` | 모두 exit 0 |

검증은 전부 서버의 운영과 분리된 임시 checkout과 일회성 container, 운영과 분리된 `kasset-test-db`의 실행별 DB에서 했다. 운영 checkout·`.env.kasset`·운영 DB/볼륨은 사용하지 않았고 운영 DB는 SELECT만 했다.

### 백테스트 (ExitStrategy)

KR 코호트 100종목 × 417거래일(2025-01-07~2026-09-22), 13개 arm 비교.

| | 기존 | 채택안 |
|---|---|---|
| 총수익 | +3.85% | **+12.49%** |
| 승률 | 42.6% | 50.0% |
| 손익비 | 1.696 | **1.764** |
| MDD | 6.76% | 7.28% |
| 사이클 | 47 | 98 |

핵심 발견: **1차 익절만 앞당기면 오히려 나빠진다**(+1.64%, 손익비 1.446). 상승만 잘리고 하방은 -3 ATR 그대로이기 때문이며, 본전 바닥과 반드시 함께 가야 한다. 수량 50%는 손익비 1.251로 하락해 30%가 우월하고, 임계값 0.75/1.0 ATR은 총수익·손익비 모두 하락한다.

### 09-22 재현

- **청산**: 수정 전 6종목 전부 `NONE`(운영 SELL 0건과 일치), 수정 후 000155(+0.721 ATR)·005940(+1.167 ATR)에서 `PARTIAL_SELL` 30%.
- **추천**: 24 → 21(3건 억제), 체결 9 → 9(보존). 계산이며 실행 관측이 아니다.

## 남은 위험·미확인

- **집행 지연 노출 60분 → 80분**. 주문이 MARKET·수량 고정이라 80분 뒤 체결되면 진입 기준가와 벌어질 수 있다. 집행 시점 가격 신선도 관문은 없다. 되돌릴 곳은 `vertical_slice.py`의 `_RECOMMENDATION_VALIDITY` 한 줄이다.
- **breakout-baseline 경로는 중복 억제 미적용**. `decision_ttl` 30분이 유효기간을 더 짧게 자른다. 09-22에는 0건이라 영향 없었으나 breakout이 다시 나오면 반복이 남는다. `decision_ttl`은 장중 trigger 신선도 계약이라 건드리지 않았다.
- **조기 보호선(2단)은 백테스트에서 한 번도 발동하지 않았다**. 1차 익절이 먼저 성립하기 때문이며, 부분익절 미체결 상태로 가격이 달아나는 실거래 경로 전용 안전망이다. 단위 테스트로만 방어된다.
- **추천 21건도 크게 준 것은 아니다**. 더 줄이려면 체결을 잃는다(추천 수 기준 카운트는 24→11이지만 체결 9→4). 사용자가 체결 증가를 긍정했으므로 체결 보존을 택했다.
- **02826K가 APPROVED 상태로 4번 모두 claim되지 않은 원인 미조사**. 집행기 소유이며 유효기간 연장으로 체결이 늘어날 수 있다.
- **detector 두 경로의 후보 생성량과 breakout-baseline 0건 원인 미확인**. `same_time_rvol_shadow` 채점이 필요해 범위 밖이다.
- **앱에 옛 항목이 보인 현상이 500 해소로 사라지는지 미확인**. 앱 캐시 가능성은 서버에서 판별 불가이며 앱 저장소가 이 PC에 없다.

## 배포

migration 0건이라 main merge가 자동 배포를 실행한다. 배포 후 다음 정규장의 자연 추천·체결·청산을 읽기 전용으로 확인한다. 검증 목적으로 주문을 제조하지 않는다.
