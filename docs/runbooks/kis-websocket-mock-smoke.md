# KIS WebSocket mock smoke — 운영 종료

KIS cutover로 이 smoke 절차와 실행 진입점은 폐기되었다. `scripts/kis_websocket_mock_smoke.py`는 운영 경로에서 제거되었고 보관 묘비는 실행 시 종료 코드 2로 fail-close한다.

KIS credential, WebSocket approval key, subscription 또는 연결을 시도하지 않는다. 현재 production WebSocket monitor는 Upbit 전용이다. Toss equity 체결 증거는 최대 2분 간격의 `toss_live.poll_fills_periodic` 또는 주문 직후 대상 reconcile로 수집한다.
