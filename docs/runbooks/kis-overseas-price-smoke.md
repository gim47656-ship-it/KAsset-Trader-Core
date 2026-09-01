# KIS overseas price smoke — 운영 종료

KIS cutover로 이 live quote smoke와 KIS-first fallback 설정 절차는 폐기되었다. 관련 스크립트는 운영 경로에서 제거되었고 보관 묘비는 종료 코드 2로 fail-close한다.

US equity quote와 intraday candle은 active symbol 검증 후 Toss만 사용한다. 정상 빈 응답은 빈 결과로 유지하고 provider failure는 오류로 처리한다.
