# Maker 기록: 토스 1분봉 압축 정책·deploy.sh 이미지 정리 (중간 인계)

RECORD: maker-TossCompressionImagePrune
DATE: 2026-10-06
SCOPE: toss 1분봉 hypertable 압축(columnstore) 정책, 압축 비율 실측, 압축 청크 upsert, deploy.sh 옛 이미지 정리
PATHS: alembic/versions/20261006_toss_minute_compression.py, deploy/kasset/deploy.sh, tests/services/research_candles/test_toss_minute_compression_migration.py, ci_shards/shard-3.txt, docs/runbooks/toss-minute-compression.md, docs/runbooks/toss-minute-gap-repair.md, doc/history/2026/10/06-toss-compression/
STATUS: partial (Main이 범위를 필수 검증으로 줄임. 필수 항목은 모두 통과. 운영 규모 조회 측정·cagg EXPLAIN 원문·`toss_phase2`/`callback_inbox` 체인 테스트는 미측정/미실행)

## 1. 파일별 완성 여부

| 파일 | 상태 | 비고 |
|---|---|---|
| `alembic/versions/20261006_toss_minute_compression.py` | 완성, 서버 검증 끝 | down_revision=`20261005_kasset_longterm_shadow`, 단일 head |
| `deploy/kasset/deploy.sh` | 완성, 서버 검증 끝 | `prune_old_images` 함수 + 마지막 호출. 작업 트리는 CRLF(autocrlf), git 색인은 LF. 서버 검증은 `tr -d '\r'`로 LF 변환해서 함 |
| `tests/services/research_candles/test_toss_minute_compression_migration.py` | 완성, 서버 실행 통과 | 실제 hypertable round trip(정책 02:30 KST, 활성 청크 미압축, 압축 청크 upsert+구멍 메우기, downgrade 순서). TimescaleDB 없으면 skip. 서버에서 skip 아닌 실제 실행으로 `2 passed` |
| `ci_shards/shard-3.txt` | 한 줄 추가, 정렬·중복 검사 통과 | 167행. `LC_ALL=C sort -c` 통과, `cat ci_shards/shard-*.txt \| sort \| uniq -d` 0줄. `scripts.ci.file_shard_plan check`는 미실행 |
| `docs/runbooks/toss-minute-compression.md` | 완성(신규) | 설정·상태 조회·Paused 재개·되돌리기(경고)·장중 회피·조회 실측. 운영 규모와 cagg EXPLAIN 원문은 "미측정"으로 표기 |
| `docs/runbooks/toss-minute-gap-repair.md` | 완성(4절 추가) | 압축 청크 복구 절차와 실측. 이웃 세션 조회 운영 규모는 "미측정" |

증거 파일(`doc/history/2026/10/06-toss-compression/evidence/`): `deploy-prune-dryrun.txt`, `migration-roundtrip-timescale.txt`, `migration-roundtrip-plain.txt`, `pytest-deps-volume.txt`(비어 있을 수 있음, 아래 6절).

## 2. 압축 비율 (격리 실측, 운영은 SELECT만)

표본: 운영 DB를 `default_transaction_read_only=on`으로 읽어 종목 해시 1/8(`abs(hashtext(symbol)) % 8`)을 청크 단위로 복사. 격리 DB(`kasset-test-db`의 일회용 DB)에 운영과 같은 DDL·인덱스 5개·cagg 4개로 적재, `timescaledb.compress_segmentby='symbol'`, `compress_orderby='time_utc DESC'`로 `compress_chunk`. TimescaleDB 2.29.2, PG 17.10.

| 표본 | 행 | 압축 전 | 압축 후 | 전체 | heap(table+toast) | index |
|---|---|---|---|---|---|---|
| 9/10 주 청크 `_hyper_20_25`, 496종목 | 1,283,752 | 366,264,320 B (heap 222,683,136 + index 143,572,992) | 16,326,656 B | **22.43배** | 13.70배 | 143.6MB -> 0.16MB(약 920배) |
| 9/17 주 청크 `_hyper_20_27`, 502종목 | 1,415,307 | 401,711,104 B | 17,694,720 B | **22.70배** | 13.92배 | 동일 경향 |

- 대표성: 종목 단위 표본이라 종목당 배치 구조를 그대로 보존. 서로 다른 주·종목군 두 표본이 22.4~22.7로 일치. 패딩 행 비율 표본 0.5569 vs 운영 청크 3% 시스템 표본 0.5584, `batch_id` 평균 길이 29.0 동일.
- 복사본은 새로 적재해 운영 청크보다 촘촘하다(index/heap 0.64 vs 운영 0.83). 운영 실제 대비 비율은 더 높게(약 25배) 나올 것으로 추정(미실측).
- 행당 약 12.7B. 압축 3.2초/140만 행, 복원(decompress) 45.6초/270만 행(약 59k 행/초). 압축->복원 후 행 해시 합 동일(7692358483215290042317, 2,699,059행)로 무손실 확인.
- 운영 환산: 청크 15·21·25·27(첫 정책 실행 대상)이 현재 약 11.5GB, 압축 후 약 0.4GB(추정).

## 3. 압축 청크 DML 실측 (복구 CLI와 같은 `INSERT ... ON CONFLICT ON CONSTRAINT uq_research_kr_candles_1m_toss_time_symbol DO UPDATE`, 500행 multi-VALUES)

격리 DB, 압축된 두 청크(총 270만 행)에서:

| 시나리오 | 결과 |
|---|---|
| 한 종목 한 세션(720행, 500+220) 충돌 upsert, 한 트랜잭션 | 41ms + 9ms. `Batches decompressed: 2, Tuples decompressed: 2000`, 중복 키 0 |
| 구멍 메우기: 100분 DELETE 후 같은 100행 INSERT(충돌 없음) | DELETE 15ms, INSERT 5ms |
| 단일 행 DELETE 후 INSERT | 각 1~14ms |
| 종목별 커밋 100종목 x 1세션(CLI 방식) | 5,477ms 합계(종목당 약 55ms) |
| 한 트랜잭션에서 300종목 x 1세션(165,790행) | `ERROR: tuple decompression limit exceeded by operation DETAIL: current limit: 100000, tuples decompressed: 101000` (2.6초 뒤 실패). `SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0`이면 16.9초에 성공 |
| DML 뒤 청크 상태 | `_timescaledb_functions.chunk_status_text(...)`가 `{COMPRESSED,PARTIAL}`. `chunk_compression_stats().compression_status`는 계속 `Compressed`라 부분 상태를 못 본다 |
| 정책 job 실행(`CALL run_job(<id>)`) | 0.9초에 PARTIAL -> `{COMPRESSED}` 재압축, 행 해시 불변 |
| 롤백한 대량 DML 뒤 | 압축 청크 크기가 34MB -> 68MB로 부풀었다(죽은 튜플). VACUUM 또는 재압축 전까지 유지 |

공식 근거(필요한 것만): ON CONFLICT DO UPDATE는 충돌한 배치만 decompress(PR timescale/timescaledb#7108, 2.16 계열), 2.11부터 압축 데이터 UPDATE/DELETE·ON CONFLICT 지원, 2.16.0부터 tuple filtering, `timescaledb.max_tuples_decompressed_per_dml_transaction` 기본 10만(53400). 문서: https://www.tigerdata.com/docs/build/tips-and-tricks/troubleshoot-hypertables , https://docs.timescale.com/api/latest/compression/ . 유니크 키 `(time_utc, symbol)`에 segmentby(`symbol`)와 orderby(`time_utc`)가 모두 들어 있어 충돌 검사가 배치를 좁힌다(`Batches scanned` 1436은 충돌 검사 500튜플 x 종목 배치 약 3개). `symbol`은 NOT NULL이라 NULL segmentby 버그(#10721)와 무관.

## 4. 조회 성능 (격리 DB, 표본 270만 행 = 운영의 약 12.5%, 중앙값 ms, 3회)

| 조회 | 비압축 | 압축 |
|---|---|---|
| `kr_candles_5m_toss` 1종목 1주 | 13.6 | 11.6 |
| `kr_candles_1h_toss` 1종목 1주 | 11.4 | 9.8 |
| `kr_candles_5m_toss` 하루 전 종목(bucket 범위) | 1089.0 | 1147.8(min)~1355(med, 잡음 큼) |
| `kr_candles_5m_toss` 연구식(100종목, 1주, KRX_REGULAR, non-padding) | 835.8 | 648.1 |
| 1m 하루 20종목(`fetch_kr_toss_minutes` 형태) | 10.7 | 7.5 |
| 1m 하루 `session_segment_counts`(CLI dry-run) | 97.4 | 23.2 |
| 1m `max(session_date_kst) < D`(CLI `neighbour_session_dates`) | 0.0 | 85.7 |
| 1m `min(session_date_kst) > D`(CLI) | 0.0 | 187.4 |
| `load_same_time_bucket_volumes`(20종목 x 4버킷, 55일 창) | 219.4 | 185.8 |
| `stalest_active_symbols` LATERAL(3,944종목) | 36.4 | 48.6 |

- 앱이 cagg 뷰를 읽는 경로: `app/`·`scripts/`에는 없다. 읽는 쪽은 `doc/history/2026/09/{22,23,28}-*`의 연구 SQL뿐이다(`rg`로 확인). `app/`은 1분봉 원본 표를 직접 읽는다: `daily_candles/repository.py::fetch_kr_toss_minutes`(`session_date_kst`+`symbol IN`), `research_candles/same_time_volume_profile.py`(`vertical_slice`의 RVOL shadow, 사이클마다 `SET LOCAL statement_timeout='20s'`), `toss_minute_repository.py`(1분 job의 `stalest_active_symbols`, CLI의 `session_*`).
- 이 표본은 2개 청크라 운영(청크 6개 이상, 약 8배)과 규모가 다르다. 압축이 느려진 것은 CLI 전용 `neighbour_*` 두 개뿐이고 운영 규모에서도 초 단위로 예상(미실측).

## 5. 정책 job 실측 (2.29.2)

- `add_compression_policy(..., compress_after=>'7 days', schedule_interval=>'1 day', initial_start=>다음 02:30 KST, timezone=>'Asia/Seoul')`. `fixed_schedule=t`, 마이그레이션 직후 `next_start`=2026-10-07 02:30 KST, `application_name`='Columnstore Policy [id]'.
- 실패 재시도(`max_retries=3, retry_period=30분`): 첫 실패 뒤 +33.5분, 두 번째 뒤 +67분(지수 백오프, 지터 약 12%). **연속 3번째 실패에서 job이 `Paused`(`scheduled=false`, `next_start` NULL)가 되고 다음 날에도 안 돈다.** 재개: `SELECT alter_job(<job_id>, scheduled => true, next_start => now() + interval '1 minute')`. 재개 뒤 또 실패하면 실패 카운터가 안 지워져 바로 다시 Paused. 마지막 시도는 첫 실패 후 약 1시간 50분이라 08:50 KST 한참 전이다. 런북에 이 한 줄과 상태 조회(`timescaledb_information.jobs`, `job_stats`의 `job_status`·`last_run_status`, `job_errors`)를 넣을 것(Main 승인 사항).
- 백업 cron(`/etc/cron.d/kasset-db-backup`) 18:30 KST, dart 18:30, symbol-master 22:00과 겹치지 않는다.
- 첫 실행(2026-10-07 02:30 KST)은 7일 지난 청크 15·21·25·27만 압축한다(청크 34는 10/08 02:30부터). 9/30·10/06 복구는 그 전에 비압축 청크에서 끝난다.

## 6. 끝난 검증 (명령·exit·결과)

모두 서버 `kasset-server`, 배포 이미지 `kasset-trader-core:48c3a57a874562d10b4bf5a7c2a81aa48c922d2d`(=origin/main 48c3a57, 운영 실행 중).

| # | 명령(요약) | exit | 결과 |
|---|---|---|---|
| 1 | 운영 DB `COPY ... TO STDOUT`(읽기 전용) -> 일회용 DB `COPY FROM STDIN`, `compress_chunk`, `chunk_compression_stats` | 0 | 2절 표 |
| 2 | 일회용 DB에서 DML 시나리오 SQL(`/tmp/probe_dml.sql`, 서버 임시 파일) | 0 | 3절 표 |
| 3 | `/tmp/probe_bench.sql`을 phase별(압축·ANALYZE 후·복원)로 3회 | 0 | 4절 표 |
| 4 | `bash /tmp/mig_roundtrip.sh toss_mig_probe_20261006`: alembic stamp 부모 -> upgrade head -> `CALL run_job` -> downgrade -1 -> upgrade head (2청크 1,402,083행) | 0 | upgrade 직후 `compressed_chunks 0/2`(동기 압축 없음), 정책 id 1000 `next_start 2026-10-07 02:30 KST`, `max_retries 3`, `retry_period 00:30:00`, `symbol/time_utc DESC`. 정책 실행 3.5초에 2/2 압축. downgrade 29초, 정책 제거·`compression_enabled=false`·0/2·행 해시 동일(8872373523103970555160). 재upgrade 성공. 원문 `evidence/migration-roundtrip-timescale.txt` |
| 5 | `bash /tmp/mig_plain.sh`, `/tmp/mig_plain2.sh`: 확장 없는 DB(template0), 일반 표 DB, 확장은 있으나 hypertable 아닌 DB 각각 downgrade/upgrade | 0 | 모두 NOTICE 후 no-op으로 통과(`alembic_version` 갱신 확인, 정책 job 0개). 원문 `evidence/migration-roundtrip-plain.txt` |
| 6 | `bash -n /tmp/deploy_prune_dryrun_src.sh`(서버, LF 변환본) | 0 | `bash -n OK` |
| 7 | `uvx --from shellcheck-py shellcheck -s bash <LF 변환 deploy.sh>`(shellcheck 0.11.0, 로컬) | 0 | 지적 0건 |
| 8 | `/tmp/prune_dryrun_harness.sh`(서버): deploy.sh의 정리 함수만 `source`, `docker rmi`/`builder`만 가로채고 나머지는 읽기 전용으로 실행 | 0 | 이미지 4개(48c3a57 실행 중, f1d8bf4, 90e7073, 588672d). 다음 배포 시뮬레이션(target=새 SHA, 직전=48c3a57)은 90e7073·588672d 삭제 후보, 실행 중 48c3a57과 f1d8bf4 유지. KEEP=1에서도 실행 중 48c3a57은 "컨테이너가 쓰는 ...는 건너뜀". 빌드 캐시는 `docker builder prune -f --keep-storage 3GB`. 원문 `evidence/deploy-prune-dryrun.txt` |
| 9 | 서버 `alembic heads` | 0 | `20261006_toss_minute_compression (head)` 한 개 |
| 10 | 서버 `run_server_pytest kasset-pytest-toss-comp tests/services/research_candles/test_toss_minute_compression_migration.py tests/services/paper_cohort/test_migration.py::test_real_postgresql_upgrade_downgrade_upgrade_single_head -rs` (deps 볼륨 `kasset-pytest-deps-48c3a57a`, 체크아웃 `/tmp/kasset-toss-comp-src`, 48c3a57 + 변경 파일) | 0 | `2 passed in 44.25s`, skip 없음. 가드: `ROB-1296 ... 0 blocked requests`, `ROB-1880 socket guard: active=True blocked_attempts=0`, `test schema bootstrap: databases=1 applied=1`. 원문 `evidence/server-pytest-ruff-ty.txt` |
| 11 | 서버 `ruff check --no-cache` / `ruff format --no-cache --check` / `ty check --error-on-warning` (새 마이그레이션 + 새 테스트 2파일) | 0 / 0 / 0 | `All checks passed!` / `2 files already formatted` / `All checks passed!` |
| 12 | 운영 격리 확인: `psql -U kasset -d kasset -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"` | 0 | 110(운영 DB 테이블 수 유지), 컨테이너 목록 9줄 |

실제 삭제(`docker rmi`, `builder prune`)는 한 번도 실행하지 않았다. `docker system df`: 이미지 17.75GB(10.16GB 회수 가능), 빌드 캐시 7.49GB 전부 회수 가능.

## 7. 생략·미측정 (Main이 범위를 필수 검증으로 줄임)

필수 검증(1. 격리 TimescaleDB alembic upgrade -> downgrade -> upgrade, 2. 서버 pytest + Ruff·ty, 3. `bash -n`과 운영 docker 읽기 전용 dry-run)은 모두 위 6절에 통과로 기록했다. 아래는 하지 않았다.

- cagg EXPLAIN ANALYZE 원문과 운영 규모(청크 6개, 행 8배) 조회 측정: 격리 표본(12.5% 규모) 중앙값만 있다(4절). 런북에는 "미측정"으로 적었다.
- `tests/research/toss_phase2/test_migration.py`, `tests/services/order_proposals/callback_inbox/test_migration_chain.py`, `tests/services/research_candles` 전체, `python3 -m scripts.ci.file_shard_plan check`: 실행하지 않았다(shard-3 정렬·중복만 확인).
- 확장 없는 PostgreSQL에서 새 테스트가 skip되는지는 CI(plain PG15)에서 처음 확인된다. 서버에서는 skip 없이 실행됐다. 마이그레이션 no-op 경로는 확장 없는 DB(template0)와 일반 표 DB로 6절 4~5번에서 확인했다.
- `docker builder prune --keep-storage 3GB` 실행은 하지 않았고 `--help`로 플래그만 확인했다(buildx 0.14.0, docker 26.1.3).
- 6절 4번(격리 alembic round trip)은 일회용 DB를 지우기 전에 한 번 통과했다. 새 테스트(6절 10번)도 같은 순서를 실제 hypertable에서 다시 돌린다.

이어서 서버 pytest를 다시 돌릴 때: 체크아웃 `/tmp/kasset-toss-comp-src`(48c3a57 + 변경 파일 3개 LF), deps 볼륨 `kasset-pytest-deps-48c3a57a`(uv.lock 핀과 일치 확인 완료, 기존 `kasset-pytest-deps-4e6329d1`은 핀이 달라 안 씀). 서버 HEAD가 바뀌면 체크아웃·이미지·볼륨을 새 SHA로 다시 만든다. 스크립트는 서버 `/tmp/run_pytest_toss.sh`.

## 8. 남은 위험·주의

- 정책 job이 연속 3번 실패하면 Paused 상태로 남고 아무도 모른다(5절). 알림이 없으니 런북 상태 조회가 유일한 확인 수단이다.
- downgrade는 전체를 decompress한다(현재 6청크 약 3천만 행, 약 59k 행/초면 10분 안팎, 여유 디스크 약 15GB 필요). 코드 변경 없이 런북 경고로만 남기기로 Main이 정했다.
- 대량 복구를 한 트랜잭션으로 하면 10만 튜플 한도(53400)에 걸린다. 복구 CLI는 종목별 커밋이라 안전하다(종목당 약 55ms). 롤백된 대량 DML은 죽은 튜플로 압축 청크를 부풀린다.
- 신규 테스트는 서버에서 `2 passed`(6절 10번)로 처음 돌려 통과했다. CI plain PG15에서는 skip된다.
- 첫 정책 실행(2026-10-07 02:30 KST)은 청크 4개(약 11.5GB)를 압축하며 디스크 여유가 충분하다(운영 디스크 99GB 중 40GB 여유).
- 실수 한 건: 테스트 파일을 한 번 `python -`으로 일괄 치환해 편집했다(`edit` 도구 규칙 위반). 결과는 읽어서 확인했고 내용은 의도와 같다.

## 9. 일회용 자원 정리

- 격리 DB의 일회용 DB 3개(`toss_comp_probe_20261006`, `toss_mig_probe_20261006`, `toss_mig_probe_plain_20261006`)는 `DROP DATABASE ... WITH (FORCE)`로 지웠고 `toss_%` DB가 남아 있지 않음을 확인했다. pytest가 만든 run-owned DB는 하네스가 지웠다.
- 남아 있는 것: 서버 `/tmp/kasset-toss-comp-src`(체크아웃), 서버 `/tmp/*.sh`·`/tmp/*.sql` 임시 스크립트, 도커 볼륨 `kasset-pytest-deps-48c3a57a`(다음 pytest 재사용용), 로컬 `%TEMP%`의 스크립트. 운영 DB·운영 컨테이너에는 쓰지 않았다.

## 10. Lesson 후보

- MSYS bash에서 `python3`로 `/tmp/...`를 쓰면 Windows 경로(`C:\tmp`)로 가서 `scp`가 다른 파일을 올린다. 파일을 로컬에 만들지 말고 `ssh host 'cat > /tmp/x' <<'EOF'` 또는 stdin 파이프로 서버에 직접 쓴다(증거: 첫 `CREATE MATERIALIZED VIEW ... relation does not exist` 실패 -> stdin 파이프로 성공).
- 이 저장소 Settings는 alembic 실행에도 `SECRET_KEY`(대/소문자·숫자 포함)·`OPENDART_API_KEY`가 필요하다. `scripts/setup-test-env.sh`의 더미 값을 쓴다.
- TimescaleDB `max_retries`는 상한 도달 시 job을 Paused로 둔다(다음 날 재시도 아님).
