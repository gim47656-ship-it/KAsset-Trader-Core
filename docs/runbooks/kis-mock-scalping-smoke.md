# KIS mock scalping smoke — 운영 종료

KIS cutover로 mock scalping daemon, holdings-delta smoke, 주문·정리 절차는 폐기되었다. 관련 `scripts/kis_*` 진입점은 운영 경로에서 제거되었고 `scripts/_archive_kis/`의 묘비는 provider를 import하지 않은 채 종료 코드 2로 fail-close한다.

과거 `kis_mock_order_ledger` 행은 감사·조회 목적으로만 보존한다. KIS mock 주문, reconcile, WebSocket 또는 credential 활성화를 시도하지 않는다. NH PLUG는 KR mock read-only이며 주문 기능을 제공하지 않는다.
