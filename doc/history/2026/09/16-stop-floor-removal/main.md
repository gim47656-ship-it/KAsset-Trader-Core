RECORD:
DATE: 2026-09-16
SCOPE: 매수 부재 조사, -3% 손절 바닥 제거, ATR 손절 계약 복귀
PATHS: app/extensions/kasset/automation/position_manager.py, app/extensions/kasset/automation/position_manager_service.py, tests/extensions/kasset/automation/test_position_manager.py, docs/kasset/AUTOMATION_BREAKOUT_CONTRACT.md, CHANGELOG.md
STATUS: accepted

병합: PR #66 `051700ffe`(`19e626086`)로 `main`에 병합됐다. 본문의 "CI 대기" 표기는 작성 시점 기준이다.

## 2026-09-16 — 매수 부재 원인 조사와 -3% 손절 바닥 제거 (`fix/remove-paper-stop-floor`, PR #66, CI 대기)
### 조사 결과 (읽기 전용, 운영 SHA `bac03e62`)
- 서버·API·worker·scheduler는 정상이고 자동매매 사이클은 10분마다 돌았다. 9/11 15:20 `443060` 매수 뒤 매수가 없는 원인은 (1) 9/10 22:04 KST 앱 설정 `recommendation_market_scope=KR_ONLY`로 미국장 사이클이 `no_configured_regular_market_open`으로 즉시 종료, (2) KR 일봉 셋업 통과 종목이 하루 5~6개인데 9/15~9/16에는 대부분 `SELL` 방향, (3) SELL 셋업은 보유가 없어 트리거가 발동해도 `presizing_zero_quantity:ZERO_HOLDING`으로 제외되는 구조다. 매수 방향 셋업은 `relative_volume_not_confirmed`(1.5배 미달)·`no_directional_trigger`에서 떨어졌다. 근거는 `review.kasset_automation_cycle_events`와 worker 로그다.
- 9/9 `010955`·`267250`, 9/11 13:30 `443060` BUY 추천은 PENDING 상태로 30분 뒤 만료됐고 실행 이력이 없다. `user_settings.kasset.ai_trading`이 9/11 14:02에 갱신된 직후부터 자동 승인·체결이 시작됐으므로 그 전까지 AUTO_PAPER가 아니었던 것으로 추정한다(당시 worker 로그는 컨테이너 재생성으로 소실, `[INFERENCE]`).
- 손절 조사: 9/7 이후 청산 4건(138040 -2.5%, 180640 소급 결함으로 +4.6%, CRWD -2.7%, 443060 -4.3%)은 모두 `initial_stop == current_stop == 평단×0.97`이었다. 앱이 매수 시 보여준 전략 손절(진입가 -3 ATR, 약 -10~15%)·목표(+3 ATR)는 실제 적용되지 않았다. KR 후보군 중앙 ATR 비율이 약 5%라 -3%는 0.6 ATR로 일중 잡음 안에 있었고, 손익비가 1:1에서 사실상 0.2:1로 무너졌다. 사용자가 이 진단을 보고 바닥 제거를 선택했다.
- 부수: `kasset_news_summary` AI 경로는 매 사이클 `subscription CLI timed out`으로 실패 중이다(매수 판정 비관여 shadow). 개장 후 ~1시간은 `relative_volume_unavailable`, 장중 간헐 `intraday_provider_unavailable`이 있다. 이번에 수정하지 않았다.

### 변경 (`maker` 1개 구현, Main 리뷰)
- `position_manager.py`: `STOP_LOSS_FLOOR_RATIO`, `stop_loss_floor`, `apply_stop_loss_floor`, `adopt_initial_atr` 삭제. `initial_atr`을 `ManagedPositionState`·`ExitLevelVersion`·`PositionExitSignal`·`initialize_position`에서 필수 `Decimal`로 환원하고 평가기의 optional-ATR 분기를 제거했다. `PositionManagerConfig` 3/3/3, STOP exact-touch/gap, PARTIAL, TIME_STOP, TREND_BROKEN, PR #62의 temporal history(`_transition_exit_levels`, `_raise_trailing_stop`)는 그대로다.
- `position_manager_service.py`: 신규 state 생성에서 floor 래핑 제거. `daily_usable`이 아니거나 ATR이 없으면 `logger.info(reason=atr_unavailable)` 후 `return None`으로 상태를 만들지 않는다. 재적재 경로의 floor/adopt 블록 제거. `_state_from_row`는 NULL `initial_atr` 행을 `ValueError`로 거부한다(운영 NULL 행 0, 열린 state 0, 보유 0 확인). `models.py`·alembic은 변경 없음, `initial_atr` 컬럼은 nullable 유지(downgrade 금지 규약).
- 테스트: floor/optional-ATR 테스트 9개 삭제, temporal 테스트 11개를 floor API 없이 재작성, ATR 계약 회귀 3개 추가. 문서: `docs/kasset/AUTOMATION_BREAKOUT_CONTRACT.md` Position Manager 절, `CHANGELOG.md` Unreleased.
- 검증: 규약 14에 따라 로컬 pytest/ruff/ty를 실행하지 않았다. 정적 확인은 삭제 심볼 참조 0건, optional-ATR 분기 0건. PR #66 CI `Test` run `35055477756`이 정본이며 `ci-required`까지 완료를 기다린 뒤 병합한다. migration이 없으므로 병합 후 기존 `Deploy` workflow가 자동 배포한다.
- 배포 후 확인: 다음 자연 BUY 체결에서 `kasset_paper_position_states.initial_stop == avg_price - 3*initial_atr`인지, `exit_level_history`가 `[]`로 시작하는지 read-only로 본다. 강제 sweep·수동 주문은 만들지 않는다.

### 남은 결정 (사용자 몫, 코드 변경 안 함)
- 미국장 재개(`KR_ONLY`→`KR_US`)는 앱 설정만으로 가능하다. 상대거래량 1.5배·no-chase 2%·돌파 버퍼 0.2%는 `intraday_triggers.py` 코드 상수이며, 변경은 `same_time_rvol_shadow` 채점 뒤에만 한다(AGENTS.md 12).
