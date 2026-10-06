# Maker 기록 — 토스 1분봉 수집 결측 방지와 과거 결측 복구 CLI

RECORD: maker-TossMinuteGapRepair
DATE: 2026-10-06
SCOPE: toss minute candles, kr_candles_1m_toss, 1분봉 수집 결측, before 커서, 커버리지 선정, 재시도 백오프, future_minute, 결측 복구 CLI, 2026-09-30
PATHS: app/jobs/toss_minute_candles.py, app/services/research_candles/toss_minute_source.py, app/services/research_candles/toss_minute_repository.py, app/services/research_candles/toss_minute_gap_repair.py, scripts/repair_toss_minute_gaps.py, tests/services/research_candles/test_toss_minute_persistence.py, docs/runbooks/toss-minute-gap-repair.md
STATUS: accepted (Main 검수 대기. 커밋·배포·`--commit` 복구는 하지 않았다)

## 원인 (관측)

- 활성 3,944종목(`kr_symbol_universe`, NXT 602·KRX 3,342)을 분당 20종목씩 벽시계 분 번호
  오프셋으로 돌렸다. 한 바퀴 약 197.2분인데 한 번에 받는 봉은 200개라 여유가 3분도 안 된다.
- 어떤 분의 실행이 빠지면(워커 재시작·지연) 그 분 오프셋의 20종목은 다음 바퀴(약 197분 뒤)까지
  안 잡혀 200봉 밖이 영구 결측이 된다. 늦게 돈 실행은 자기 시각으로 오프셋을 다시 계산해 빠진
  배치를 건너뛰고, 밀린 실행 둘은 같은 배치를 두 번 돈다.
  2026-10-06 13:45 KST 재시작 직후 실행 두 개가 6초 간격으로 같은 `0445Z` 배치를 돌았다.
- 그 배치가 분 경계를 넘으면서 `future_minute`로 종목이 실패했다. 1m `timestamp`는 봉 종료 시각이라
  진행 중 봉에는 응답 시각의 다음 분이 붙는데, 판정 기준이 배치 시작 시각이었다. 실패 종목은
  재시도가 없었다.
- 피해 규모: 일별 행 수가 09-29 2,193,590, 09-30 1,636,480, 10-01 2,214,456이다. 10-06 15:31 KST
  선정 쿼리 결과로는 `000660` 등 가장 오래된 정상 종목의 마지막 수집이 09:30 KST였다(6시간 전).
  14일 안에 봉이 없는 종목(`094800` 404 `stock-not-found` 포함) 3개와 09-28 이후 멈춘 종목
  4개도 있었다(`evidence/selection-plan.sql.txt`).

## 무엇을 바꿨나

- 선정(A): `TossMinuteCandleRepository.stalest_active_symbols`. 활성 종목마다 최신 저장 봉 1건을
  `(symbol, time_utc DESC)` 인덱스 LATERAL로 짚어 **그 봉의 `retrieved_at`(마지막 성공 수집 시각)이
  오래된 순**으로 20개를 고른다. 14일 안에 봉이 없으면 NULL로 맨 앞에 둔다.
  `time_utc`가 아니라 `retrieved_at`을 키로 쓴 이유는 이렇다. 15:30 이후 KRX 전용 종목은 최신 봉
  시각이 더 나아가지 않아서, `time_utc`를 키로 쓰면 3천여 종목이 매 분 다시 뽑혀 NXT 종목이 밀린다.
  운영 DB 실측(generic plan, 캐시 warm)은 97ms, 버퍼 hit 15.9k였다. 첫 실행은 디스크 읽기 544건
  때문에 5.7초가 걸렸다.
- 재시도: 빠진 분과 실패 종목은 `retrieved_at`이 그대로라 다음 실행에서 다시 맨 앞에 온다.
  계속 행을 못 만드는 종목(404·빈 응답)은 워커 프로세스 메모리의 `_RetryBackoff`가 막는다.
  첫 실패는 다음 실행에서 다시 시도하고, 연속 두 번째부터 2·4·8…분, 최대 240분 쉰다.
  지수는 8로 묶어 오래 도는 워커에서도 `timedelta` 오버플로가 나지 않게 했다.
- 메우기(B): 최신 페이지의 가장 오래된 봉이 저장된 마지막 봉보다 뒤면 `nextBefore`로 뒤 페이지를
  받는다. 종목당 추가 3쪽, 실행당 추가 20쪽이 상한이다. 따라서 **실행당 Toss 호출은 최대 40회**
  (`MARKET_DATA_CHART`)다. 상한에 걸려 남는 구멍은 `unfilled_gaps`와 경고 로그
  `Toss minute gap left for the repair CLI ...`로 남긴다. 그 구멍은 1분 작업이 다시 감지하지 못하므로
  복구 CLI 몫이다(Main 지시, 런북 §2에 명시).
- `future_minute` 판정을 응답 시각(`clock`, 기본 `datetime.now(UTC)`) 기준 다음 분까지로 바꿨다.
  그 너머의 봉은 여전히 종목 실패로 처리한다.
- `TossMinuteCandleSource.fetch`는 `before`를 받고 `TossMinutePage(rows, next_before)`를 돌려준다.
  `_batch_offset`, `active_symbol_count`, `active_symbol_batch`는 쓰는 곳이 없어 지웠다. 결과 dict에서
  `batch_offset`·`batch_limit`·`symbols_total`이 빠지고 `toss_calls`·`gap_fill_pages`·
  `unfilled_gaps`·`symbols_backing_off`가 붙었다. `batch_id`는 `toss-1m-<UTC 분>`이다(외부 소비자 없음,
  grep 확인).
- 복구(C): `app/services/research_candles/toss_minute_gap_repair.py`와
  `scripts/repair_toss_minute_gaps.py`. `--commit` 없으면 dry-run이고 Toss·쓰기 모두 없다.
  `--commit`은 종목별 가장 늦은 결측 분부터 inclusive `before`로 거꾸로 걸으며 그 세션 날짜 봉만
  upsert하고 종목마다 커밋한다. `--max-calls`로 호출 수를 묶는다.
- 스케줄(`* 8-19`, `0 20`), `TOSS_MINUTE_BATCH_SIZE=20`, 업서트 키, 세그먼트 분류·padding·value
  의미, 새 테이블·마이그레이션·스케줄은 바꾸지 않았다.

## 한도 근거

- 공식 Rate Limits 표(openapi.tossinvest.com/openapi-docs/overview.md):
  `MARKET_DATA_CHART` 초당 20회, 클라이언트(앱 키)×그룹 단위.
- 실호출 응답 헤더 `X-RateLimit-Limit=20`(`evidence/toss-candles-probe-20261006.txt`).
- 1분 작업은 분당 최대 40회(평균 0.67회/초)이고, 워커 프로세스 리미터(`TossRateLimiter`, 이 그룹
  초당 5회)를 그대로 지난다. 운영 차트 조회와 같은 예산을 나눠 쓴다.

## Toss `before` 확인 (openapi v1.2.19 + 실호출 7건)

- `before`는 inclusive 상한이고, `nextBefore`는 가장 오래된 봉 - 1분이다. 응답은 최신순이다.
- `+09:00`(httpx가 `%2B`로 인코딩)과 `Z` 모두 같은 결과를 준다. 2026-01-05 분봉도 받는다.
- KRX 전용 `000020`도 09-30 20:00까지 거래량 0 padding 봉이 온다.
- 실호출은 장 마감 뒤 compose 일회성 worker 컨테이너에서 캐시 토큰만으로 했다(재발급·429 재시도 끔).
  `docker run --env-file .env.kasset`는 따옴표 값 때문에 `Settings` 검증에서 실패했고, 그때 Toss 호출은
  없었다. 같은 방식을 런북에도 반영했다.

## 2026-09-30 dry-run (운영 DB 읽기 전용, 10-06 장 마감 뒤)

- 첫 판정 규칙("그 날 과반이 가진 분")은 격자를 정규장 224·312·235분으로 줄여 결측을 크게 놓쳤다.
  09-30은 한 번의 장애로 대부분 종목이 같은 시간대(예: 08:15~14:46)를 잃었다. 그래서 규칙을
  바꿨다. 기대 세그먼트는 앞뒤 세션 양쪽에 있던 세그먼트를 더하고, 클래스 격자에는 앞뒤 세션
  모두에서 과반이었던 KST 시각을 더한다. 회귀 테스트는
  `test_gap_plan_keeps_minutes_most_symbols_lost_together`다.
- 최종 결과(`evidence/dryrun-20260930.json`, 31초, 최대 RSS 약 48MB):
  - 클래스: KRX+시간외 1,894종목(390+270분), KRX 전용 1,441종목(390분),
    NXT 601종목(59+391+270분)
  - `symbols_expected=3936`, `symbols_without_rows=0`
  - `symbols_with_gaps=3230`, `missing_minutes=608270`, `gap_runs=3721`
  - `estimated_calls=4091`
  - 검산: 기대 행 2,244,750 − 저장 행 1,636,480 = 608,270
- `--commit`은 실행하지 않았다(Main이 사용자 승인 후 실행).

## 검증

`docs/runbooks/server-pytest-runner.md` §1·§4·§5·§7·§8 절차로 16:20 KST 이후 `kasset-server`에서 돌렸다.
기준은 배포 커밋 `f1d8bf4b` clone에 변경 6파일을 덮어쓴 checkout이다. 로컬 작업 트리는 `core.autocrlf=true`라
기존 3파일이 CRLF였는데, 서버 임시 checkout에서만 LF로 맞춰 커밋될 형태와 같게 했다.

| 회차 | 명령 | 결과 | 원문 |
|---|---|---|---|
| r1 | pytest `tests/services/research_candles/test_toss_minute_persistence.py` (`--cpus=1.0`, `--network container:kasset-test-db`, run-owned DB) | `30 passed` exit 0 | `evidence/server-validation-r1.txt` |
| r1 | ruff/ty (볼륨 `kasset-pytest-deps-4e6329d1`) | exit 127. 이 볼륨에 ruff·ty가 없다 | 같은 파일 |
| r1' | 같은 checkout에서 볼륨만 `kasset-pytest-deps-04d62828-swing`(ruff 0.15.9·ty 0.0.29 dist-info 확인)으로 바꿔 정적 검사 | `ruff check` 0, `ruff format --check` 1(2파일), `ty check --error-on-warning` 0 | `evidence/server-static-r1.txt` |
| — | 포맷 diff 4곳을 그대로 반영(gap_repair 1, 테스트 3) | — | — |
| r2 | 최종 revision 전체 재실행(볼륨 `04d62828-swing`, 검증 핀은 `f1d8bf4b`의 `uv.lock`과 동일) | pytest `30 passed, 12 warnings` exit 0, `ruff check` 0, `ruff format --check` 0(6 files already formatted), `ty` 0 | `evidence/server-validation-r2.txt` |

- r2 가드 줄: `ROB-1296 external HTTP boundary: 0 blocked requests`,
  `test schema bootstrap: databases=1 applied=1`, `ROB-1880 socket guard: active=True blocked_attempts=0`.
  부하는 `load average: 4.60, 3.04, 2.13`, 컨테이너 CPU 91.41%였다.
- 경고 12건은 모두 `OpenDartReader/dart_utils.py`의 `SyntaxWarning`이다(서드파티, 이번 변경과 무관).
- 격리 확인: 운영 DB `public` 테이블 수 110(변동 없음), 운영 컨테이너 9개가 그대로 떠 있었다.
- 정리: 서버 `/tmp` 임시 checkout·스크립트·SQL과 로컬 임시 파일을 지웠다.
- 새 테스트 파일은 없다. 기존 `test_toss_minute_persistence.py`(`ci_shards/shard-3.txt`)를 확장했다.

### 수용 조건 대응

| 수용 조건 | 상태 | 근거 |
|---|---|---|
| 분 작업이 빠지거나 fetch가 실패해도 다음 실행에서 다시 선정 | met | `test_failed_and_skipped_minute_symbols_are_selected_on_the_next_run`, `test_symbol_that_keeps_failing_backs_off_instead_of_taking_every_slot`, PostgreSQL 순서 `test_stalest_active_symbols_orders_by_last_fetch_on_postgres` (r2) |
| 200봉 밖 틈을 페이지 상한 안에서 메움 | met | `test_gap_beyond_the_latest_page_is_filled_with_before_cursor`(틈 메움·틈 없음·진행 중 봉 재기록), `test_gap_wider_than_the_symbol_page_cap_is_reported_for_repair`, `test_run_page_budget_caps_toss_calls_per_minute`, 커서 경계 `test_source_forwards_before_cursor_and_returns_next_cursor` (r2) |
| 분당 Toss 호출 상한과 한도 근거 | met | 코드 상수 `TOSS_MINUTE_GAP_FILL_*`(실행당 최대 40), 공식 표 20/s, 응답 헤더 `X-RateLimit-Limit=20` (`evidence/toss-candles-probe-20261006.txt`) |
| 복구 CLI dry-run 기본 + 2026-09-30 dry-run 결과 | met | `test_repair_cli_defaults_to_dry_run_without_toss_or_writes`, `evidence/dryrun-20260930.json` (결측 3,230종목·608,270분, 예상 4,091회) |
| upsert 멱등 | met | `test_regular_tick_upserts_rows_idempotently_and_updates_partial_revision`, `test_repository_uses_target_unique_key_for_idempotent_upsert`, DB 테스트 재업서트 1행 유지 (r2) |
| 서버 격리 pytest·Ruff·ty | met | 위 표 r2 |

### 재사용할 교훈 후보

- 운영 서버에서 일회성으로 앱 코드를 돌릴 때 `docker run --env-file .env.kasset`를 쓰면 따옴표 값 때문에
  `Settings` 검증이 실패한다(`AUTH_SMTP_PORT` 등 6건). `docker compose --env-file .env.kasset -f
  docker-compose.kasset.yml run --rm --no-deps -T ... worker`를 쓴다. 스크립트 파일 경로로 실행하면
  `-e PYTHONPATH=/app`이 필요하다.
- `kasset-pytest-deps-4e6329d1` 볼륨에는 ruff·ty가 없다(exit 127). 정적 검사는 ruff·ty dist-info가 있는
  볼륨을 쓰거나 새로 만든다.

## 남은 위험·미확인

- 프로세스 메모리 백오프는 워커 재시작 때 초기화된다. 재시작 직후 404·빈 응답 종목 7개가 몇 번 더
  시도된다(2·4·8분 간격).
- 밀린 실행 둘이 동시에 돌면 같은 20종목을 고른다. 데이터 손실은 없고 처리량만 준다. advisory lock은
  넣지 않았다.
- 순환 여유가 얇다(3,944/20 = 197.2분 vs 200봉). 워커가 자주 실행을 놓치면 실행마다 추가 페이지가
  늘어난다. 실행당 20쪽 상한을 넘는 구멍은 CLI 몫이다.
- 짧은 세션(개장 지연 등) 날짜에는 앞뒤 세션 시각이 결측으로 계획된다. `--commit`에서는
  `minutes_not_returned`로 남고 쓰지 않는다.
- 10-06 데이터도 같은 결함으로 비어 있다(15:30까지 1,185,477행). 배포 뒤 해당 날짜도 CLI로 dry-run을
  해 봐야 한다.
