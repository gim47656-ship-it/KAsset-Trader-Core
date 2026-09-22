# 추천 종목명 누락과 매수 추천 과다 (CandidateFlow)

RECORD:
DATE: 2026-09-22
SCOPE: ai-recommendations, candidate-universe, symbol-master-name, same-symbol-reentry, recommendation-validity, buy-recommendation-volume
PATHS: app/extensions/kasset/automation/vertical_slice.py, app/extensions/kasset/automation/policy.py, tests/extensions/kasset/automation/test_vertical_slice.py, tests/extensions/kasset/automation/test_ai_trading_policy.py
STATUS: accepted

운영 read-only 관측 원문은 [`evidence/candidateflow-prod-readonly.md`](evidence/candidateflow-prod-readonly.md)에 있다.
검증 로그는 [`evidence/candidateflow-pytest.txt`](evidence/candidateflow-pytest.txt),
[`evidence/candidateflow-kasset-suite.txt`](evidence/candidateflow-kasset-suite.txt),
[`evidence/candidateflow-static-checks.txt`](evidence/candidateflow-static-checks.txt),
수정 전 실패 증거는 [`evidence/candidateflow-baseline-pytest.txt`](evidence/candidateflow-baseline-pytest.txt)에 있다.

---

## 1. 종목명이 비어 앱에 종목코드가 그대로 노출되던 문제

### 원인

브리프 가설(“live KR 경로가 마스터를 조회하지 않는다”)이 맞았고, 운영 데이터로 한 단계 더 좁혔다.

`kasset_automation_cycle_events.candidate_sources`를 보면 09-22 모든 cycle이
`{"watchlist": 4, "paper_holding": 4, "tvscreener_kr": 100}`다. **`invest_screener_snapshots`는 0건**이다.
즉 종목 마스터로 이름을 채우던 snapshot 경로는 이날 한 번도 실행되지 않았고, 이름을 채우지 않는
`_load_live_kr_candidates`가 후보 100건 전부를 만들었다. `005930`만 이름이 있었던 이유는 watchlist 4건에
들어 있어서다.

이름을 채우지 않는 수집 경로는 하나가 아니다.

| 경로 | 이름 | 비고 |
| --- | --- | --- |
| `watchlist` | 있음 | `item.name` |
| `invest_screener_snapshots` | 있음 | 인라인 `SymbolMaster` 조회 |
| `tvscreener_kr` | **스크리너가 준 것만** | 우선주 대부분 없음 |
| `tvscreener_us` | **항상 `None`** | 하드코딩 |
| `paper_holding` | **항상 `None`** | 하드코딩 |

### 고친 방식

live KR 경로에만 같은 조회를 하나 더 붙이는 대신, **수집이 끝나고 상한을 적용한 뒤 한 곳에서** 채운다.
`_load_candidates` 끝에 `_fill_candidate_names()`를 두고 snapshot 경로의 인라인 조회는 제거했다.

- 이름이 빈 후보만 모아 시장별로 `SymbolMaster`를 **한 번씩** 조회한다. 상한(`cap_candidate_universe`)
  적용 뒤라 대상은 최대 `candidate_limit`개다. 기존 인라인 조회는 상한 적용 **전** 전체를 조회했으므로
  쿼리 수도 대상 수도 늘지 않는다.
- 마스터 이름이 종목코드와 같으면 `None`으로 둔다 — 기존 계약을 그대로 옮겼다.
- 위 표의 `tvscreener_kr`·`tvscreener_us`·`paper_holding` 세 경로가 한 번에 덮인다. 스크리너 경로만
  고쳤다면 같은 결함이 US와 보유 포지션 경로에 그대로 남는다.

`_merge_trading_candidate`의 `current.name or incoming.name` 우선순위는 그대로다.

---

## 2. 매수 추천 과다 — 관측

09-22 추천 24건 / 체결 9건 / 6종목.

- 배치는 09:00, 10:10, 11:20, 12:30, 13:40, 14:50 — **70분** 간격. producer(`kasset_market_events.run`)는
  10분 주기인데 `_OWNER_BUY_COOLDOWN`이 owner 단위 1시간이라 `60 + 10 = 70분`이 나온다.
- 추천 `valid_until`은 전부 `created_at + 60분`. **모든 추천이 다음 배치 10분 전에 이미 만료**된다.
- 집행 결과: SUCCEEDED 9, FAILED 5(전부 `risk_preview_rejected:BUDGET`), 미집행 10.
  미집행 10건은 `decision`이 PENDING인 6건 + APPROVED인데 끝내 claim되지 않은 4건(전부 `02826K`)이다.
- owner 4는 `risk_level: 5` → `same_symbol_reentry_limit == 2`. 실제로 `000155`·`000157`·`005930`이
  하루 2회씩 체결됐다. 주문 단계는 이 한도를 이미 지키고 있었다.
- **추천 생성 단계에는 종목 단위 제한이 하나도 없었다.** 13:40 `000157` 추천은 자기 `hard_risk` 증거에
  `ORDER_COUNT ... sameSymbolBuys=2/2, passed: false`를 달고 저장됐다. 주문 단계가 절대 통과시키지
  않을 것을 알면서 추천 행을 만든 것이다.
- **집행은 owner당 한 tick에 한 건뿐이다**(`job.py` `authorize_next_for_auto_execution`).
  배치마다 추천 5건이 나오는데 60분 안에 그 owner가 집행할 수 있는 건 많아야 몇 건이고,
  나머지는 쓰이지도 못한 채 만료된다.

---

## 3. 고친 것 — 두 개가 맞물려 있다

### 3-1. 추천 유효기간을 배치 간격보다 길게

`vertical_slice.py`의 유효기간 상한 `timedelta(hours=1)`을 파생 상수로 바꿨다.

```python
_PRODUCER_TICK = timedelta(minutes=10)
_RECOMMENDATION_VALIDITY = _OWNER_BUY_COOLDOWN + 2 * _PRODUCER_TICK   # 80분
```

**왜 80분인가.** 다음 배치는 빨라야 `쿨다운(60) + tick 1회(10) = 70분` 뒤다. 상한이 그 간격을 덮지
못하면 직전 배치 추천이 다음 배치가 시작되기 전에 만료된다. 70분은 경계값이라(만료 시각 = 다음 배치
시각) 부족하고, tick 한 번의 여유만 더한 **80분이 창을 덮는 최소값**이다. 숫자를 고르지 않고
쿨다운과 tick에서 파생했으므로 둘 중 하나가 바뀌면 함께 움직인다.

이 상한이 실제로 구속력이 있다는 것은 확인했다. 유효기간은 `min(전략 결과, ranking, 상한, trigger)`인데
전략 결과는 `data_as_of + 4일`(`strategies.py`), ranking은 `as_of + 24시간`(`candidate_ranker.py`)이라
둘 다 훨씬 길고, 09-22 추천 24건이 전부 정확히 `+60분`이었다는 관측이 상한이 구속했다는 증거다.

### 3-2. 재진입 한도를 추천 생성 단계에도 적용

owner의 기존 `same_symbol_reentry_limit`를 쓴다. 세는 것은 **체결(SUCCEEDED)·집행 중(CLAIMED)
추천 + 아직 만료되지 않은 미집행 추천**뿐이다. `risk_preview_rejected:BUDGET` 같은 실패와 만료된
추천은 재진입 기회를 실제로 쓴 적이 없으므로 세지 않는다.

넣은 위치는 후보 평가 루프 안, **AI 검토 앞단**이다. 한도를 다 쓴 종목이 AI 슬롯과 사이징 계산을
소모하지 않는다. 제외 사실은 기존 `preAiExclusions` / `candidateExclusions` 채널에
`same_symbol_reentry_exhausted`로 남는다. 새 응답 키나 스키마는 만들지 않았다.

거래일 경계는 주문 단계 hard risk와 **같은 함수**를 쓴다. 그러려고 `policy._trading_day_start`를
`policy.trading_day_start`로 공개했다(동작 변경 없음).

### 왜 둘이 함께여야 하는가

유효기간을 그대로 두고 억제만 넣으면 09-22 기준 24 → 23건이다. 미만료 PENDING이 구조적으로 항상 0이라
사실상 체결 수만 세기 때문이다.

반대로 유효기간을 건드리지 않고 “생성 후 70분 이내 PENDING”을 세는 방법도 있었다. 집행 staleness를
전혀 안 늘리는 대신 **체결을 잃는다.** 11:20 `005930` 중복을 막으면 그 자리에서 나온 11:25 체결이
사라진다. 10:10 추천은 그 시점에 이미 만료(11:10)라 대신 집행될 수 없기 때문이다.

유효기간을 80분으로 늘리면 10:10 추천이 11:30까지 살아 있고, `authorize_next_for_auto_execution`은
`decision=PENDING` 행도 `valid_until > now`면 집행 후보로 잡아 **자동 승인**한다(`job.py:359-374`,
`job.py:409-414`). 그래서 중복을 막아도 체결은 **먼저 나온 추천으로 옮겨 붙는다.**
유효기간 연장은 중복 억제를 작동시키는 조건이자 체결을 지키는 조건이다.

---

## 4. 09-22 타임라인 되짚기

규칙: 배치 시점 T에서 종목 S의 카운트 = (그때까지의 SUCCEEDED/CLAIMED) + (status NULL이고
`created + 80분 > T`인 추천). 한도 2. 집행 결과는 실제 관측을 그대로 쓰되, 중복이 막힌 자리의
집행은 아직 살아 있는 이전 추천으로 옮겨 붙는다.

| 배치 | 실제 추천 | 새 규칙 | 막힌 것 |
| --- | --- | --- | --- |
| 09:00 | 5 | 5 | — |
| 10:10 | 5 | 5 | — |
| 11:20 | 5 | **3** | `000157`(S1+live1=2), `005930`(S1+live1=2) |
| 12:30 | 4 | 4 | — |
| 13:40 | 4 | **3** | `000157`(S1+S1=2) |
| 14:50 | 1 | 1 | — |
| **합계** | **24** | **21** | 3건 |

**체결은 9건 그대로다.**

- 11:25 `005930` 체결 → 막힌 11:20 추천 대신 10:10 추천(11:30까지 유효)으로 붙는다.
- 12:35 `000157`, 13:45 `005935`, 14:55 `005940` 체결은 그 추천이 막히지 않아 그대로다.
- 13:40 `000157`은 실제로도 끝내 집행되지 않은 행이고, hard risk가 이미 `sameSymbolBuys=2/2`로
  실패 판정한 바로 그 행이다. 순수 소음이다.

이 표는 09-22 타임라인에 새 규칙을 되짚은 **계산**이지 실행 관측이 아니다. 유효기간이 길어지면
집행기가 만료 전에 처리하는 건수 자체가 늘 수 있어서, 실제로는 체결이 9건보다 늘어날 여지도 있다
(줄어들 경로는 위 replay에 없다).

---

## 5. 새로 생긴 위험 — 감추지 않고 적는다

**집행 지연 노출이 60분에서 80분으로 33% 늘어난다.**

- 주문은 `orderType="MARKET"`, 수량 고정이다(`consumer.py`). 집행 시점의 시장가로 체결된다.
- 진입 기준가·손절가는 추천 **생성 시점**에 계산돼 고정된다. 집행이 80분 뒤면 그만큼 다른 가격에
  들어갈 수 있고, `entry - 3*ATR` 손절 거리와 risk-per-trade 사이징이 의도와 어긋날 수 있다.
- no-chase 2% 관문은 **trigger 평가 시점**에만 걸린다. 집행 시점의 가격 이탈을 막지 않는다.
  집행 시점에 가격 신선도를 다시 보는 관문은 없다(`consumer.py`·`job.py` 확인).
- 즉 새로운 종류의 위험이 아니라 **기존 노출 창이 20분 길어진 것**이다. 단타 성격상 무시할 만하지는
  않다. 사용자 판단이 필요하면 `_PRODUCER_TICK` 여유분을 빼 70분 경계값으로 좁히는 선택지가 있으나,
  그러면 만료 시각과 배치 시각이 같아져 경계에서 불안정해진다.

**breakout-baseline 경로는 이 억제가 배치 간 적용되지 않는다.**
`_candidate_valid_until`은 `trigger_decision.valid_until`과도 `min`을 취하는데 그 값은
`decision_ttl = 30분`이다(`intraday_triggers.py`). 그래서 breakout 경로 추천은 30분짜리로 남고
다음 배치 전에 만료된다. 09-22에는 breakout-baseline이 0건이라 관측에 영향이 없었다.
`decision_ttl`은 장중 trigger 신선도 계약이라 진입 임계값 보존 지시에 따라 건드리지 않았다.

---

## 6. 보존한 계약

- 초기 손절 `entry - 3*ATR`, 부분익절 `+3 ATR`: 건드리지 않았다(다른 조각 소유).
- 진입 임계값(상대거래량 1.5배, no-chase 2%, 돌파 버퍼 0.2%): 불변. `decision_ttl`도 불변.
- `_OWNER_BUY_COOLDOWN` 1시간 owner 쿨다운: 유지. 제거하면 10분 주기가 풀려 추천량이 오히려 늘어난다.
- `first-pullback` / `nr7-inside-day` detector 두 경로: 제거도 판정 변경도 없다.
- breakout 경로 판정, `_persist_recommendation`의 hard risk 평가, 추천 `evidence`/`rationale` 구조,
  앱 스키마: 불변.
- 이름이 symbol과 같으면 `None`: 유지.
- `market_pipeline.py`의 `kasset_market_events` 추천(1시간)은 다른 producer라 건드리지 않았다.
- DB 스키마·migration·스케줄러 등록: 없음.

---

## 7. 검증

서버(`root@100.73.186.78`)의 격리 checkout `/tmp/kv-candidateflow`(base `a2d0b1517` tar + 내 소유 파일만
덮어씀)에서 일회성 container로 실행했다. DB는 운영과 분리된 `kasset-test-db`의 실행별 DB다.
운영 checkout `/opt/kasset-trader-core`, `.env.kasset`, 운영 DB/볼륨은 사용하지 않았다. 운영 DB는 SELECT만 했다.
형제 maker가 동시에 편집 중이므로 작업 트리 전체가 아니라 **base + 내 delta**만 올려서 검증했다.

```
cwd=/w (container)

python -m pytest tests/extensions/kasset/automation/test_vertical_slice.py \
                 tests/extensions/kasset/automation/test_ai_trading_policy.py \
                 tests/extensions/kasset/automation/test_candidate_ranker.py \
                 -q -ra --tb=short -p no:cacheprovider
→ 98 passed                                   (exit 0)

python -m pytest tests/extensions/kasset -q -ra --tb=short -p no:cacheprovider
→ 1320 passed in 838.03s                      (exit 0)

uv run ruff check <changed 4 files>            → All checks passed!        (exit 0)
uv run ruff format --check <changed 4 files>   → 4 files already formatted (exit 0)
uv run ty check app/ --error-on-warning        → All checks passed!        (exit 0)
```

유효기간 변경은 producer·consumer·job·strategy promotion까지 영향이 닿으므로 `tests/extensions/kasset`
전체를 돌렸다. `test_candidate_ranker.py`는 내 소유가 아니지만 `_load_candidates`를 직접 호출하므로
회귀 확인용으로 포함했다.

### 추가·변경한 회귀

| 테스트 | 무엇을 막나 |
| --- | --- |
| `test_unnamed_screener_candidates_take_symbol_master_names` | 스크리너가 이름을 안 줘도 마스터 이름이 실린다. 우선주(`000155` 두산우, `02826K` 삼성물산우B) 포함. 마스터 이름이 종목코드와 같으면 `None` 유지 |
| `test_failed_buy_recommendation_keeps_the_symbol_retryable` | **이번 결정의 핵심 경계.** 체결·집행 중 추천과 만료 전 대기 추천만 세고 BUDGET 실패·만료 대기·SELL은 세지 않는다. 그리고 **직전 배치(쿨다운+tick 전)에 만든 미집행 추천이 아직 살아 있어 카운트된다** — 유효기간이 배치 간격을 덮는지를 SQL 경로로 검사한다 |
| `test_exhausted_reentry_symbol_produces_no_new_buy_recommendation` | 한도를 채운 종목은 추천 행이 생기지 않고, 같은 cycle의 다른 종목은 정상 추천된다 |
| `test_krx_detector_entry_paths_signal_on_the_last_completed_session_bar` | 기존 테스트. detector 경로 유효기간 상한을 60분 → 80분으로 갱신 |

수정 전 base 소스에서 앞의 세 테스트가 실패하는 것을 확인했다(`evidence/candidateflow-baseline-pytest.txt`).

---

## 8. 남은 경계 / 확인하지 못한 것

- **위 §5의 집행 지연 20분 증가는 사용자 판단 항목이다.** 실장 운용에서 문제가 되면 되돌릴 곳은
  `_RECOMMENDATION_VALIDITY` 한 줄이다.
- breakout-baseline 경로는 `decision_ttl` 30분 때문에 배치 간 중복 억제가 안 걸린다(§5).
- `02826K`가 APPROVED 상태로 4번 모두 claim되지 않은 이유는 조사하지 않았다. 집행기 소유다.
  유효기간이 길어지면 이 행들도 집행 후보로 더 오래 남으므로 체결이 늘어날 수 있다.
- `first-pullback` / `nr7-inside-day` detector의 후보 생성량과 `breakout-baseline` 0건의 원인은
  미확인이다. 진입 임계값 변경은 `same_time_rvol_shadow` 채점이 필요하고 범위 밖이다.
- §4의 24 → 21은 **계산**이다. 운영 반영 후 실제 추천량·체결량은 실환경에서 확인해야 한다.
