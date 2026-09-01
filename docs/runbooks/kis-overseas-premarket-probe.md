# KIS overseas premarket probe — 운영 종료

KIS cutover로 이 live provider probe와 credential export 절차는 폐기되었다. 관련 스크립트는 운영 경로에서 제거되었고 보관 묘비는 종료 코드 2로 fail-close한다.

US equity quote와 intraday candle은 active symbol 검증 후 Toss만 사용한다. provider failure는 오류로 처리하며 이미 승인된 KIS intent를 Toss로 자동 전환하지 않는다.
