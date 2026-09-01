# KIS mock reconciliation — 운영 종료

KIS cutover로 KIS mock reconcile 도구, consumer, job, TaskIQ 등록과 수동 실행 절차는 폐기되었다. 관련 스크립트는 운영 경로에서 제거되었고 보관 묘비는 종료 코드 2로 fail-close한다.

과거 `review.kis_mock_order_ledger` 행은 수정하거나 Toss로 재해석하지 않고 감사·조회 목적으로 보존한다. NH PLUG는 KR mock read-only이며 reconcile 또는 주문 기능을 제공하지 않는다.
