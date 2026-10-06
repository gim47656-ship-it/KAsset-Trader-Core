# 토스 1분봉 수집 결측 방지와 복구

`research.kr_candles_1m_toss`를 채우는 1분 작업(`research.candles.kr.toss.1m.sync`,
`* 8-19 * * 1-5`·`0 20 * * 1-5` KST)이 빠진 분·실패 종목을 스스로 메우는 방식과,
그래도 남은 구멍을 세션 날짜 단위로 메우는 일회성 CLI
`scripts/repair_toss_minute_gaps.py`의 사용법이다.

## 1. 1분 작업이 고르는 종목

한 번 실행에 활성 종목(`kr_symbol_universe.is_active`) 20개(`TOSS_MINUTE_BATCH_SIZE`)를
고른다. 순서는 **마지막으로 성공한 수집이 가장 오래된 종목부터**다.

- 기준은 종목별 최신 저장 봉의 `retrieved_at`이다. 그 봉을 마지막으로 가져온 시각이라,
  장이 끝난 KRX 전용 종목도 다시 가져오면 앞으로 간다(최신 봉의 `time_utc`를 기준으로
  쓰면 15:30 이후 KRX 전용 종목 3천여 개가 매 분 다시 뽑혀 NXT 종목이 밀린다).
- 최근 14일(`TOSS_MINUTE_COVERAGE_LOOKBACK`) 안에 저장된 봉이 없는 종목은 "수집한 적
  없음"으로 보고 맨 앞에 둔다. 조회는 활성 종목마다 `(symbol, time_utc DESC)` 인덱스를
  한 번 짚는 LATERAL이며 하이퍼테이블 행을 훑지 않는다.
- 어떤 분의 실행이 통째로 빠지거나(워커 재시작 등) 종목 fetch가 실패하면 그 종목의
  `retrieved_at`이 그대로라 다음 실행에서 다시 맨 앞에 온다. 예전처럼 벽시계 분 번호로
  오프셋을 정하던 방식은 빠진 분의 20종목을 한 바퀴(약 197분) 뒤로 미뤘다.
- 404·빈 응답처럼 계속 행을 못 만드는 종목은 워커 프로세스 메모리의 지수 백오프를 탄다.
  첫 실패는 바로 다음 실행에서 다시 시도하고, 연속 두 번째부터 2·4·8…분, 최대 240분
  쉰다(`TOSS_MINUTE_RETRY_BACKOFF_CAP_MINUTES`). 워커가 재시작되면 초기화된다.

## 2. 최신 200봉 밖으로 밀린 틈 메우기

최신 페이지(200봉)의 가장 오래된 봉이 저장된 마지막 봉보다 뒤에 있으면 그 사이가 비어
있을 수 있다. 이때 응답의 `nextBefore`를 `before`에 넣어 뒤 페이지를 더 받는다.
Toss `before`는 "그 시각 이하의 봉"을 주는 inclusive 상한이고, 1m `timestamp`는 봉
종료 시각이다(openapi v1.2.19).

| 상한 | 값 | 넘으면 |
|---|---|---|
| 종목당 추가 페이지 | 3 (`TOSS_MINUTE_GAP_FILL_SYMBOL_PAGES`) | `unfilled_gaps.<symbol>.reason = symbol_page_cap` |
| 실행당 추가 페이지 | 20 (`TOSS_MINUTE_GAP_FILL_RUN_PAGES`) | `unfilled_gaps.<symbol>.reason = run_page_budget` |
| 실행당 Toss 호출 | 최대 40 = 기본 20 + 추가 20 | — |

호출은 `MARKET_DATA_CHART` 그룹이다. 공식 한도는 클라이언트(앱 키)×그룹당 초당 20회이고
운영 차트 조회와 같은 예산을 나눠 쓴다. 1분 작업은 분당 최대 40회(평균 0.67회/초)이고,
워커 프로세스 리미터(`TossRateLimiter`, 이 그룹 초당 5회)를 그대로 지난다.

**상한을 넘어 남은 구멍은 1분 작업이 다시 보지 못한다.** 더 새 페이지를 저장하는 순간
그 종목의 마지막 봉이 앞으로 가서, 다음 실행은 그 아래 구멍을 모른다. 이런 구멍은
워커 로그에 아래 경고로 한 번 남고, 메우는 일은 3절 CLI 몫이다.

```text
Toss minute gap left for the repair CLI symbol=<symbol> stored_through=<UTC> fetched_from=<UTC> reason=<reason>
```

한 실행의 결과 dict에는 `toss_calls`, `gap_fill_pages`, `unfilled_gaps`,
`symbols_backing_off`가 함께 남는다.

## 3. 세션 날짜 단위 복구 CLI

### 결측 판정

- 종목의 기대 세그먼트(`NXT_PRE`·`KRX_REGULAR`·`NXT_POST`)는 그 날 봉이 있는 세그먼트에
  앞뒤로 가장 가까운 저장 세션 **양쪽에 모두** 있던 세그먼트를 더한 것이다. 그래서
  정규장이 통째로 빠진 종목이나 그 날 봉이 하나도 없는 종목도 잡힌다.
- 기대 세그먼트가 같은 종목끼리 한 클래스로 묶는다. 2026-09-30 기준 실제 클래스는 셋이다.

  | 클래스 | 종목 수 | 격자 |
  |---|---|---|
  | `KRX_REGULAR`+`NXT_POST` (KRX 종목, 시간외 padding 포함) | 1,894 | 09:01~15:30 390분 + 15:31~20:00 270분 |
  | `KRX_REGULAR` (KRX 전용) | 1,441 | 09:01~15:30 390분 |
  | `NXT_PRE`+`KRX_REGULAR`+`NXT_POST` (NXT 종목) | 601 | 08:01~08:59 59분 + 09:00~15:30 391분 + 270분 |

- 클래스 격자는 "그 날 그 세그먼트에 봉이 있는 클래스 구성원의 과반이 가진 분"에 "앞뒤
  세션 모두에서 과반이었던 KST 시각"을 더한 것이다. 2026-09-30처럼 한 번의 장애가 대부분
  종목에서 같은 시간대를 지우면 그 날 과반만으로는 격자가 줄어(정규장 224분 등) 결측을
  크게 놓친다. 구성원이 3개 미만인 클래스는 그 세그먼트를 가진 가장 큰 클래스의 격자를
  빌린다.
- Toss는 거래 없는 분도 거래량 0 봉(`is_padding`)으로 채워 주므로, 빠짐없는 종목은
  격자와 정확히 같다.
- 한계: 개장 지연처럼 그 날만 짧은 세션이면 앞뒤 세션의 시각이 결측으로 계획된다.
  `--commit`에서 Toss가 그 분을 주지 않으므로 `repair.minutes_not_returned`로 남고 쓰지
  않는다.
- 예상 호출 수는 종목별로 가장 늦은 결측 분에서 `before`로 200개씩 거꾸로 걸으며 남은
  결측이 덮일 때까지의 페이지 수다. Toss가 격자의 모든 분을 준다고 가정한 값이다.

### dry-run (기본, 쓰기 없음)

운영 DB를 읽기만 하고 Toss도 부르지 않는다. KRX 장중(09:00~15:30 KST)을 피해 돌린다.
배포된 이미지에는 `scripts/`가 들어 있으므로 worker 서비스 정의로 일회성 컨테이너를
띄운다. `.env.kasset`은 따옴표 값이 있어 compose가 읽어야 한다(`docker run --env-file`로
넘기면 `Settings` 검증에서 실패한다).

```bash
ssh kasset-server
cd /opt/kasset-trader-core
docker compose --env-file .env.kasset -f docker-compose.kasset.yml \
  run --rm --no-deps -T --entrypoint /app/.venv/bin/python worker \
  -m scripts.repair_toss_minute_gaps --session-date 2026-09-30
```

출력(JSON) 주요 필드:

| 필드 | 뜻 |
|---|---|
| `neighbour_session_dates` | 기대 세그먼트·격자를 보탠 앞뒤 저장 세션 |
| `symbol_classes` | 클래스별 세그먼트, 종목 수, 격자 분 수 |
| `symbols_expected`·`symbols_without_rows` | 기대 종목 수, 그중 그 날 봉이 하나도 없는 종목 수 |
| `symbols_with_gaps`·`missing_minutes`·`gap_runs` | 결측 종목 수, 결측 분 수, 연속 구간 수 |
| `estimated_calls` | `--commit` 때 예상 Toss 호출 수 |
| `largest_gaps` | 결측이 큰 종목 상위 `--top`개(기본 10) |

`--symbols 005930,000660`으로 일부 종목만 볼 수 있다.

2026-10-06 운영 DB dry-run(장 마감 뒤, 31초, 최대 RSS 약 48MB) 결과:
`symbols_expected=3936`, `symbols_without_rows=0`, `symbols_with_gaps=3230`,
`missing_minutes=608270`, `gap_runs=3721`, `estimated_calls=4091`. 기대 행
2,244,750(1,894×660 + 1,441×390 + 601×720)에서 저장 행 1,636,480을 뺀 값과 결측 분 수가
같다. 원문은 `doc/history/2026/10/06-toss-minute-gap/evidence/dryrun-20260930.json`.

### --commit (사용자 승인 후에만)

`--commit`은 Toss를 부르고 그 세션 날짜의 봉만 upsert한다(다른 날짜 봉은 받아도 쓰지
않는다). 키는 `uq_research_kr_candles_1m_toss_time_symbol`이라 다시 돌려도 같은 행을
덮어쓸 뿐이고, 종목마다 커밋하므로 중간에 끊겨도 쓴 만큼은 남는다. 다시 돌리면 남은
결측만 다시 계획한다.

```bash
docker compose --env-file .env.kasset -f docker-compose.kasset.yml \
  run --rm --no-deps -T --entrypoint /app/.venv/bin/python worker \
  -m scripts.repair_toss_minute_gaps --session-date 2026-09-30 \
  --commit --max-calls 5000
```

- 20:00 KST 이후나 장 시작 전에 돌린다. 1분 작업과 운영 차트 조회가 같은
  `MARKET_DATA_CHART` 예산을 쓰고, CLI 프로세스의 리미터는 초당 5회다. 2026-09-30의
  예상 4,091회는 초당 5회로 약 14분이다.
- `--max-calls`로 호출 수를 묶을 수 있다. 상한에 닿으면 `repair.stopped_at_call_cap`이
  `true`가 되고, 같은 명령을 다시 돌리면 이어서 메운다.
- `repair.minutes_not_returned`는 요청 범위 안이었는데 Toss가 주지 않은 분이다. Toss
  자체에 없는 분이므로 다시 돌려도 채워지지 않는다.
- 결과 확인은 같은 날짜로 dry-run을 다시 돌려 `missing_minutes`가 줄었는지 본다.

### GitHub Actions로 실행 (서버 셸 없이)

운영서버의 self-hosted 러너(`kasset-prod`)에서 같은 명령을 도는 수동 워크플로
`.github/workflows/toss-minute-gap-repair.yml`(이름 `Toss Minute Gap Repair`)이 있다.
스케줄·자동 트리거는 없고 `workflow_dispatch`로만 돈다. 기본은 dry-run이다.

```bash
# dry-run
gh workflow run toss-minute-gap-repair.yml -f session_date=2026-09-30
# commit (사용자 승인 후)
gh workflow run toss-minute-gap-repair.yml -f session_date=2026-09-30 -f commit=true -f max_calls=5000
gh run list --workflow toss-minute-gap-repair.yml --limit 1   # run id 확인 후 gh run view <id> --log
```

- `commit=true`는 평일 08:00~20:04 KST에 거부된다(1분 작업 마지막 실행 20:00과 겹치지 않게).
- Deploy와 같은 `production-deploy` concurrency 그룹이라 배포 중이면 끝난 뒤에 돈다.
- 실행 이미지는 운영 `.env.kasset`의 `CORE_IMAGE_TAG`(현재 배포본)다.

## 4. 대상 세션이 압축 청크에 있을 때

`research.kr_candles_1m_toss`는 청크 끝이 7일 넘게 지나면 매일 02:30 KST에 압축된다
([toss-minute-compression.md](toss-minute-compression.md)). 7일 안쪽 세션은 영향이 없고,
가능하면 압축 전에 복구한다(예: 2026-09-30은 청크 `09-24~10-01`이라 2026-10-08 02:30 KST 전).

세션이 압축 청크에 있는지:

```sql
SELECT c.chunk_name, c.range_start::date, c.range_end::date,
       _timescaledb_functions.chunk_status_text(
         format('%I.%I', c.chunk_schema, c.chunk_name)::regclass) AS status
  FROM timescaledb_information.chunks c
 WHERE c.hypertable_name = 'kr_candles_1m_toss' ORDER BY c.range_start;
```

`{COMPRESSED}`이면 압축 청크다. 절차는 그대로이고 CLI(`--commit`)를 같은 명령으로 돌리면 된다.
청크를 미리 decompress할 필요가 없다. 격리 DB 실측(TimescaleDB 2.29.2, 압축 청크 2개 270만 행):

| 작업 | 결과 |
|---|---|
| 한 종목 한 세션 720행(500+220행 `ON CONFLICT ... DO UPDATE`) 한 트랜잭션 | 41ms + 9ms. 배치 2개(튜플 2,000개)만 decompress, 중복 키 0 |
| 구멍 메우기(100분 삭제 뒤 같은 100행 INSERT, 충돌 없음) | DELETE 15ms, INSERT 5ms |
| 종목별 커밋 100종목 x 1세션(CLI 방식) | 합계 5.5초(종목당 약 55ms) |
| 한 트랜잭션 300종목 x 1세션(165,790행) | 53400 `tuple decompression limit exceeded by operation`(한도 10만, 101,000튜플에서 2.6초 뒤 실패). `SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0`이면 16.9초에 성공 |

- CLI는 종목마다 커밋하므로 한도에 닿지 않는다. 한 트랜잭션에 여러 종목을 모아 SQL로 직접 upsert하지 않는다.
  어쩔 수 없으면 트랜잭션 안에서만 `SET LOCAL ... = 0`을 쓰고 롤백하지 않는다(롤백된 대량 DML은 죽은 튜플로 청크를 부풀린다).
- upsert한 청크는 `{COMPRESSED,PARTIAL}`이 된다. 조회는 정상이고, 다음 02:30 KST 정책 실행이 재압축한다(실측 0.9초,
  행 해시 불변). 바로 합치려면 02:30 밖이라도 장외에 `CALL run_job(<job_id>)`(job id는 압축 런북 2절).
- dry-run은 `neighbour_session_dates`의 `max/min(session_date_kst)` 조회가 압축 청크에서 인덱스 끝점을 못 써서 느려진다
  (표본 0.0ms -> 86/187ms). 운영 규모는 **미측정**이다.
- 복구 직후 검증은 3절과 같다: 같은 날짜 dry-run의 `missing_minutes`가 줄었는지 본다.
