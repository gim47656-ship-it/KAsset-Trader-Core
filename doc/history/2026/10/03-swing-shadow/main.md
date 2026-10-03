# KRX 스윙 SHADOW 후보와 2주 성과 조회

RECORD:
DATE: 2026-10-03
SCOPE: 스윙 SHADOW, 주봉 압축 돌파, 첫 눌림, 박스 돌파 재지지, 전향 관측, 가상 성과
PATHS: app/extensions/kasset/automation/swing_shadow.py, app/extensions/kasset/automation/swing_shadow_service.py, app/models/kasset_swing_shadow.py, app/tasks/daily_candles_tasks.py, scripts/kasset_swing_shadow.py, docs/runbooks/kasset-swing-shadow.md
STATUS: partial

## 사용자 요구와 승인 경계

사용자는 단기 전략만 운영 중인 상태에서 관찰할 만한 스윙 SHADOW 후보 몇 가지를 넣으라고 요청했다. 성적 확인 시점은 처음 일주일에서 **2주 뒤**로 변경했다. 이는 구현을 2주 뒤로 미루라는 지시가 아니다. 알림·새 예약 등록 요청은 없었다.

Main은 완료 주봉 압축 돌파, 상승 추세 첫 눌림, 박스권 돌파 후 재지지 3후보를 선택했다. 재무 정정별 관측 이력이 부족한 실적 성장형은 제외했다. 기존 단기 전략·매매 지문·추천·주문·승격은 변경하지 않는다. 별도 PAPER 전략이나 수익성 입증이 아니라 비주문 관측이다.

별도 worktree `feat/swing-shadow-candidates`, base `f25b0858a9be21457d055dc7de6005140b6e8d6b`에서 작업했다. Opus Maker 1명이 세 후보·기록·성과 조회·격리 검증을 통합 소유했고 Main이 설계 승인·누적 검수·공통 검증 환경·설정 예시·공유 문서를 맡았다. 탐색에는 Gemini 보조 요약을 사용했다.

## Main이 승인한 설계

- 기존 평일 16:30 `candles.daily.kr.sync` 성공 뒤 `KASSET_SWING_SHADOW_ENABLED`(기본 false)를 확인해 관측한다. 새 스케줄은 없다. observer 실패는 일봉 수집 성공을 바꾸지 않으며 오류 근거를 남긴다.
- 실제 시각 기준 최신 완료 세션만 관측한다. 평가 시각은 해당 세션 정규장 종료, 기록 시각은 실제 wall clock이다. 다음 세션 시가를 안 뒤 기록하지 못하도록 다음 세션 장 시작 이후는 거부한다. CLI에는 과거 날짜 관측 옵션이 없다.
- 기존 첫 눌림 evaluator를 재사용하고 완성 주봉·거래일 달력·자료 공백을 구분한다. 정확한 후보 규칙은 [런북](../../../../../docs/runbooks/kasset-swing-shadow.md)이 정본이다.
- `review.kasset_swing_shadow_runs`는 실행·누락 분모, `review.kasset_swing_shadow_signals`는 최초 관측 신호와 가격 근거를 보존한다. 후보·설정·종목·anchor별 중복을 막는다. 기존 테이블은 변경하지 않는 additive migration이다.
- 성과는 다음 거래일 시가 가상 진입과 1/3/5/10거래일째 종가 가상 청산이다. 거래비용·슬리피지, MFE/MAE, 미성숙·자료 누락을 구분한다. 2주 달력 기간을 10거래일로 간주하지 않는다. 표본 부족은 연구 안내이지 매매 gate가 아니다.
- 원시 일봉은 재적재될 수 있어 관측 당시 신호봉을 보존한다. 현재 값이 바뀌면 정정 표본을 분리한다. 적재 시각만 바뀐 것을 가격 수정으로 세지 않는다.

## 검수와 실제 증거

Main은 [r2 frozen diff](evidence/SwingShadow-r2.diff)의 기존 일봉 태스크 연결, 보통주 SQL 추출의 동일 의미, 후보 규칙, 완료봉·다음 세션 시간 계약, 성과 산식, 신호 멱등키, additive migration을 직접 확인했다.

- [r2 집중 검사](evidence/r2-final-pytest.log): `87 passed`, `PYTEST_EXIT=0`. 초기 검증 로그의 `r3-pytest.log`는 재실행 번호였으므로 source revision 혼동을 없애도록 이 이름으로 정리했다.
- [DB·마이그레이션 검사](evidence/r2-db-pytest.log): `15 passed`, `PYTEST_EXIT=0`.
- [마이그레이션 round-trip](evidence/r2-alembic-roundtrip.log): 격리 DB create_all→stamp head→downgrade -1→upgrade head, 각 exit 0. downgrade 뒤 신설 테이블 0개, upgrade 뒤 2개와 제약 생성 확인. 운영 DB에 실행하지 않았다.
- [r2 시장자료 스냅샷 두 번째 관측](evidence/r2-smoke-observe-2.log): 보통주 2,478개 중 885개 평가, 주봉 2건·첫 눌림 1건·박스 재지지 0건. 재실행 신규 신호 0건, 중복 3건, `OBSERVE_EXIT=0`. 이는 운영 시장자료를 격리 DB에 복사한 기능 smoke이지 실제 전향 관측 성적이 아니다.

Main은 r2를 그대로 수용하지 않고 다음 두 finding을 같은 owner에게 보냈다.

1. `SHADOW-COVERAGE`: run이 없거나 모든 종목이 자료 부족으로 제외된 날을 정상 평가 후 신호가 없는 날과 구분하도록 보고서 커버리지 보완.
2. `SHADOW-FILL-QUALITY`: 원시 후행봉의 DB 보장 범위를 확인하고, 보장되지 않는 잘못된 OHLCV 및 거래량 0인 진입·청산일을 정상 성과와 분리.

첫 눌림의 긴 접촉 구간은 실제 스냅샷에서 확인했지만 중복 재신호 자체를 운영에서 관측한 것은 아니다. lookback 경계에 걸릴 때 anchor가 이동할 위험을 focused regression으로 방어한 것으로 한정한다.

## r3 최종 구현 수용

Main이 [r2→r3 delta](evidence/SwingShadow-r2-to-r3.delta)를 직접 검수해 두 finding을 닫았다. [전체 frozen diff](evidence/SwingShadow-r3.diff)의 변경은 순수 판정·성과, 서비스, 그 테스트 2개, 런북에 한정된다. migration·모델·기존 태스크 연결·설정·일봉 SQL은 r2와 같아 그 검증을 재사용했다.

- [r3 집중 검사](evidence/rev3-pytest.log): 4파일 `74 passed`, `PYTEST_EXIT=0`.
- [r3 정적 검사](evidence/rev3-static.log): Ruff·format·ty 모두 exit 0.
- [r3 실제 report smoke](evidence/rev3-smoke-report.log): 9/28~10/1은 `not_observed`, 10/2는 평가 885/대상 2,478의 `evaluated_with_signals`로 구분했다. 신호 3건의 모든 horizon은 `pending`이었다. 다음 진입 세션은 10/6, 10거래일째는 10/20이므로 달력 2주 뒤에도 10거래일 성과가 미성숙할 수 있다.
- run이 없는 조회도 현재 설정의 세션별 커버리지를 출력한다. 재실행의 평가 종목 수는 합산하지 않는다. 정상 무신호·관측 실패·평가 불가가 구분된다.
- 운영 원시 일봉은 가격 양수·OHLC 정합을 DB에서 보장하지 않으며 거래량 0인 봉이 존재한다. 후행봉 이상은 `invalid_bar`, 진입·청산일 거래량 0은 `entry_untradable`·`exit_untradable`로 구분한다. 중간 보유일 거래량 0은 별도 개수와 함께 평가값으로 유지한다.
- 검증용 DB는 정리됐고 [격리 확인](evidence/rev3-isolation-check.log)에서 운영 SHADOW 테이블은 0개였다. 실제 운영에 기록을 쓰지 않았다.

구현·격리 검증은 수용한다. GitHub CI는 이 변경 PR에서 확인하며, 운영 활성화 전 상태는 아래와 같다.

## 운영 반영 전 남은 경계

현재 운영 전향 관측은 0건이다. 신규 테이블 migration·코드 배포·플래그 활성화는 정확한 revision에 대해 사용자 승인 후 진행한다. 이 저장소의 migration 변경은 자동 배포가 멈추므로 DB 백업과 `allow_migration=true` 수동 승인 절차를 따른다. 2주 뒤 조회하더라도 아직 성숙하지 않은 신호는 별도로 표시하며, 초기 성과를 전략 우위의 확정 근거로 사용하지 않는다.
