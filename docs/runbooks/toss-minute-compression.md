# 토스 1분봉 압축 (TimescaleDB)

`research.kr_candles_1m_toss`(hypertable, 청크 7일)를 7일 지난 청크부터 압축한다.
마이그레이션 `20261006_toss_minute_compression`이 켠다. 운영 디스크를 80GB로 옮기기
전에 주간 청크 약 3~3.5GB(heap + 인덱스 5개)를 줄이려는 것이다.

## 1. 설정

| 항목 | 값 |
|---|---|
| 압축 | `timescaledb.compress`, `compress_segmentby='symbol'`, `compress_orderby='time_utc DESC'` |
| 정책 | `add_compression_policy(compress_after => 7 days)` (청크 끝이 7일 넘게 지난 것만) |
| 실행 | 하루 1번 02:30 KST(`fixed_schedule`, `timezone => 'Asia/Seoul'`). NXT 마감(20:00)과 DB 백업(18:30) 뒤, 개장(08:00 NXT, 09:00 KRX) 전 |
| 재시도 | `max_retries=3`, `retry_period=30분`. 실측(2.29.2): 실패 뒤 +33.5분, +67분(지수 백오프)이고 **연속 3번째 실패에서 job이 `Paused`가 된다.** 마지막 시도가 첫 실패 후 약 1시간 50분이라 08:50 KST 전에 끝나게 한 것이고, 대가로 사람이 재개해야 한다(4절) |
| 마이그레이션 | 기존 청크를 동기 압축하지 않는다. 첫 압축은 정책 첫 실행(배포 다음 02:30 KST) |
| 하지 않는 것 | cagg 정책, retention, 다른 hypertable |

실측(2026-10-06, 격리 DB, 운영 청크 2개의 종목 1/8 표본): 전체 22.43배·22.70배, heap 13.7~13.9배,
인덱스는 143.6MB가 0.16MB. 표본은 운영보다 촘촘해 운영에서는 더 높게(추정 약 25배, 미실측) 나온다.
첫 실행 대상은 청크 4개(약 11.5GB)이고 압축 뒤 약 0.4GB로 예상한다(추정).

## 2. 상태 조회

```sql
-- 압축 켜짐과 설정
SELECT hypertable_name, compression_enabled FROM timescaledb_information.hypertables
 WHERE hypertable_name = 'kr_candles_1m_toss';
SELECT attname, segmentby_column_index, orderby_column_index, orderby_asc
  FROM timescaledb_information.compression_settings WHERE hypertable_name = 'kr_candles_1m_toss';

-- 청크별 상태. PARTIAL은 압축 청크에 DML이 들어가 재압축을 기다린다는 뜻이다.
-- chunk_compression_stats()의 compression_status는 PARTIAL도 'Compressed'로 보이니 이 쿼리를 쓴다.
SELECT c.chunk_name, c.range_start::date, c.range_end::date,
       _timescaledb_functions.chunk_status_text(
         format('%I.%I', c.chunk_schema, c.chunk_name)::regclass) AS status
  FROM timescaledb_information.chunks c
 WHERE c.hypertable_name = 'kr_candles_1m_toss' ORDER BY c.range_start;

SELECT * FROM hypertable_detailed_size('research.kr_candles_1m_toss');
SELECT * FROM chunk_compression_stats('research.kr_candles_1m_toss');

-- 정책 job. job_status가 Paused이거나 last_run_status가 Failed이면 4절.
SELECT j.job_id, j.scheduled, j.next_start, j.max_retries, j.retry_period,
       s.job_status, s.last_run_status, s.last_run_started_at, s.total_runs, s.total_failures
  FROM timescaledb_information.jobs j JOIN timescaledb_information.job_stats s USING (job_id)
 WHERE j.proc_name = 'policy_compression' AND j.hypertable_name = 'kr_candles_1m_toss';
SELECT start_time, err_message FROM timescaledb_information.job_errors
 WHERE job_id = <job_id> ORDER BY start_time DESC LIMIT 5;
```

## 3. 장중 회피

정책은 02:30 KST에만 돈다. 수동으로 압축·복원·`run_job`을 돌릴 때도 KRX 장중(08:50~16:20)과
NXT 거래 시간(~20:00)을 피한다. 운영 호스트는 2 vCPU라 압축(1코어)이 1분 작업과 겹치면
느려진다. DB 백업(18:30)과 복구 CLI `--commit` 시각도 피한다. 정책이 돌 때 7일 안쪽 청크와
1분 수집(최근 약 800분만 upsert)은 건드리지 않는다.

## 4. Paused job 재개

연속 3번 실패하면 job이 `scheduled=false`, `next_start=NULL`이 되고 다음 날에도 돌지 않는다
(알림 없음). 원인(디스크, 락, 청크 상태)을 `job_errors`로 보고 고친 뒤 재개한다. 재개한 뒤
또 실패하면 실패 카운터가 안 지워져 바로 다시 Paused가 된다. 02:30 KST 밖에서 돌리면 3절을 지킨다.

```sql
SELECT alter_job(<job_id>, scheduled => true, next_start => now() + interval '1 minute');
-- 지금 한 번 돌리기(트랜잭션 밖에서, autocommit)
CALL run_job(<job_id>);
```

## 5. 되돌리기

- 정책만 멈춤: `SELECT alter_job(<job_id>, scheduled => false);`
- 청크 하나 복원: `SELECT decompress_chunk('_timescaledb_internal._hyper_20_25_chunk');`
- 전체 되돌리기: `alembic downgrade -1`(정책 제거 -> 압축 청크 전부 decompress -> compress 끄기 순서).
  **경고: 전체 행을 다시 써서 느리고 디스크를 쓴다.** 실측 약 59k 행/초(격리 DB 270만 행 45.6초).
  현재 6청크 약 3천만 행이면 10분 안팎, 복원 뒤 크기만큼(약 15GB) 여유 디스크가 필요하다. 코드로 막지
  않았으니 여유와 시각(3절)을 확인하고 실행한다.

## 6. 압축 청크를 쓰는 쪽

- 1분 수집(`app/jobs/toss_minute_candles.py`)은 최근 약 800분만 upsert하므로 압축 청크를 건드리지 않는다.
- 복구 CLI는 임의의 과거 세션을 upsert한다. 압축 청크에서도 동작한다. 절차와 실측은
  [toss-minute-gap-repair.md](toss-minute-gap-repair.md) 4절.
- DML 한계: 한 트랜잭션이 decompress하는 튜플이 10만(`timescaledb.max_tuples_decompressed_per_dml_transaction`)을
  넘으면 `tuple decompression limit exceeded by operation`(53400)으로 실패한다. 복구 CLI는 종목별 커밋이라
  한 번에 많아야 3천 튜플이다.
- 근거: ON CONFLICT DO UPDATE는 충돌한 배치만 decompress한다(timescale/timescaledb PR #7108, 2.16 계열).
  2.11부터 압축 데이터 UPDATE/DELETE·ON CONFLICT가 된다. 한도와 해결은
  https://www.tigerdata.com/docs/build/tips-and-tricks/troubleshoot-hypertables . 이 표의
  유니크 키 `(time_utc, symbol)`에 segmentby(`symbol`)와 orderby(`time_utc`)가 들어 있어 충돌 검사가 배치를 좁힌다.

## 7. 조회 영향 실측 (격리 표본, 운영의 약 12.5% 규모, 중앙값 ms)

| 조회 | 비압축 | 압축 |
|---|---|---|
| `kr_candles_5m_toss` 1종목 1주 | 13.6 | 11.6 |
| `kr_candles_1h_toss` 1종목 1주 | 11.4 | 9.8 |
| `kr_candles_5m_toss` 하루 전 종목 | 1089 | 1148~1355(잡음) |
| `kr_candles_5m_toss` 연구식(100종목 1주, KRX_REGULAR, non-padding) | 836 | 648 |
| 1분봉 하루 20종목(`fetch_kr_toss_minutes`) | 10.7 | 7.5 |
| `load_same_time_bucket_volumes` 20종목 x 4버킷 55일(RVOL shadow, 20초 제한) | 219 | 186 |
| `stalest_active_symbols` LATERAL 3,944종목 | 36 | 49 |
| 복구 CLI `session_segment_counts` 하루 | 97 | 23 |
| 복구 CLI `max/min(session_date_kst)` 이웃 세션 | 0.0 / 0.0 | 86 / 187 |

- cagg 뷰(`kr_candles_{5m,15m,30m,1h}_toss`)는 refresh 정책이 없는 실시간 뷰라 압축 여부와 관계없이 원본
  1분봉을 집계한다. `app/`과 `scripts/`에서 이 뷰를 읽는 코드는 없다(연구 SQL만:
  `doc/history/2026/09/{22,23,28}-*`).
- **미측정**: 운영 규모(청크 6개, 행 8배)에서의 위 조회, 하루 전 종목 cagg 조회의 `EXPLAIN ANALYZE` 원문,
  장중 1분 작업·RVOL shadow 동시 실행 중의 지연. 사용자 요청으로 15분 범위에서 생략했다. 첫 정책 실행 뒤
  `load_same_time_bucket_volumes`가 20초 제한에 걸리면(`baseline_load_failed` 로그) 이 표를 다시 잰다.
- 압축 후 `ANALYZE`를 안 한 상태와 한 상태의 결과 차이는 없었다(잡음 범위).
- 롤백한 대량 DML은 죽은 튜플로 압축 청크를 부풀린다(실측 34MB -> 68MB). 재압축 또는 VACUUM 전까지 유지된다.
