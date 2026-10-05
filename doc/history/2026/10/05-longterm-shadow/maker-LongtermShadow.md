# Maker 기록 — 장기 추세·재무성장 SHADOW 구현

RECORD: maker-LongtermShadow
DATE: 2026-10-05
SCOPE: longterm shadow, 장기 추세, 12-1 모멘텀, 재무성장, 주간 코호트, 벤치마크, 초과수익, KASSET_LONGTERM_SHADOW_ENABLED
PATHS: app/extensions/kasset/automation/longterm_shadow.py, app/extensions/kasset/automation/longterm_shadow_service.py, app/extensions/kasset/automation/swing_shadow.py, app/extensions/kasset/automation/swing_shadow_service.py, app/models/kasset_longterm_shadow.py, alembic/versions/20261005_kasset_longterm_shadow.py, scripts/kasset_longterm_shadow.py, docs/runbooks/kasset-longterm-shadow.md
STATUS: accepted (Main 검수 대기; 운영 활성화·마이그레이션 적용·커밋은 하지 않음)

## 무엇을 바꿨나

- 신규: 순수 판정(`longterm_shadow.py`), DB 서비스(`longterm_shadow_service.py`), ORM(`kasset_longterm_shadow.py`),
  alembic `20261005_kasset_longterm_shadow`(down_revision `20261003_kasset_swing_shadow`, additive 새 테이블 2개),
  CLI `scripts/kasset_longterm_shadow.py`, 런북, 테스트 2파일, 플래그 `KASSET_LONGTERM_SHADOW_ENABLED`(기본 False).
- 스윙 공유화(로직 동일): `swing_shadow.screen_bars`(품질 제외 추출, `min_history_sessions` 선택 인자),
  `BarScreenConfig`/`OutcomeConfig` Protocol, `evaluate_outcome` 설정 타입 일반화.
  `swing_shadow_service`는 `_load_bars` 등 7개 helper를 이름만 공개화하고
  `session_coverage`에 후보 키 인자와 `not_applicable` run 상태 처리를 추가했다(스윙은 그 상태를 만들지 않는다).
- Main 승인(체크포인트)으로 OWNED_PATHS 밖 6파일을 #112 선례대로 한 줄씩 수정:
  `app/models/__init__.py`, `tests/_schema_bootstrap.py`(57→58),
  `tests/extensions/kasset/test_multi_user_migration_guards.py`,
  `tests/services/order_proposals/callback_inbox/test_migration_chain.py`,
  `tests/services/paper_cohort/test_migration.py`, `tests/services/paper_evaluation/test_migration.py`,
  `tests/unit/tasks/test_daily_candles_tasks.py`(태스크 연결 테스트).

## 판단 근거

- 운영 시간 금지구간: `docs/runbooks/server-pytest-runner.md`는 "Do not run this procedure on **KRX business days**
  from 08:50 through 16:20 KST"라고 적는다. 2026-10-05는 개천절 대체공휴일이라 KRX 영업일이 아니고(관측
  `last_final_session_kr`=2026-10-02, Main이 운영 사이클 `no_regular_market_open` 확인) 문언이 영업일에 한정하므로
  11:3x KST에 서버 검증을 실행했다(`uptime` load 0.22, 컨테이너 `--cpus=1.0`, 운영 DB는 읽기 전용 `\copy`만).
- PIT: `filing_date <= S`(Main 승인). `fundamentals_stale`(최신 분기 period_end가 S보다 200일 초과 이전)는 config 필드
  `fundamentals_max_staleness_days`로 fingerprint에 포함, run 탈락 사유로 센다(Main 승인).
- 재무는 추세·모멘텀 통과 종목에만 읽는다(재무 사유 개수는 그 모집단 안의 분포).
- 동률 처리는 모멘텀 정확값(Decimal, 반올림 없음) 내림차순 → symbol 오름차순.
- 벤치마크 분모는 같은 세션 가장 먼저 완료된 run의 `evaluated_symbols`(재실행이 달라도 최초 관측 기준).

## 실제 smoke에서 발견·수정한 결함

- 첫 smoke report가 모든 코호트를 `calendar_unavailable`로 냈다. 원인: forward 세션 조회 끝을 `h×3+20`일(120일 →
  380일)로 잡아 거래소 calendar 범위(현재+약 1년)를 넘으면 `trading_sessions_in_range`가 구간 전체를 빈 값으로
  돌려준다(fail-closed). 스윙은 최대 10거래일이라 50일이라 안 드러났다. `_forward_sessions`로 `h×7//5+45`일 창을
  쓰게 고쳤고 회귀 테스트(`test_longest_horizon_sessions_are_available_right_after_a_recent_signal_session`)를 추가했다.
  수정 뒤 같은 smoke DB report는 20/60/120 모두 `pending`(정상)이다.

## 검증 (서버 격리 러너, cwd=`/tmp/kasset-longterm-shadow-20261005-5e7c1a3d`, 이미지 `kasset-trader-core:b22c1eac…`, deps 볼륨 `kasset-pytest-deps-04d62828-swing`)

| 검사 | 결과 | 증거 |
|---|---|---|
| ruff check / ruff format --check (변경 .py 18개) | exit 0 / exit 0 (최종 코드 기준) | `evidence/static-final.log`, 포맷 적용분 `evidence/ruff-format-applied.diff` |
| ty check --error-on-warning app/ | exit 0 | `evidence/static-final.log` |
| pytest 신규 2 + 스윙 2 + 태스크 | `91 passed`, PYTEST_EXIT=0 | `evidence/pytest-final.log` |
| pytest 마이그레이션·가드 6파일 | `24 passed`, PYTEST_EXIT=0 | (러너 stdout, 아래 참고) |
| alembic upgrade→downgrade→upgrade | 모두 exit 0, 신설 테이블 2개 생성→0→재생성, 제약 24개, head 1개 | `evidence/alembic-roundtrip.log` |
| 스윙 fingerprint 불변 | 기준 b22c1eac와 수정 코드 모두 `a501328c…f627` | `evidence/swing-invariance.log` |
| 스윙 observe 결과 불변 | 같은 smoke DB에서 기준·수정 코드 JSON이 run 메타·inserted/duplicates 제외 동일(885 평가, 신호 0/1/2) | `evidence/swing-invariance.log` |

기존 스윙 테스트(`test_swing_shadow.py`, `test_swing_shadow_service.py`)는 수정 없이 통과했다.

## smoke (운영 시장자료 복사본, 격리 DB `longterm_smoke_20261005_5e7c1a3d`)

복사: 유니버스 3,957 / 일봉 744,063(2025-08-01~2026-10-02) / 재무 47,232(kr 분기). 운영 DB 쓰기 0.
`observe`(실제 시계 2026-10-05 11:44 KST → S=2026-10-02 금, 주 마지막 거래일) → completed, runId 1.

- universe 2,478, 평가 852, 제외 `below_min_turnover` 1,451 / `insufficient_history` 84 / `below_min_close` 52 /
  `halted_suspect` 23 / `price_discontinuity` 15 / `stale_latest_bar` 1.
- `trend_momentum`: 추세·모멘텀 통과 178 → 상위 20 저장. 탈락: `below_sma200` 508, `sma50_not_above_sma200` 107,
  `sma200_not_rising` 50, `momentum_not_positive` 9, `outside_top_n` 158.
- `quality_growth_trend`: 재무 평가 대상 178 중 통과 44 → 상위 20 저장(`outside_top_n` 24). 재무 탈락:
  `fundamentals_stale` 36, `fundamentals_revenue_growth_below_minimum` 24, `fundamentals_net_income_growth_below_minimum` 22,
  `fundamentals_ttm_net_income_not_positive` 18, `fundamentals_prior_ttm_net_income_not_positive` 16,
  `fundamentals_missing` 6, `fundamentals_quarter_gap` 6, `fundamentals_discrete_missing` 5,
  `fundamentals_insufficient_quarters` 1.
- 재실행(runId 2): inserted 0 / duplicates 20·20.
- report(`--since 2026-10-02 --cohorts`): coverage `evaluated_with_signals` 1, missing 0, 코호트·벤치마크(852종목) 모두 h=20/60/120 `pending`,
  `sampleAdvisory=insufficient_sample`.
- 선정 예(근거는 `evidence/smoke-selected-signals.log`): `trend_momentum` 1위 010170 대한광통신(12-1 7.5048, 종가 17,800, SMA50 13,405.8,
  SMA200 11,230.675 > 20세션 전 9,863.835), 2위 009150 삼성전기(6.3045), 3위 000500 가온전선(5.1451).
  `quality_growth_trend` 1위 009150 삼성전기(최신 분기 2026Q2 공시 2026-08-14, TTM 순이익 922,054,381,757 vs 전년 659,075,394,396 = +39.9%,
  TTM 매출 +14.2%), 2위 000500 가온전선(+82.6% / +41.4%), 3위 131290 티에스이(+89.9% / +28.3%).

## 한계·미검증

- 성숙(mature) 코호트의 실데이터 값은 아직 없다(S=2026-10-02는 20거래일이 지나야 mature). mature 경로의 집계·초과수익은 통합 테스트(합성 일봉)로만 검증했다.
- 12-1 모멘텀 7.5배(대한광통신)처럼 큰 값은 수정주가 부재(유상증자·액면 이벤트가 35% 일간 한도 안에 숨을 수 있음)와 구분하지 못한다.
- `fundamentals_stale` 36개는 smoke 기준 시점의 순환 갱신 지연분이다(재무 2026Q2 보유 종목이 일부).
- 운영 마이그레이션 적용·플래그 활성화·배포는 하지 않았다.

## 격리 확인

아래 `isolation` 결과 참조(검증 DB·임시 디렉터리 정리 포함).

운영 DB `kasset-trader-db-1`에는 longterm 테이블 0개(`information_schema` 조회 0), 스윙 테이블 2개 그대로,
public 테이블 110개(런북 기록 109 이후 다른 변경분이며 이 작업은 운영 DB 쓰기가 없다). 내 검증 DB
(`longterm_smoke_20261005_5e7c1a3d`, `longterm_alembic_20261005_5e7c1a3d`)와 `/tmp/kasset-longterm-shadow-*`
디렉터리 3개(소스·base clone·logs)는 정리했다(`evidence/isolation-cleanup.log`). `kasset-test-db`에 남은
`test_db_pytest_*` 7개는 생성일이 2026-08-31~09-06이라 이전 작업의 잔재이며 건드리지 않았다.

## 환경상 실패(이 변경과 무관)

`tests/ci` 실행 시 `test_cli_*` 9건이 setup에서 `FileNotFoundError: 'git'`로 에러였다. 배포 이미지에 git 바이너리가
없기 때문이며 GitHub Actions에서 확인해야 한다. 같은 실행의 나머지 `tests/ci` 판정은 에러 요약에 포함되지 않아
성공 개수는 확인하지 못했다(`-k 'not test_cli_'`로도 fixture 에러가 남아 요약을 얻지 못함).
