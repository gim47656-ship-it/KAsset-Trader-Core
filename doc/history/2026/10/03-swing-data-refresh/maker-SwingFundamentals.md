# maker-SwingFundamentals: DART 재무 올해 분기·결측 보충 지속 수집

RECORD: maker-SwingFundamentals
DATE: 2026-10-03
SCOPE: financial_fundamentals, DART 재무, 분기 수집, refresh-due, skip-existing, ETF 제외, 요청 예산, 정정 재수집
PATHS: app/services/financial_fundamentals_snapshots/builder.py, app/jobs/financial_fundamentals_snapshots.py, scripts/build_financial_fundamentals_snapshots.py, tests/test_financial_fundamentals_job.py, tests/test_financial_fundamentals_builder_orchestration.py, tests/test_build_financial_fundamentals_cli.py
STATUS: partial (r3.1 focused 검사·smoke 완료; Main 수렴 검수·서버 cron 교체·배포 뒤 운영 DB 확인 대기)

## 원인
- `default_dart_fetcher`가 `today.year - 1`부터 5년만 요청해 분기에서도 올해가 빠졌다 → 2026Q1·Q2 0행.
- `--skip-existing`가 행이 1개라도 있는 종목을 영구 제외 → 기존 2,271종목은 새 분기·partial·정정 재수집 기회가 없었다.
- 후보 필터가 `classify_kr_instrument(symbol, name, None)` 이름 규칙만 사용 → `kr_symbol_universe.security_type`(ETF)을 무시.
- CLI 추정은 `--skip-existing`와 무관하게 앞 N종목 × 41(실제 상한과도 다름)로 계산했고, 예산 소진 시에도 exit 0이었다.

## 변경 (Main 승인 범위: 2026-10-03 approved-with-scope 3점 조정 반영)
- `DartFetchPlan`·`dart_fetch_plan`: **이미 종료된** 분기·회계연도만 요청한다. 법정기한은 조건이 아니므로 조기 공시도 받는다. 2026-10-03 기준 2026 Q1~Q3 + 2025~2021 연간·Q1~Q3이다. 진행 중인 분기와 미종료 연간은 요청하지 않는다. 미공시 기간은 빈 응답이라 행을 만들지 않는다. Q1 단독, Q2~Q4 직전누적 차감과 직전 누락 시 skip은 그대로다. 공시목록 조회 시작은 계획의 최소 연도 1/1이다.
- `max_requests` 최악 상한은 연간 CFS+OFS+배당 3, 분기 CFS+OFS 2, 목록 1이다. 분기 포함 전체는 52, 최근(2개 회계연도)은 16, 연간만 5년은 16이다.
- `--refresh-due`(새 모드, `--skip-existing`·`--symbol`과 상호배타, 기본 off): 기존 행의 `max(source_collected_at)`, 최신 종료기간 보유 여부, 5년 창(2026-10 기준 2021-01-01 이후) 안의 `data_state='partial'` 최소 기간말을 1회 집계 조회한다. 창 밖 partial은 어떤 계획도 다시 받지 못하므로 due가 아니다(r3, FUND-PARTIAL-WINDOW). 그보다 오래된 partial이 창 안 partial을 가리지 않는다.
  1. missing_latest: 최신 종료기간이 없다. 그 기간 종료 뒤 한 번도 안 봤거나, 본 지 7일이 지났다(미공시 주간 재확인).
  2. partial: 창 안 partial 행이 있고 7일이 지났다.
  3. uncollected: 행이 없다. 5년 전체를 받는다.
  4. stale: 30일이 지났다(정정 sweep). 같은 tier 안에서는 오래된 순이다.
- 공정성(r3, FUND-FAIRNESS): 정렬한 pool에서 fairness slice를 먼저 배정한다. slice 크기는 최악 계획(52)으로 항상 들어가는 수 `min(limit, budget // 52)`이고, 매일 `date.toordinal() × slice` 위치로 slice 크기만큼 전진한다. 남은 예산은 위 tier 우선순위로 채운다. slice 안의 due 종목은 반드시 들어가므로, 모든 due 종목이 실패 지속 여부와 상관없이 `ceil(len(pool)/slice)`일 안에 선택된다. 2,400종목·예산 18000이면 slice 346, 7일 이내다. hash와 새 상태는 쓰지 않았다. r2의 미수집 전용 회전은 제거했다(Main 재현: limit 1에서 실패하는 missing_latest가 매일 독점).
- 기수집 종목은 최근 계획(최신 종료 연간 연도 + 올해 종료 분기)만 받는다. 창 안이면서 그보다 이전에 partial 행이 있으면 전체 계획을 받는다.
- `_BudgetAllocator`: `--limit`과 `opendart_daily_request_budget`(18000 불변) 안에서 최악 상한을 앞에서부터 채운다. 초과분은 `deferred_symbols`로 넘긴다. 같은 예산 bound를 `--skip-existing`/`--all`/기본 `--limit` 경로에도 적용한다. 명시 `--symbol`은 추정만 하고 자르지 않는다.
- 실패 종목은 행을 쓰지 않으므로 `source_collected_at`이 갱신되지 않는다(가짜 성공 없음). 다음 실행에서 다시 due가 된다.
- 예산 소진: 기존대로 commit을 취소한다. 결과 `budget_exhausted=True`, CLI는 `DART BUDGET EXHAUSTED`를 출력하고 exit 3으로 끝낸다(성공 아님).
- 전부 실패(r3, FUND-EMPTY-FAILURE): 선택된 종목이 있는데 payload가 0이고 소진도 아니면 `no_rows_collected=True`, `committed=False`가 된다. CLI는 `NO ROWS COLLECTED … Not a successful run`을 출력하고 exit 4로 끝낸다. no-due(선택 0)와 estimate는 exit 0, 일부 성공은 기존 `--allow-partial` 의미대로 exit 0이다.
- 후보 필터: `security_type`이 NULL이 아니면서 `STOCK`이 아니면 제외하고, `is_common_share is False`도 제외한다. 마스터가 `STOCK`+`is_common_share=True`로 확인한 행은 코드 끝자리 5·7·9 우선주 규칙보다 마스터를 따른다(r2, r1 smoke에서 발견: 확인된 보통주 100005가 빠짐). REIT/SPAC 이름 제외는 유지한다. 마스터 미분류(NULL) 행은 기존 이름 규칙을 쓴다(보통주 누락 최소화). 진실원은 `toss_symbol_master_service`가 채우는 `kr_symbol_universe.security_type/is_common_share`다. `sync_symbol_master`도 이 두 열에서 `symbol_master` ETF/COMMON_STOCK을 만든다.
- 기본 dry-run, `--commit`의 `--allow-partial` 요구(PartialCommitBlocked), `--estimate-only`의 fetch·DB write 0은 유지한다. refresh 모드의 estimate는 evidence DB read만 한다.

## 기아(starvation) 설명
- 성공한 fetch는 그 종목의 최근 행을 `now`로 다시 쓰므로 tier 1·2·4에서 최소 7일 빠진다. tier에 계속 남는 것은 fetch가 계속 실패하는 종목뿐이다.
- 실패가 하루 예산·limit을 다 차지해도 fairness slice가 정렬 pool 위를 매일 slice 크기만큼 전진한다. slice에 들어온 due 종목은 반드시 배정되므로 하위 tier도 `ceil(len(pool)/slice)`일 안에 선택된다. 테스트: limit 1에서 실패 1·미수집 1·stale 1 → 3일 안에 모두 선택. 실패 20(예산 초과)·미수집 1·stale 1, slice 2 → 11일 안에 22종목 모두 선택.
- 분기 종료 직후에는 전 종목(약 2,400)이 tier 1이 된다. 최근 계획 16요청 기준 약 2~3일에 소화한다.

## 운영 (Main 소유: 서버 cron/스크립트 변경)
- 서버 `kasset-dart-daily.sh` ① 교체안: `python -m scripts.build_financial_fundamentals_snapshots --with-quarterly --refresh-due --all --commit --allow-partial` (budget이 자동 bound, 공시 단계 잔여 2,000 유지).
- 선확인(무fetch, DB read만): `python -m scripts.build_financial_fundamentals_snapshots --with-quarterly --refresh-due --all --estimate-only`
- exit 0 정상(일부 실패 포함 `--allow-partial`), 2 commit guard 또는 argparse 오류, 3 예산 소진, 4 선택했지만 수집 0행.
- 갱신이 필요한 Main 소유 문서: `docs/runbooks/financial_fundamentals.md` §2·§3(41/11 추정, skip-existing 설명, exit 3), README 「수동·예약 데이터 작업」 DART 행.

## 검사 (서버 kasset-prod, image `kasset-trader-core:04d62828e…`, checkout `/tmp/kasset-swing-20261003-01a0fead` readonly, `--cpus=1.0 --network container:kasset-test-db`)
- r1 pytest: `-m pytest -q --tb=short -p no:cacheprovider tests/test_financial_fundamentals_builder_orchestration.py tests/test_financial_fundamentals_job.py tests/test_build_financial_fundamentals_cli.py tests/test_financial_fundamentals_builder_parse.py tests/test_financial_fundamentals_jsonb_safety.py tests/test_snapshot_commit_guard_wiring.py` → 52 passed, EXIT=0 (`evidence/SwingFundamentals-r1-pytest.txt`).
- r1 `ruff check` 6파일 EXIT=0, `ty check` production 3파일 EXIT=0, `ruff format --check` EXIT=1(test_financial_fundamentals_job.py 1곳 → r2에서 수정).
- r1 격리 DB CLI smoke(빈 API 키 실패 경로, 외부 호출 0): CLI exits `[0, 0, 2, 0, 0]`, 실패 fetch 뒤 `source_collected_at` 불변(`unchanged_after_failed_fetch: True`), 다음 estimate가 같은 종목을 다시 선택. ETF(이름 토큰 없음) 제외 확인. 이 smoke에서 코드 끝자리 5·7 보통주 누락을 발견 → r2. (`evidence/SwingFundamentals-r1-smoke-failpath.txt`)
- r2 재검사(2파일 overlay sha256 일치 확인 뒤): pytest `tests/test_financial_fundamentals_job.py tests/test_snapshot_commit_guard_wiring.py` 27 passed EXIT=0, ruff check 0, ruff format --check 0, ty 0 (`evidence/SwingFundamentals-r2-checks.txt`).
- r2 성공 경로 smoke(가짜 OpenDART client만 교체; 실제 CLI `run(parse_args())` → job → `default_dart_fetcher`(BudgetedClient) → repository upsert, 격리 DB `swingfund_smoke_7c9564d8` exact-name create/drop, 외부 호출 0) EXIT=0 (`evidence/SwingFundamentals-r2-smoke-success.txt`). 임시 스크립트는 실행 뒤 삭제했다(sha256 `e4d3585b…`).
  - estimate: 2026Q3 기준 missing_latest 1·stale 1·uncollected 3(코드 끝 5 보통주 포함, 이름 토큰 없는 ETF 제외), projected 188, fake 호출 0.
  - commit: 68행(insert 67/update 1). 100001에 2026Q1(차감 100, filing 2026-05-14)·2026Q2(150, 2026-08-13)·2025Q4(600−450=150)가 rcept_no provenance와 함께 들어갔다. 미공시 2026Q3와 2026A는 행이 없다. 실패 종목 100004는 행이 없다.
  - 같은 날 재실행: refresh 선택은 uncollected 100004 하나(나머지 5 not due). 명시 rerun(100005)은 wouldInsert 0/wouldUpdate 27이고 행 수는 불변이다.
  - 결함(FUND-EMPTY-FAILURE): 100004만 선택돼 전부 실패했는데 `committed 0 rows.`·EXIT=0으로 찍혔다(위 파일 55-70행) → r3.
  - 예산 5 강제: exit 3, `DART BUDGET EXHAUSTED`, 0행, 100002 불변.
  - 8일 뒤 선택: 미공시 2026Q3 주간 재확인 3종목(16요청)과 실패 종목 uncollected(52)를 고른다. 최신·신선 종목은 제외된다.
- Main 검수 REWORK(FUND-FAIRNESS·FUND-EMPTY-FAILURE·FUND-PARTIAL-WINDOW) → r3. 공정성 재현 원문은 `evidence/Main-fairness-before.txt`(Main 소유)다.
- r3 검사: pytest `tests/test_financial_fundamentals_job.py tests/test_build_financial_fundamentals_cli.py tests/test_snapshot_commit_guard_wiring.py` 40 passed EXIT=0, ruff format --check 0, ty(job·CLI) 0, ruff check EXIT=1(C408 CLI 테스트 `dict()` → r3.1에서 literal로 수정) (`evidence/SwingFundamentals-r3-checks.txt`).
- r3 smoke EXIT=0 (`evidence/SwingFundamentals-r3-smoke.txt`): exits estimate 0 / refresh-commit 0 / same-day 0 / all-fail 4(`NO ROWS COLLECTED`) / explicit-rerun 0(행 수 불변) / budget-exhaustion 3 / recovery 0 / nothing-due 0. 창 밖 partial(2019)만 있는 100003은 due가 아니다(`partial in window: None`). 이 smoke에서 nothing-due가 `committed 0 rows.`로 찍히는 것을 확인 → r3.1에서 no-symbols 결과 `committed=False`, CLI `nothing due: no fetch, no rows written.`으로 수정했다.
- r3.1 재검사(3파일 overlay sha256 일치 뒤): pytest `tests/test_build_financial_fundamentals_cli.py tests/test_financial_fundamentals_job.py tests/test_snapshot_commit_guard_wiring.py` 40 passed EXIT=0, ruff check 0, ruff format --check 0, ty(job·CLI) 0. smoke EXIT=0, exits estimate 0 / refresh-commit 0 / same-day 0 / all-fail 4 / explicit-rerun 0 / budget-exhaustion 3 / recovery 0 / nothing-due 0(`nothing due: no fetch, no rows written.`) (`evidence/SwingFundamentals-r3.1-checks-smoke.txt`). 임시 smoke 스크립트는 로컬·서버 모두 삭제했다.
- 최종 frozen diff: `evidence/SwingFundamentals-r3.1.diff`(6파일). `evidence/SwingFundamentals-r3.diff`는 r3.1 내용으로 덮어쓴 중간 증거다. r1·r2 diff 사본은 Main의 앞선 허용("새 최종 diff와 실패/수정 증거만 남겨도 됨")에 따라 삭제했다. 그 뒤 r2 보존 요청이 왔지만 로컬 사본은 이미 없다(당시 sha256 `77406deb…`, Main 보유 여부는 미확인). r2 smoke(`SwingFundamentals-r2-smoke-success.txt`)는 남아 있다. builder·orchestration 테스트는 r1 이후 무변경이라 r1 pytest 결과를 재사용한다.

## 미검증·경계
- 실제 DART 응답(조기 공시 반영 시점, 3분기 미공시 시 CFS/OFS 빈 응답)은 운영에서만 관측 가능하다. 외부 API는 호출하지 않았다.
- 1~3월에는 최근 계획이 직전 연도(미제출 연간)와 그 분기만 받으므로, 그 기간 전전년도 연간 정정은 30일 sweep 범위 밖이다.
