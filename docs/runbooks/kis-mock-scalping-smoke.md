# KIS mock scalping smoke — 운영 종료

KIS cutover로 mock scalping daemon, holdings-delta smoke, 주문·정리 절차는 폐기되었다. 관련 `scripts/kis_*` 진입점과 그 묘비 디렉터리는 저장소에서 삭제되었다.

과거 `kis_mock_order_ledger` 행은 감사·조회 목적으로만 보존한다. KIS mock 주문, reconcile, WebSocket 또는 credential 활성화를 시도하지 않는다.
