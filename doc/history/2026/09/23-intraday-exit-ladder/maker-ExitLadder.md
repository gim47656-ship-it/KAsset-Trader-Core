# 장중 익절 소수 보유·본전 바닥 및 추가 보호 arm 조사
RECORD: maker-ExitLadder
DATE: 2026-09-23
SCOPE: PAPER KRX 장중 부분익절 수량, SUCCEEDED 이후 본전 보호선, 5분봉 장중 추가 익절 백테스트
PATHS: app/extensions/kasset/automation/position_manager_service.py, tests/extensions/kasset/automation/test_position_manager.py, doc/history/2026/09/23-intraday-exit-ladder/evidence/
STATUS: partial

## 판단과 변경
- 운영 관측(005940 3주에서 30% ROUND_DOWN → 0주; 기존 3건 부분익절 후 손절선 -3ATR 유지)과 코드의 `_quantity_for_signal` → `quantity<=0` 무신호, `SUCCEEDED` → `partial_exit_completed=True`만 저장되는 경로를 대조했다.
- KRX는 2주 이상에서 `max(1주, 내림(보유×부분익절비율))`, 최대 보유-1주로 제한한다. 1주일 때 +0.5 ATR을 관측하면 **팔지 않고** 해당 완료 장중 bucket의 종료 시각부터 본전 바닥으로 올린다. `partial_exit_completed`는 계속 false이며 매도 추천/확정손익을 만들지 않는다. 다음 bucket에서 그 바닥이 깨지면 기존 전량 손절 추천을 생성한다. 이미 손절선이 바닥 이상이면 history도 늘리지 않는다. US의 0.0001 quantum은 보존했다.
- 실제 PARTIAL_SELL 추천이 `SUCCEEDED`면 동일 관리 tick의 `now`를 activation으로 삼아 `entry_price + post_partial_floor_atr×initial_atr`까지 잔량 `current_stop`을 올린다. `_raise_trailing_stop`으로 과거 exit-level version을 보존하고, 기존 손절선이 더 높으면 그대로 둔다. 일봉 `last_evaluated_at`과 초기 -3ATR은 그대로다.
- [장중 arm 비교](evidence/intraday-arm-comparison.md): 100종목 17영업일의 5분봉으로 HEAD/b/c(1·1.5·2·2.5·3 ATR)/d를 같은 고정 진입 사이클에 비교. 11 진입 중 4개를 불완전한 분봉으로 제외한 **7개 유효 사이클**뿐이다. b 총수익 +1.588%, MDD 0.701%; c k1은 총수익 +0.943%로 낮고 완료 사이클 기대값 +2.674%로 높아 우열을 결정하지 못한다. 8개 legacy 분봉 집계 뷰는 비었고 Toss 집계 4개는 동일한 1m 원본(코호트 실질 2026-09-01~23)이라 3개월 이력 부재. **3번 성과 결론은 미확인**, 본 요청에서는 추가 추격/2차 익절 코드를 넣지 않고 b(1·2 수정)만 출고한다. 다시 선택하려면 동일 코호트의 3개월 이상 장중 자료와 결측을 제외한 30개 이상 완료 사이클이 필요하다.
- `strategy_artifact.py:54-70`의 fingerprint는 `position_manager.py`와 기본 config를 포함하지만 이번 변경 대상인 `position_manager_service.py`는 포함하지 않는다. 따라서 version/fingerprint는 이번 수정으로 달라지지 않으며 기존 promotion 게이트를 완화·변경하지 않았다(Main 조향 동의). 향후 c/d로 `position_manager.py`를 변경하면 fingerprint mismatch와 재승격이 필요하므로 그때 별도 조향을 받는다. 새 DB 컬럼·마이그레이션은 없다.

## 수용 조건별 연결 (revision R1: 아래 두 코드 파일 포맷 후 수정본)
원본 frozen delta: [exit-ladder-R1.diff](evidence/exit-ladder-R1.diff) (tracked 두 코드 파일의 HEAD 대비 정확한 차이). 영향 호출부는 `position_manager_service.py:378-452`의 `run_owner`, `:498-852`의 `_manage_position`, `position_manager.py:347-412`의 provenance 전이, `:705-803`의 장중 evaluator, `strategy_promotion_service.py:499-520`의 런타임 fingerprint 대조다.

1. 소수 보유: **충족, 서버 격리 검증**. 2주→1주 PARTIAL 추천, 1주→추천 없이 바닥 활성화·반복 no-op·다음 bucket 본전 청산, US 소수점 quantum을 [검증 원문](evidence/server-validation.txt) 67개 focused 테스트에서 확인. `test_small_kr_partial_sells_one_share_without_liquidating_two`, `test_one_kr_share_arms_floor_once_then_exits_only_on_later_bucket`, `test_us_fractional_exit_keeps_its_existing_quantum`.
2. 부분익절 직후 본전: **충족, 서버 격리 검증**. `SUCCEEDED`를 읽은 tick의 now부터 current_stop=100, 이전 bucket의 70 보존, 이후 bucket에 100 손절, 재평가 history 중복·기존 높은 stop 하향 없음. `test_succeeded_partial_raises_floor_at_reconciliation_tick_not_past_bucket`, `test_succeeded_partial_never_lowers_an_existing_higher_stop` 및 기존 delayed replay·CLAIMED 테스트. [검증 원문](evidence/server-validation.txt).
3. 추가 장중 익절/추격선: **선택 근거 미확인**, 출고하지 않음(Main retarget). 5분봉·기존/수정/추격/2차 arm 비교의 raw 출력 `artifact://133`와 [결과표](evidence/intraday-arm-comparison.md). 유효 완료 사이클 최대 7개, 3개월 이상 장중 이력 없음. 표본 확보 후 재실행 필요.
4. 검증/CI: 코드·테스트의 서버 isolated runner `67 passed`, scoped Ruff/ty 통과. 새 테스트 파일 없이 기존 파일에 추가했으므로 `ci_shards` 변경 불필요. PR/Actions는 Main 몫으로 현재 미확인.

## 검사와 격리
- 명령, cwd, 종료코드·출력은 [server-validation.txt](evidence/server-validation.txt). 핵심: `docker run --rm --network container:kasset-test-db … pytest -q --tb=short -p no:cacheprovider tests/extensions/kasset/automation/test_position_manager.py` (cwd `/work`, exit 0, 67 passed), `ruff check --no-cache` (exit 0), `ruff format --no-cache --check` (exit 0), `ty check --error-on-warning` (exit 0). 모두 서버 `/tmp/kasset-exit-ladder-20260923` test checkout에 한정. 연구 runner는 `--network none --cpus=1.0`(exit 0, 결과 `artifact://133`). 로컬 워크스테이션에서 Python 검사·빌드는 실행하지 않았다.
- 테스트 후 운영 DB의 public 테이블 수 read-only `109`, api/worker/scheduler/DB 등 8개 서비스 모두 Up. 주문 제조·브로커 실행·운영 코드 checkout 수정 없음.
- 연구용 임시 CSV는 로컬·서버 모두 정확한 3개 파일을 제거했다. 연구/pytest 컨테이너는 매번 `--rm`으로 종료됐다. 서버 임시 checkout `/tmp/kasset-exit-ladder-20260923`은 보존 중이다(하위 절대 경로 recursive 삭제는 Maker 금지); Main이 검수 뒤 정리해야 한다.

## Main 기록·HANDOFF 영향
- 공유 `doc/README.md`, `main.md`, `HANDOFF.md`는 수정하지 않았다. Main이 본 항목(버그 1·2 배포 후보, 장중 c/d 표본 부족 미판정, 재검토 조건, fingerprint 경계, 서버 임시 checkout 정리, PR/Actions 미검증)을 반영할 수 있다. 운영 PAPER 자연 체결 검증 및 배포는 별도 사용자 승인/관찰을 따르며 이번 Maker 결과로 완료됐다고 보지 않는다.
