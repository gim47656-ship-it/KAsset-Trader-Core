# KIS mock 자율매매 루프 — 운영 종료

KIS cutover로 mock reconcile, execution consumer, 자동집행, TaskIQ 등록과 관련 flag 활성화 절차는 폐기되었다. 이 문서의 과거 단계는 더 이상 실행 계약이 아니다.

관련 KIS 스크립트는 운영 경로에서 제거되었고 보관 묘비는 종료 코드 2로 fail-close한다. 과거 mock ledger·journal·attribution 행은 감사·조회 목적으로만 보존하며 Toss 또는 NH PLUG 주문으로 재해석하지 않는다. NH PLUG는 KR mock read-only다.
