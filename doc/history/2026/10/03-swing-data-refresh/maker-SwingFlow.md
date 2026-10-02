# maker-SwingFlow: 전종목 수급 일일 갱신 경로 보완

RECORD: maker-SwingFlow
DATE: 2026-10-03
SCOPE: investor_flow, 수급 일일 갱신, scheduled_kr_investor_flow, 토요일 수집 기회, status ok/partial/failed
PATHS: app/jobs/investor_flow_snapshots.py, app/tasks/investor_flow_snapshot_tasks.py, tests/test_investor_flow_snapshot_tasks.py
STATUS: partial (검증 증거 evidence/SwingFlow-validation.txt, Main 검수 전)

## 변경
- cron `30 8 * * 1-6`, 게이트: 오늘 또는 전일이 XKRX 세션이면 실행(금요일분 토 08:30, 평일 휴장 다음날 결행분 보충, 일요일 미등록).
- job result `symbols_with_rows`(실제 행이 나온 고유 종목 수), task 결과 `symbolsWithRows`/`status`(ok/partial/failed). failed는 `logger.error`, partial은 `logger.warning`. raise 없음.
- 기본 flag false/dry-run/매매 gate 불변. 새 schema·cron·gate 없음.

## 분석
- 기존 전종목 경로 재사용: all_symbols, days20, batch100. upsert는 행 단위, `_classify_idempotency`는 키당 4 bind라 100×20×4=8,000 < 32,767. 250일은 batch 25 필요(26,000).
- raise 보류 근거: `taskiq_broker`의 SmartRetryMiddleware는 default_retry_label=False이고 이 task에 retry label이 없어 재시도는 없음. 그래도 승인 범위대로 status=failed+logger.error 보수안.
- 토요일 Naver 원천 완성은 미검증. 다음 실행도 days=20 upsert로 최근 20일을 다시 확인한다(원천 장애·휴장 시 회복은 보장하지 않음).
- 작업 중 편집이 root main에 한 번 적용됐으나 patch로 root 복구(git status clean 확인), worktree에 동일 적용.

## 검사(미실행, Main slot 대기)
`tests/test_investor_flow_snapshot_tasks.py tests/test_investor_flow_snapshot_job.py` (컨테이너 pytest, Main 지정 명령). 추가 테스트: status 3종, 게이트 5케이스(토/화/일/금휴장/토휴장 후).

## 운영 exact 명령(승인 후)
- 활성화 env: `INVESTOR_FLOW_SCHEDULE_ENABLED=true`, `INVESTOR_FLOW_SNAPSHOTS_COMMIT_ENABLED=true`(서버 재시작 필요, schedule은 import 시 등록).
- 1회 수동 보충: `python -m scripts.build_investor_flow_snapshots --market kr --all --days 20 --batch-size 100 --commit`
- 장기 백필: `--days 260 --batch-size 25`

## 검증 결과 (kasset-prod 컨테이너, r1)
- pytest 2파일: `14 passed` EXIT 0
- ruff check 4파일: All checks passed EXIT 0 / ty app 2파일: All checks passed EXIT 0
- ruff format --check: tests/test_investor_flow_snapshot_tasks.py 1건 reformat 지적 EXIT 1 → r2(evidence/SwingFlow-r3.diff)에서 주석 위치 수정, 재확인 필요
- smoke(실제 job+격리 DB, fake fetcher, Naver 호출 없음): 230종목(3개 실패) all_symbols days=20 batch100 → batches 3 [100,100,30], fetch days=20만, built 4540, symbols_with_rows 227, dry wouldInsert 4540 → commit rows 4540 → 재dry wouldUpdate 4540 insert 0
