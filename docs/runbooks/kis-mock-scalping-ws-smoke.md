# KIS mock quote WebSocket smoke — 운영 종료

KIS cutover로 live/mock quote WebSocket 비교와 smoke 진입점은 폐기되었다. 관련 스크립트는 운영 경로에서 제거되었으며 보관 묘비는 종료 코드 2로 fail-close한다.

KIS credential, approval key 또는 WebSocket host를 사용하지 않는다. 현재 production WebSocket monitor는 Upbit 전용이고 equity market data는 Toss를 사용한다.
