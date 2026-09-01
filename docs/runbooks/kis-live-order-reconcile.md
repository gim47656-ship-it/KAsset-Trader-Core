# KIS live order reconcile — 운영 종료

KIS cutover로 KIS live place/cancel/modify/reconcile 및 자동 reconcile CLI·TaskIQ 표면은 폐기되었다. 운영 DB의 KIS live ledger에서 `accepted`/`pending`/`partial` 행이 0건임을 확인한 뒤 실행 진입점을 차단했다.

과거 `review.kis_live_order_ledger` 행과 원래 provider provenance는 감사·조회 목적으로 그대로 보존한다. 행을 Toss로 재해석하거나 자동 종결하지 않는다. 새 Toss 주문은 accepted-only ledger와 broker-evidence fill booking을 사용하며, 체결 증거는 최대 2분 간격의 `toss_live.poll_fills_periodic` 또는 주문 직후 대상 reconcile로 수집한다.
