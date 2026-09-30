# 2026-09-30 — 제출 시점 BUDGET 초과 BUY의 수량 축소

RECORD:
DATE: 2026-09-30
SCOPE: Hard Risk BUDGET, 종목 비중, max_buy_notional, 제출 수량 축소
PATHS: app/extensions/kasset/automation/job.py, app/extensions/kasset/automation/policy.py
STATUS: accepted

## 관측

- owner 4의 NH투자증권(005940) BUY가 10:20 KST에 `risk_preview_rejected:BUDGET`으로 실패했다. 추천 수량 229주 × 26,150원 = 5,988,350원으로, 5단계 종목 한도(2천만 원 × 30% = 600만 원)를 거의 채운 수량이었다. 제출 시점에는 시세가 조금 올라 한도를 넘었다.
- 2026-09 전체로 보면 BUY 체결은 31건, `risk_preview_rejected:BUDGET` 거절은 18건이다(운영 DB `review.ai_recommendations`). 18건에는 운영 예산이 실제로 바닥난 경우도 섞여 있을 수 있다. 사유별 분해는 하지 않았다.

## 판단

- 안전장치(한도 초과 거절)는 유지한다. 사용자가 선택지 ① "한도 안 최대 수량으로 줄여 제출"을 선택했다.
- 줄이는 위치는 KAsset Hard Risk를 적용하는 `OwnerScopedPaperOrders._assess`다. `consumer.py`는 추천 수량을 그대로 요청으로 만들고 스스로 수량을 정하지 않는 계약이라 건드리지 않았다.
- 조건은 BUY이면서 실패한 관문이 `BUDGET` 하나뿐인 경우다. 수량은 `max_buy_notional`(운영예산 잔액과 종목 비중 잔액 중 작은 값)을 그 순간 기준가로 나눠 lot 단위로 내림한다. 줄인 수량으로 preview와 Hard Risk를 다시 통과해야 제출한다. 수량은 늘리지 않는다. kill switch나 다른 관문이 함께 실패하면 원래 판정을 유지한다.

## 남은 경계

- 추천의 `suggestedQuantity`는 그대로 두므로 주문 수량이 추천보다 작을 수 있다. 앱에서 "추천 229주 / 주문 228주"로 보일 수 있다.

## 운영 관찰 (2026-09-30)

- PR #106(`bbac54a3`)은 11:33 KST에 배포됐다. 11:30에 생성된 owner 4의 005940 BUY 추천(`suggestedQuantity` 229)은 11:41에 **228주 26,300원 FILLED**로 체결됐다. 229주 × 26,300원은 6,022,700원이라 종목 한도 600만 원을 넘는다.
- worker 로그 `kasset BUY quantity fitted to budget`은 확인하지 못했다. 11:45 PR #107 배포로 worker 컨테이너가 교체되면서 이전 컨테이너 로그가 사라졌다. 수량 축소의 근거는 추천 수량과 체결 수량의 차이다.
- 배포 뒤 `risk_preview_rejected:BUDGET`은 0건이다(당일 1건은 배포 전인 10:20).
