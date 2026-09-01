# Order Approval Hash & Intent Guard

## 운영 범위

현재 주문 표면은 Toss equity와 Upbit crypto만 지원한다. KIS live/mock 도구와 실행 경로는 폐기되었으며 과거 KIS intent의 `approval_hash`, `idempotency_key`, provider provenance는 감사 목적으로만 보존한다. 해당 intent를 Toss로 자동 전환하거나 키 namespace를 재사용하지 않는다.

## Toss approval hash

1. `toss_preview_order`는 정규화된 symbol, side, quantity, price, market, rung와 private client identity를 canonical payload로 묶는다.
2. 응답의 `approval_hash`와 `approval_expires_at`은 해당 payload와 TTL에 결속된다.
3. `toss_place_order`는 live send 직전 canonical payload를 다시 계산한다. 불일치, 만료, 누락은 fail-close한다.
4. `clientOrderId`는 Toss 전용 namespace에서 파생한다. 과거 `review.order_send_intents` KIS 키를 사용하지 않는다.
5. live sell은 hash 외에도 fresh Toss sellable preflight와 기존 loss/high-value/NXT/hard-risk/kill-switch guard를 모두 통과해야 한다.

`TOSS_APPROVAL_HASH_MODE`는 `off|optional|warn|required` 중 하나다. 운영 cutover는 `warn` 로그로 미전환 호출자를 확인한 뒤 `required`를 적용한다. 잘못된 enum 값은 설정 로드에서 거부한다.

## Upbit idempotency

Upbit 주문은 content-based idempotency key를 broker `identifier`로 전달한다. 승인 hash 정책과 기존 crypto hard-risk guard를 우회하지 않는다.

## Fill evidence

HTTP acceptance는 fill이 아니다. Toss 주문은 accepted-only ledger에 기록한 뒤 broker order evidence로만 fill/journal/PnL을 book한다. `toss_live.poll_fills_periodic`은 최대 2분 간격으로 실행해야 하며, 해당 cadence를 보장할 수 없으면 accepted send 직후 주문 ID를 대상으로 reconcile한다. Pending/unknown evidence는 terminal state로 추론하지 않는다.

## 제거된 표면

- `/api/screener/order` live submit은 fail-closed다.
- `kis_live_*`, `kis_mock_*`, KIS reconcile/mirror 도구는 등록되지 않는다.
- `account_mode="real"|"live"`는 ambiguous로 거부한다.
- `account_mode="kis_live"|"kis_mock"`는 historical read provenance 외 operational dispatch에서 거부한다.
