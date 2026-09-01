# KIS mock attribution chain — 역사 계약

KIS cutover로 KIS mock signal·submit·reconcile 실행 표면은 폐기되었다. 과거 `kis_mock_signal_ledger`와 `kis_mock_order_ledger`의 correlation/provenance는 감사·조회 목적으로만 보존한다.

새 KIS mock intent를 만들거나 과거 intent를 Toss로 자동 전환하지 않는다. 관련 스크립트 묘비는 provider를 import하지 않은 채 종료 코드 2로 fail-close한다.
