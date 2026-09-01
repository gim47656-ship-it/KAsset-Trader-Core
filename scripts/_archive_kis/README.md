# KIS 스크립트 보관소

KIS 운영 cutover로 기존 수동 실행·프로브·mock·WebSocket·reconcile 진입점을 이 디렉터리로 옮겼다. 각 파일은 provider 모듈을 import하지 않는 실행 차단 묘비이며 실행 시 메시지를 stderr에 기록하고 종료 코드 2를 반환한다.

- 원래 `scripts.<name>` 모듈 경로는 더 이상 존재하지 않는다.
- 이 파일들을 운영 작업, cron, launchd, TaskIQ 또는 진단 절차에 연결하지 않는다.
- KIS 과거 ledger와 model은 감사·조회 목적으로 그대로 보존한다.
- 현재 equity 주문·계좌·체결 증거 경계는 Toss이며, NH PLUG는 KR mock read-only만 지원한다.
- Toss accepted 주문의 체결 증거는 최대 2분 간격의 `toss_live.poll_fills_periodic` 또는 주문 직후 대상 reconcile로 수집한다.
- `scripts._archive_kis.rob278_kr_dryrun`은 제거된 KIS 계좌 범위와 시세 수집기에 의존하던 보고서 dry-run을 Toss로 자동 재해석하지 않고 동일하게 차단한다.

KIS 기능을 다시 활성화하거나 Toss로 자동 fallback하는 용도로 이 묘비를 수정하지 않는다. 새로운 provider 배선은 별도 승인과 안전 검토가 필요하다.
