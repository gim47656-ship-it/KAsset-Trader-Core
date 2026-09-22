RECORD:
DATE: 2026-09-07
SCOPE: 장중 보호 청산, 손절 이후 매수 후보 검토, loss streak gate, intraday exit
PATHS: app/extensions/kasset/automation/position_manager.py, app/extensions/kasset/automation/position_manager_service.py, app/extensions/kasset/automation/job.py, app/extensions/kasset/automation/policy.py, app/extensions/kasset/automation/loss_streak_gate.py, app/extensions/kasset/automation/vertical_slice.py, app/services/kasset_automation_audit.py, app/schemas/ai_recommendations.py, tests/extensions/kasset/automation/test_position_manager.py, tests/extensions/kasset/automation/test_job.py, tests/extensions/kasset/automation/test_ai_trading_policy.py, tests/extensions/kasset/automation/test_loss_streak_gate.py, tests/extensions/kasset/automation/test_vertical_slice.py
STATUS: accepted

병합: PR #60 `e3680671b`(`438e4da61`)로 `main`에 병합됐다. 본문의 "운영 미배포" 표기는 작성 시점 기준이며, 최종 판정 절도 이 기록과 함께 옮겼다.

## 2026-09-07 — 장중 보호 청산과 손절 이후 후보 검토
### 원인·수정
- 운영 보유 138040/180640의 일봉은 9/4에 머물렀다. 각각 `latest_at <= last_evaluated_at`/`latest_at <= entry_at` 때문에 청산 전체를 건너뛰었고 두 종목은 장중 갱신 대상인 관심종목에도 없었다.
- `automation/position_manager.py`: 일봉 상태 전이와 별개인 `evaluate_position_intraday`로 저장 손절·부분익절선 도달을 판단한다. 진입 전에 시작한 bucket 제외, 전량 손절이 앞선 부분익절보다 우선, bucket 종료 시각 기반 idempotency를 유지한다. 장중 봉을 일봉 보유일수로 세거나 일봉 trailing 커서에 쓰지 않는다.
- `automation/position_manager_service.py`: 공용 `load_completed_session_bars`로 모든 보유종목의 정규장 완료 5분봉을 읽는다. 중복·진입 이전·오래된·없는 일봉도 기존 state의 장중 보호를 막지 않는다. (당시에는 신규 state를 ATR 근거 없이 만들지 않았다. 위 2026-09-07 손절 바닥 델타에서 이 fail-closed는 고정 손절선 전용 상태로 대체됐다.) 일봉 부분익절보다 장중 전량 손절이 우선하며 미체결 부분익절의 기존 만료/대체 계약을 유지한다. 근거에 `evaluationHorizon`, `barPeriod`, `barSource`, `dataAsOf`를 기록한다.
- claim·재시도 경계: 실행 중인 CLAIMED 부분익절은 유효기간이 지났어도 먼저 기존 claim/lease 복구를 기다린다. 전량 손절을 병행 생성하지 않고 화해 후 최신 잔량으로 산정한다. 종료된 REJECTED/FAILED/expired 추천의 `barAsOf` 이후 새 완료 bucket에서만 재추천하며, `last_exit_signal_key`를 종료 후에도 보존해 재시작 시 재시도 경계가 사라지지 않는다. 일봉 커서는 이 용도로 쓰지 않는다.
- `automation/job.py`: 후보를 제거하지 않고 열린 시장 → 만료 claim 복구 → 결정론 청산 → 기존 승인/시간 순으로 정렬한다. 장외 US BUY나 먼저 승인된 BUY가 KRX 보호 SELL을 가리는 문제를 고친다. 정규장·시세·권한·claim/멱등성 gate는 유지한다.
- `automation/policy.py`, `loss_streak_gate.py`: DAILY_MAX_LOSS·LOSS_STREAK을 BUY veto가 아닌 관측 근거로 전환한다. 기존 wire rule/근거는 유지하고 detail에 비차단임을 명시한다. `LossStreakGateResult.buy_locked`는 실제 주문 잠금이 아니라 관측값이다.
- `automation/vertical_slice.py`: 손절 추천 후에도 같은 cycle에서 BUY 후보를 검토한다. owner 1시간 쿨다운은 BUY 추천만 계산하고 SELL 추천은 제외한다. 모든 반환에서 생성한 exit id를 보존한다. `services/kasset_automation_audit.py`에서 사라진 조기 skip 사유를 제거했다.
- `schemas/ai_recommendations.py`: maxDailyLossRatePct/maxDailyLossAmount가 종목 손절률이나 BUY veto가 아닌 참고값임을 Field description에 명시했다. wire 형태·기존 값의 범위는 유지한다. Android 실행 코드는 변경하지 않았다.
- 기존 테스트 5파일 수정: `test_position_manager.py`, `test_job.py`, `test_ai_trading_policy.py`, `test_loss_streak_gate.py`, `test_vertical_slice.py`. 모델/마이그레이션 소스 문자열을 고정하던 테스트 1개는 실제 DB 행위 테스트가 같은 계약을 방어하므로 제거했다. 신규 테스트 파일·schema migration 없음.

### 검증 증거
- 최초 Windows 집중 pytest: `133 passed, 2 failed`/exit 1. 두 신규 PENDING 승인 테스트의 고정 선정 시각과 실제 승인 clock이 어긋난 fixture 문제를 기존 AIRecommendationService의 clock 주입으로 수정했다. 실제 시각에 의존하는 유효기간 우회는 제거했다.
- Main 순수 함수 재현: entry=100/ATR=10/stop=70, 앞 bucket high=131, 뒤 bucket low=69 → 수정 전 `expected=STOP actual=PARTIAL_SELL`/exit 1, 수정 후 동일 입력 `expected=STOP actual=STOP`/exit 0. 일봉 PARTIAL vs 장중 STOP, 일봉 rows=[]의 기존 state/신규 state 경계를 회귀에 포함했다.
- 통합 Linux 검증: `python -m pytest tests/extensions/kasset tests/services/test_kasset_automation_audit.py tests/schemas/test_ai_recommendations_schema.py -q --tb=short` → **1311 passed, 14 warnings in 450.04s**, exit 0. 기존 Pydantic/OpenDartReader 경고만 남았다. 전체 저장소 테스트가 아니라 전체 KAsset + audit/schema 계약 범위다.
- 검증은 별도 `kasset-test-db`의 실행별 DB와 일회성 컨테이너(2 CPU/3 GiB)에서 실행했다. 변경 worktree의 tracked 파일만 stdin tar로 `/tmp`에 넣고 테스트 의존성은 uv.lock 버전에 맞췄다. 운영 DB/환경파일/credentials/실주문 경로를 사용하지 않았다. socket guard 차단 0건, 외부 HTTP 차단 0건, schema bootstrap 1.75초. 실행 종료 시 컨테이너 자동 제거.
- 변경 Python 13파일 Ruff 통과, 실행 코드 8파일 ty 통과. 후속 수정 경로의 Ruff/ty도 통과했다.
- 독립 검수 MAJOR 2건(CLAIMED partial과 full STOP 병행 생성, terminal intraday 추천의 당일 재시도 차단)을 Main이 ACCEPTED로 수용해 수정했다. 수정 후 `test_position_manager.py test_job.py test_vertical_slice.py test_consumer.py test_portfolio_backtest.py` 집중 pytest → **198 passed, 12 warnings in 149.75s**, exit 0. 같은 bucket 중복 억제·후속 bucket 새 ID/최신 잔량·CLAIMED의 만료 전후 대기·일봉 커서/종료 참조 보존을 검증했다. 해당 delta Ruff/ty exit 0.
- 운영 읽기 스모크 13:39 KST: 실제 보유 두 종목 모두 공용 장중 loader가 `period=5m`, `source=toss`, `dataAsOf=13:35`, 55봉을 반환했다. 주문 없이 입력 공급 경로만 확인했다.
- 통합 독립 checker 결과와 Git 마감은 아래 최종 판정에 기록한다.

### 유지된 제약·다음 작업
- 10분 producer + 5분 execution sweep이며 틱 즉시 손절이 아니다. 장중 조건 발생 후 봉 완료·다음 평가·다음 집행을 기다린다. 장 마감 직전 bucket, provider 지연/실패, 시장 종료 후의 체결은 보장하지 않는다. 장외 강제 주문은 하지 않는다.
- 기존 ATR 손절 폭, 일봉 trailing/추세/기간 판정, 목표 수익 EXIT_ONLY, STAGED_REDUCTION의 BUY 수량×0.75, BUY 1시간 중복 방지·일일 주문/동일종목 재진입 횟수 제한은 보존했다. 손실 원인 veto 제거를 이 모든 제한의 제거로 해석하지 않는다.
- 기존 일봉 백테스트 수익률은 새 장중 집행 전략의 성과 검증이 아니다. 실시간 체결·장마감 경계·운영 rollout은 별도 승인/관찰 대상이다.
- maxDailyLossRatePct/maxDailyLossAmount는 앱 wire에 남으며 참고값이다. 앱이 이를 강제 매수중단/종목손절로 표현하지 않는지 소비자 문구를 별도로 검토해야 한다.
- 배포 승인 후 CI·promotion fingerprint·운영 이미지 정합과 자연 SELL→후속 BUY 후보 흐름을 관찰한다. 진입/청산 임계값은 이번에 임의 변경하지 않는다.

## 최종 판정
- **FINAL: PASS, OWNER: MAIN** — 독립 checker 1회에서 제기한 MAJOR 2건 모두 ACCEPTED·수정 후 같은 review의 findings closure PASS, 추가 finding 없음. 전체 KAsset 1311 통과는 검수 전 통합본, 최종 delta는 관련 198 통과와 Ruff/ty로 검증했다. 운영 미배포이며 main 병합/배포 승인은 별도다.
