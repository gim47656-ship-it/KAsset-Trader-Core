# maker-SwingShadow — KRX 스윙 SHADOW 3후보 관측·2주 성과 조회

RECORD: maker-SwingShadow
DATE: 2026-10-03
SCOPE: swing shadow, weekly compression breakout, uptrend first pullback, box breakout retest, forward observation, virtual next-open outcome, report coverage, fill quality, KASSET_SWING_SHADOW_ENABLED
PATHS: app/extensions/kasset/automation/swing_shadow.py, app/extensions/kasset/automation/swing_shadow_service.py, app/models/kasset_swing_shadow.py, alembic/versions/20261003_kasset_swing_shadow.py, scripts/kasset_swing_shadow.py, app/tasks/daily_candles_tasks.py, app/core/config.py, app/services/daily_candles/sync_service.py, docs/runbooks/kasset-swing-shadow.md
STATUS: partial

구현과 격리 검증은 끝났다. 실제 전향 관찰은 운영 마이그레이션·배포·`KASSET_SWING_SHADOW_ENABLED`
활성화(사용자 승인 대상) 뒤에 시작되므로 지금까지 0건이다. 그래서 `partial`이다.

## 최종 revision: SwingShadow-r3

- frozen full diff: `evidence/SwingShadow-r3.diff` (3,713줄, sha256 c0c3c28b5ea879ee314350f377c4823b3a5028be398b9398540f75e4b27b7641, base f25b0858)
- r2→r3 delta: `evidence/SwingShadow-r2-to-r3.delta` (sha256 eeeffa39…925a). 변경 파일 5개:
  swing_shadow.py, swing_shadow_service.py, test_swing_shadow.py, test_swing_shadow_service.py,
  docs/runbooks/kasset-swing-shadow.md. 새 파일 없음, migration·모델·태스크·설정 무변경.
- 이전: r1 `evidence/SwingShadow-r1.diff`, r2 `evidence/SwingShadow-r2.diff`(2753dc5a…6ebb).

## 바꾼 것

- 순수 판정·성과 `app/extensions/kasset/automation/swing_shadow.py`: 후보 3종 v1 규칙(조건은
  `docs/runbooks/kasset-swing-shadow.md`)과 다음 세션 시가 가상 진입 성과(1/3/5/10거래일,
  진입일=1일째, 매수 0.015%·매도 0.015%+세금 0.18%·편도 슬리피지 0.1%, MFE/MAE).
  First Pullback은 `shadow_setups.evaluate_shadow_setups`의 `confirmed`를 그대로 쓰고 첫 접촉·
  상승추세 조건과 접촉 묶음 잘림 가드만 더했다. 비용 값은 `FEE_RATES`를 import하지 않고
  설정에 같은 값을 둔다(`paper_trading_service`가 upbit client를 import하는 부작용 회피).
- DB 경계 `swing_shadow_service.py`, 모델 `app/models/kasset_swing_shadow.py`, additive migration
  `20261003_kasset_swing_shadow`(down `20260926_symbol_master_adr`). runs는 실행마다 append되며
  분모·미관측 세션·거부/실패 사유를 담는다. signals는 UNIQUE(candidate, config_fingerprint,
  symbol, anchor_session_date) `ON CONFLICT DO NOTHING`로 저장하고 관측 시점 신호봉을 보존한다.
- 실행 연결 `app/tasks/daily_candles_tasks.py`: 기존 `candles.daily.kr.sync`가 `status=ok`이고
  `KASSET_SWING_SHADOW_ENABLED`(기본 false, `app/core/config.py`)일 때만 한 번 이어 실행한다.
  observer 실패는 결과의 `swing_shadow` 항목과 `failed` run으로 남는다. 새 예약은 없다.
- CLI `scripts/kasset_swing_shadow.py observe|report`.
- `sync_service.resolve_backfill_targets`의 KR 보통주 SQL을 `KR_COMMON_SHARE_UNIVERSE_SQL` 상수로
  추출했다. 조건·정렬 문자열은 원본과 같다.
- 마이그레이션 관례: bootstrap v57, post-parent/boundary 목록 4곳, shard 3·4 등록.

### r3 보완 (Main finding SHADOW-COVERAGE, SHADOW-FILL-QUALITY)

- SHADOW-FILL-QUALITY: 운영 `kr_candles_1d` 제약을 확인했다. NOT NULL·venue CHECK·UNIQUE만
  있고 가격 양수·OHLC 정합 CHECK는 없다. 운영 220일 KRX 구간 실측은 가격 0 이하 0건, OHLC
  역전 0건, 거래량 0인 봉 8,144건이다. 그래서 성과 계산 전에 진입~청산 봉을 검사한다. 검사에
  실패하면 `invalid_bar`, 진입 세션 거래량 0은 `entry_untradable`, 청산 세션 거래량 0은
  `exit_untradable`로 둔다. 중간 보유일 거래량 0은 mature로 남기고 `zeroVolumeHoldingSessions`
  /`matureWithZeroVolumeHolding`으로 센다. 관측 판정의 OHLC 검사도 같은 `_bar_is_valid`를 쓴다.
- SHADOW-COVERAGE: 현재 설정 cohort는 run·신호가 0이어도 항상 나온다. 세션별 상태는
  `not_observed`·`observation_failed`·`not_evaluated`·`evaluated_no_signal`·
  `evaluated_with_signals`로 나눈다. 같은 날 재실행은 합산하지 않고 마지막 completed run 하나의
  universe/evaluated/exclusions/후보별 noSignalReasons·notApplicable·signalsObserved와
  storedNewSignals를 노출한다.

## 검증 (kasset-prod 격리 checkout `/tmp/kasset-swing-shadow-20261003-01a0fead`, 이미지 `04d62828`, 1CPU)

| 검사 | revision | 결과 | 원문 |
|---|---|---|---|
| ruff/format/ty 변경 .py 17개 | r1→r2 | r1 F401·포맷 5 → 수정 후 0/0/0 | evidence/r2-static-unit.txt, r2-final-static.log |
| focused pytest 5파일 | r2 | 87 passed exit 0 | evidence/r2-final-pytest.log |
| DB 통합 + migration 4종 pytest | r2 | 15 passed exit 0 | evidence/r2-db-pytest.log |
| 고유 DB alembic stamp→downgrade -1→upgrade head | r2 | 각 exit 0, 테이블 2·제약 21개(ck 17·pk 2·fk 1·uq 1), DB DROP | evidence/r2-alembic-roundtrip.log |
| 전략 fingerprint 대상 17파일 diff | r2(r3도 해당 파일 무변경) | 출력 없음 exit 0 | evidence/r2-static-unit.txt |
| 스냅샷 observe 2회 + report | r2 | 신호 3, 2회째 신규 0·중복 3 | evidence/r2-smoke-*.log, smoke-snapshot-prep.txt, run.sh |
| ruff/format/ty r3 변경 .py 4개 | r3 | 포맷 1건 수정 후 0/0/0 | evidence/rev3-static.log |
| focused pytest 4파일 | r3 | 74 passed exit 0 | evidence/rev3-pytest.log |
| 스냅샷 재구성 observe 1회 + report --since 2026-09-28 | r3 | OBSERVE 0, REPORT 0 | evidence/rev3-smoke-prep.log, rev3-smoke-observe.log, rev3-smoke-report.log |
| 운영 격리 | r2·r3 | 운영 review 스윙 테이블 0, 테스트 swing DB 0, 컨테이너 9개 Up | evidence/r2-isolation-check.log, rev3-isolation-check.log |

r2 결과를 재사용한 근거: r3는 migration·모델·태스크·config·sync_service·bootstrap·shard를 바꾸지
않았다. 그래서 migration 4종, alembic 왕복, task 연결 테스트 결과는 그대로 유효하다. r3 변경
범위(swing_shadow.py, swing_shadow_service.py와 그 두 테스트 파일)는 r3에서 다시 돌렸다.
test_shadow_setups·test_daily_candles_tasks도 함께 재실행했다. backfill 테스트는 r3에서
sync_service가 바뀌지 않아 제외했다.

r3 스냅샷 report(2026-10-03 토 10:31 KST, 운영 읽기 전용 복사 일봉 409,657행·유니버스 3,957행,
일회성, 검증 뒤 DB 삭제): 현재 설정 cohort의 9/28~10/1은 `not_observed`, 10/2는
`evaluated_with_signals`다(평가 885/보통주 2,478, 후보별 no-signal 사유 포함). 신호 3건은 전 horizon
`pending`이다. **이 스냅샷 결과는 일회성 검증이며 실제 전향 성과가 아니다.**

## 미검증·남은 경계

- 실제 전향 관찰: 운영 마이그레이션·배포·플래그 활성화(사용자 승인) 전에는 0건이다.
- `candles.daily.kr.sync`의 task status ok는 로그로 직접 관측하지 못했다. 적재 시각 정황만 있다.
- 수정주가 계수가 없어 35% 급변 제외로만 거른다. 거래가 있었던 날의 시가 상·하한가 잠김은
  구분하지 못한다. 유니버스는 관측 시점 정의다.
- 첫 눌림 가드 관련 r2 DM의 "005070에서 재신호 위험 관측"은 과장이었다. 실제로 본 것은 6주간
  이어진 긴 접촉 묶음이고, 가드는 lookback 시작에 닿는 경우를 막는다.
- 서버 `/tmp/kasset-swing-shadow-logs/`(원문 로그·run.sh·smoke-migration.sql)는 남겨 두었다.
