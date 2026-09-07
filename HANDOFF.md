# HANDOFF — KAsset-Trader-Core
갱신: 2026-09-07 (PAPER 자동 손절 -3% 바닥 구현·로컬 검증 완료, CI·운영 미반영 / 장중 보호 청산·손절 후 매수 후보 유지, 배포 완료)

## 현재 목표·운영 상태
- 사용자 확정 전략은 장중 돌파 단기매매다. 손절 조건을 장중에 평가하고 손절·실현손실 자체가 다음 매수 후보를 막지 않도록 한다. 당일 강제청산은 추가하지 않는다.
- **2026-09-07 사용자 승인 변경**: 실제 체결 평단 대비 -3% 자동 손절 바닥을 도입했다. 아래 "임의의 고정 3% 손절은 추가하지 않는다"던 기존 제약은 이 명시 승인으로 대체됐다. 기존 보유분에도 적용하며 다음 평가에서 곧바로 매도가 나올 수 있음을 인지한 승인이다.
- 운영은 `5c436eb02156d45aa458e244b969cf386475fc36`(PR #59). API·worker·scheduler·MCP·AI MCP 동일 이미지, `/health` 정상. 이번 수정은 `fix/intraday-exit`, `.worktrees/intraday-exit`에 있으며 운영 배포·강제 주문·운영 DB 수정은 하지 않았다. main merge는 자동배포이므로 별도 승인 필요.
- 기존 기본 checkout은 `main`/`e4b6043`와 사용자 `HANDOFF.md` 미커밋 변경을 그대로 보존했다. 중단된 새 worktree checkout만 복구했다.

## 2026-09-07 — PAPER 자동 손절 -3% 바닥 (구현·로컬 검증 완료, CI·운영 미반영)
### 승인 범위
- 대상은 KR/US PAPER 관리 보유 **전체**(기존 보유분 포함)다. 유효 손절선은 `max(실제 체결 평단 * Decimal('0.97'), 기존 손절선)`이다.
- 정확히 -3%에서의 체결을 보장하지 않는다. 손절선 아래에서 시가가 형성되면 기존 `STOP_GAP` 계약대로 그 시가를 참조가로 쓴다. 참조가는 추천값이며 체결가 보장이 아니다.

### 작업 기준
- `origin/main`의 `e3680671bb19c323f62d5fcac18efdcb0c91c7b7`(PR #60)에서 분리한 `task/paper-auto-stoploss`, worktree `.worktrees/paper-auto-stoploss`.
- 기본 checkout(`main`/`e4b6043` + 사용자 `HANDOFF.md` 미커밋 변경)은 읽기만 하고 그대로 보존했다. 기본 checkout이 `origin/main`보다 14커밋 뒤이므로 이 문서 갱신은 worktree 쪽에만 했다. 두 HANDOFF의 병합은 Main이 판단한다.

### 수정
- `automation/position_manager.py`: `STOP_LOSS_FLOOR_RATIO = Decimal("0.97")`, `stop_loss_floor()`, `apply_stop_loss_floor()`를 추가했다. 바닥은 `current_stop`뿐 아니라 `initial_stop`에도 먹인다. `current_stop`만 올리면 추적한 적 없는 청산이 `trailed` 판정에 걸려 `TRAILING_STOP`으로 잘못 보고된다. 평단이 없거나 양수가 아니면 손절선을 만들지 않고 `ValueError`로 끝나 기존 fail-closed 계약을 따른다.
- `automation/position_manager_service.py`: 신규 state 생성과 기존 state 재적재 **두 자리 모두**에서 원장 `PaperPosition.avg_price`를 다시 읽어 바닥을 적용한다. 덕분에 기존 보유분이 일봉 커서 전진이나 수동 DB 마이그레이션을 기다리지 않고, 다음 tick의 일봉·장중 평가가 곧바로 같은 손절선을 본다. 갱신값은 기존 `_apply_state` 경로로 저장되며 모든 return 경로가 이를 거친다.
- 평가기(`evaluate_position`, `evaluate_position_intraday`)와 `initialize_position`의 계산식은 바꾸지 않았다. 두 horizon 모두 저장된 `current_stop`만 읽으므로 상태를 세우는 자리 한 곳에서만 바닥을 먹여 두 horizon이 같은 손절선을 본다.
- `entry_price`는 덮어쓰지 않는다. 부분익절선(`entry_price + 3 ATR`)과 진전폭 판정의 기준이므로, 최신 평단은 바닥 계산에만 쓴다. 추가매수로 평단이 오르면 바닥도 오르고, 물타기로 평단이 내려가도 `max` 때문에 기존의 더 타이트한 손절선이 유지된다.
- 세션/신선도/claim·멱등성/STOP의 부분익절 우선순위/실제 잔량/BUY 판정은 건드리지 않았다. 스케줄·UI·설정 변경은 없다.
- 변경 파일: `app/extensions/kasset/automation/position_manager.py`, `app/extensions/kasset/automation/position_manager_service.py`, `app/extensions/kasset/models.py`, `alembic/versions/20260907_kasset_position_state_optional_atr.py`(신규), `tests/_schema_bootstrap.py`, `tests/extensions/kasset/automation/test_position_manager.py`, 이 문서.

### 델타 — ATR 근거가 없는 보유분도 고정 손절선으로 보호 (Main 지적 반영)
- 문제: 신규·미관리 보유분은 `_average_true_range`가 `None`(일봉 15봉 미만 또는 ATR<=0)이면 `_manage_position`이 즉시 `return None`이었다. 실제 체결 평단만 있으면 -3%는 계산할 수 있는데도 보유분이 무보호로 남았다.
- 해결: `initial_atr`을 도메인·스키마 양쪽에서 optional로 바꿨다. `ManagedPositionState.initial_atr: Decimal | None`, `initialize_position(initial_atr=None)`은 손절선을 체결 평단 -3% 바닥에서 시작한다. ATR을 0이나 임의값으로 조작하지 않는다.
- ATR이 `None`인 상태에서 평가기는 저장된 손절선 도달만 판정한다. 부분익절선(`entry_price + 3 ATR`), trailing 상향, `TIME_STOP`은 만들지 않는다. 근거 없는 목표가로 손절 보호가 가짜 익절을 내는 것을 막는다. `TREND_BROKEN`은 ATR과 무관해 그대로 두지만, 20봉 미만이면 기존 `_trend_intact`가 `True`를 돌려주므로 근거 없이 청산되지 않는다.
- 근거 필드도 정직하게 남긴다. `initialAtr`은 ATR이 없으면 문자열 대신 `null`이다.
- 나중에 일봉 근거가 생기면 `adopt_initial_atr()`이 같은 사이클 row에 ATR을 채운다. `initial_stop`/`current_stop`은 `max`로만 움직이므로 이미 들고 있던 손절선이 넓어지지 않고, ATR 손절선이 더 타이트할 때만 올라간다.
- **ATR은 쓸 수 있는 일봉에서만 만든다**(checker MAJOR1, Main ACCEPTED). `atr = _average_true_range(ordered) if daily_usable else None`. 미래 시각이거나 `_MAX_BAR_AGE`(4일)를 넘긴 일봉이 15봉 있어도 ATR을 만들지 않고, 고정 손절선 상태에 나중에 채워 넣지도 않는다. 그런 일봉은 손절선·부분익절선의 근거가 아니기 때문이다. 이미 non-null ATR을 들고 있는 상태는 그대로 두므로 저장된 손절선으로 하는 장중 보호는 영향받지 않는다.
- 스키마: `kasset_paper_position_states.initial_atr`을 `DROP NOT NULL`한다(`alembic/versions/20260907_kasset_position_state_optional_atr.py`, revision `20260907_kasset_optional_atr`, down_revision `20260903_kasset_alert_events`). 기존 `CHECK (initial_atr > 0)`은 NULL에서 `UNKNOWN`이라 제약을 만족하므로 교체하지 않았다. 데이터 삭제·재작성 없는 전방 호환 완화다.
- downgrade는 NULL 행이 남아 있으면 `RuntimeError`로 거부한다. 그 행은 고정 손절선으로 보호 중인 실제 보유분이므로 삭제하거나 가짜 ATR로 채우면 손절 근거가 조작된다.
- `tests/_schema_bootstrap.py`의 `SCHEMA_BOOTSTRAP_VERSION`을 52 → 53으로 올렸다. mirrored ALTER가 없으므로 이 bump가 상시 로컬 테스트 DB를 한 번 재생성해 nullable 컬럼을 만든다(기존 규약과 동일).
- **배포 경로**: 이 과제는 `alembic/versions` 변경을 포함한다. `deploy/kasset/deploy.sh:44-55`가 `alembic/versions` diff를 감지해 `ALLOW_MIGRATION=1`(workflow_dispatch `allow_migration=true`)이 아니면 `exit 2`로 멈춘다. 자동배포 코드·gate는 이 과제에서 **변경하지 않았다**.
- **문제**: `deploy.sh`의 승인 경로도 그대로는 쓸 수 없다. `deploy.sh:89`가 migration을 돌린 직후 `deploy.sh:96-97`이 `api worker scheduler mcp ai-mcp`를 **한 번에** `up -d` 하므로, worker·scheduler를 사전에 멈춰둬도 같은 실행에서 함께 올라온다. 즉 "migration → api만 health 확인 → 그 다음 worker" 순서를 만들 수 없고, health 실패 시 `deploy.sh:64-72 rollback()`이 **이전 SHA 이미지로 되돌린다**. nullable 상태 row가 이미 생겼다면 구버전은 `initial_atr` non-null을 가정하므로 이 자동 롤백은 위험하다.
- **사용자 승인 대기 제안(확정 배포 절차 아님)**: 아래는 Main이 CI·준비를 마친 뒤 사용자에게 "workflow 이탈 + rollback 호환성 변경"을 구체적으로 승인받기 위한 제안이다. 승인 전에는 어떤 단계도 실행하지 않는다. 실행은 Main만 한다. cwd `/opt/kasset-trader-core`(`KASSET_REPO_DIR`), compose 규약은 `deploy.sh:25`와 동일하게 `docker compose -f docker-compose.kasset.yml --env-file .env.kasset`.
  1. `.env.kasset` 백업(`.env.kasset.pre-<sha8>`) → 대상 SHA checkout → `CORE_IMAGE_TAG`/`VCS_REF`를 대상 SHA로 갱신(`deploy.sh:58-61`과 동일).
  2. worker·scheduler 정지 유지: `compose stop worker scheduler`. 자동 sweep이 nullable row를 만들기 전 상태를 고정한다.
  3. 이미지 빌드: `compose build api`(`deploy.sh:76`과 동일 규약. 5개 서비스가 같은 `kasset-trader-core:${CORE_IMAGE_TAG}` 이미지를 공유한다).
  4. DB 백업: `docker exec kasset-trader-db-1 pg_dump -U kasset -d kasset --format=custom | gzip > backups/kasset-pre-migration-<sha8>-<stamp>.dump.gz`(`deploy.sh:80-87`과 동일, 빈 파일이면 중단).
  5. migration: `compose --profile migration run --rm -T migration`(= `alembic upgrade head`, `docker-compose.kasset.yml:154-164`).
  6. `compose up -d --no-build api mcp ai-mcp`만 올린다(`ai-mcp`는 `profiles: ["ai-mcp"]`이므로 서비스명을 명시해 활성화한다). `https://$KASSET_DOMAIN/health` 200과 `docker ps`의 `kasset-trader-core:<sha>` 태그로 SHA 일치를 확인한다.
  7. 그 다음에야 `compose up -d --no-build worker scheduler`로 신규 SHA를 올리고, 다음 자연 사이클의 실행 로그를 확인한다.
  8. 자동 롤백 스크립트(`deploy.sh` rollback 경로)는 쓰지 않는다. 실패 시 worker·scheduler 정지를 유지한 채 `SELECT count(*) FROM kasset_paper_position_states WHERE initial_atr IS NULL`을 read-only로 확인한다. 0건이면 `compose --profile migration run --rm -T migration alembic downgrade <이전 revision>` 후 이전 이미지로 되돌릴 수 있다. 1건 이상이면 구버전 반영을 금지하고 nullable을 이해하는 버전으로 roll-forward한다(migration의 downgrade 자체도 NULL 행이 있으면 `RuntimeError`로 거부한다).
- 현재 상태: 운영은 **미변경**이다. 소스·commit은 진행하지만 CI 미실행, 배포·운영 DB 반영·실주문 없음. 위 절차의 실행 승인은 아직 받지 않았다.

### 검증 결과 (로컬, 2026-09-07)
- Main 최종 검증(MAJOR1 수정이 모두 들어간 현재 소스): 집중 스위트 `tests/extensions/kasset/automation/test_position_manager.py` **55 passed / 1 deselected**(6.18s, 외부 HTTP·socket 차단 0건, exit 0), `ruff check` all passed, `ruff format --check` 5 files unchanged, `ty check --error-on-warning` 실행코드 3파일 all passed(exit 0), 무네트워크 스모크 통과.
- 독립 checker 1회 FAIL → Main 판정: MAJOR1(쓸 수 없는 일봉에서 ATR 생성·후발 채움 금지) ACCEPTED 후 수정 완료, MINOR(문서) ACCEPTED 후 반영. MAJOR2(nullable row 생성 이후 구버전 롤백 불가)는 코드 변경이 아니라 배포 절차 문제로, 위 "사용자 승인 대기 제안"에 기재만 했고 **아직 승인·확정되지 않았다**.
- 순수 함수 pre/post 회귀(무DB·무네트워크): 저장 손절선 70000(-30%)·장중 시가 99000·저가 96000 → 수정 전 `청산 없음`, 수정 후 `STOP @ 97000.00`(핵심 RED→GREEN). trailing 105000은 전후 모두 `TRAILING_STOP @ 105000`. 손절선이 정확히 바닥(97000)이면 전후 모두 `STOP @ 97000`. 저장 진입가 100000·실제 체결 평단 110000이면 유효 손절선 `106700.00`.
- ATR 부재 스모크: `atr=None / initial_stop=current_stop=97.00` → 장중 저가 96에서 `STOP @ 97.00`, 장중 고가 132에서 신호 `None`(가짜 익절 없음), `bars_held=10` 일봉에서 `TIME_STOP` 없음, `adopt_initial_atr(4)` 후 stop `97.00` 유지(88로 넓어지지 않음), `adopt_initial_atr(0.5)` 후 `98.5`.
- `python -m py_compile` 변경 6파일 통과. `alembic heads` = `20260907_kasset_optional_atr (head)` 단일 head.
- **미검증**: DB가 필요한 `test_closed_cycle_survives_position_delete_as_audit`, migration roundtrip(`tests/services/**/test_migration*.py`), `tests/extensions/kasset/test_multi_user_migration_guards.py`, alembic `upgrade`/`downgrade` 실제 적용. 이 PC에 로컬 PostgreSQL·docker CLI가 없어 GitHub CI로 보완한다. CI·운영 배포·운영 DB 반영·실주문은 아직 하지 않았다.
- 임시 시연물 `.worktrees/paper_stop_floor_demo.py`와 `.worktrees/_baseline_pr60/`은 스모크 완료 후 Main 승인으로 제거했다(결과 원문은 위 두 항목에 보존).

### 재현 명령 (cwd `V:/HANSE/KAsset-Trader-Core/.worktrees/paper-auto-stoploss`, Main이 실행 완료)
- 이 PC에는 로컬 PostgreSQL이 없다(`localhost:5432` 연결 실패, `*postgre*` 서비스 없음, docker CLI 없음). `db_session`이 필요한 테스트는 이 환경에서 실행할 수 없다.
- 공통 env(값은 형식 검사만 통과하면 되고 접속하지 않는다): `PYTHONPATH=.`, `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/test_db`, `SECRET_KEY=Aa1TestSecretKeyForLocalOnly12345`, `UPBIT_ACCESS_KEY=x`, `UPBIT_SECRET_KEY=x`, `OPENDART_API_KEY=x`. 인터프리터는 기본 checkout의 `V:/HANSE/KAsset-Trader-Core/.venv/Scripts/python.exe`(worktree에는 venv가 없다).
- 집중(DB 불필요): `python -m pytest tests/extensions/kasset/automation/test_position_manager.py -q --tb=short --deselect tests/extensions/kasset/automation/test_position_manager.py::test_closed_cycle_survives_position_delete_as_audit`
- 인접 계약: `python -m pytest tests/extensions/kasset/automation/test_portfolio_backtest.py tests/extensions/kasset/automation/test_job.py tests/extensions/kasset/automation/test_vertical_slice.py tests/extensions/kasset/automation/test_p0_cycle_trace.py -q --tb=short`
- `ruff check` / `ruff format --check`를 변경 6파일에, `ty check --error-on-warning`을 실행코드 3파일(`position_manager.py`, `position_manager_service.py`, `models.py`)에 적용.
- alembic: `python -m alembic heads`(단일 head 확인). `upgrade head` 실제 적용과 downgrade 거부 확인은 PostgreSQL이 있는 환경(CI 또는 운영 migration 승인 경로)에서만 가능하다. `test_closed_cycle_survives_position_delete_as_audit`와 `tests/services/**/test_migration*.py`, `tests/extensions/kasset/test_multi_user_migration_guards.py`도 같은 이유로 이 PC에서 미검증이며 CI로 보완해야 한다.

### 수정한 기존 테스트와 이유
- `test_new_buy_creates_fresh_state_from_position_average_price`, `test_intraday_exit_fires_when_entry_is_newer_than_daily_history`: ATR 손절선 `88`을 고정하던 단언을 바닥 `97`로 바꿨다. 계약이 바뀐 자리다.
- `test_partial_fill_keeps_same_cycle_and_marks_remaining_state`, `test_intraday_full_stop_outranks_the_daily_partial_signal`: fixture 일봉의 저가가 -10%까지 내려가 새 바닥에 걸렸다. 각 테스트가 방어하는 계약(부분체결 사이클 기록, 장중 전량 손절의 일봉 부분익절 우선)은 그대로 두고 바닥에 걸리지 않는 저가로 조정했다.
- `test_intraday_exit_fires_after_the_last_daily_bar_was_evaluated`: 첫 bucket 시가가 바닥 아래여서 `STOP`이 `STOP_GAP`이 됐다. 단언한 계약을 지키도록 bucket 시가를 바닥 위로 올렸다.
- 신규 회귀: 넓은 ATR 손절선 바닥 적용, 더 타이트한 trailing 보존, 저변동 ATR 손절선 보존(장중 저가 98 → `STOP @ 98.5`), 저가가 바닥에 정확히 닿는 경계, 평단 없음 fail-closed, 일봉 커서 전진 없이 기존 보유분 보호, 실제 평단(110) vs 저장 진입가(100) 구분.
- 델타에서 다시 손댄 테스트:
  - `test_new_position_without_daily_history_stays_fail_closed` 삭제 → `test_new_position_without_daily_history_uses_the_fixed_stop`. ATR 부재만으로 보호를 거부하던 계약이 무효가 됐다. 새 단언은 `initial_atr is None` + 손절선 `97.00` + `exitKind=STOP` + `initialAtr` 근거 `null` + 전량 수량 `10`이다.
  - 신규 `test_fixed_stop_without_atr_never_invents_a_take_profit`: 장중 고가 132(ATR 10이었다면 부분익절선 130 관통)에서 추천이 생기지 않는다.
  - 신규 `test_later_atr_fills_the_state_without_loosening_the_fixed_stop`: ATR 4가 생겨도(ATR 손절선 88) 저장 손절선은 `97`을 유지하고 `initial_atr`만 채워진다.
  - 이전 단계에서 객체 동일성을 고정하던 `test_stop_floor_does_not_churn_state_at_the_equality_boundary`는 삭제되어 관측 가능한 경계 테스트(`test_floored_stop_triggers_when_the_low_exactly_touches_it`)로 대체됐고, 저변동 보존 테스트도 `is` 단언 없이 실제 청산가로 단언한다.
  - 신규 `test_unusable_daily_history_does_not_derive_an_atr`, `test_unusable_daily_history_does_not_hydrate_a_fixed_stop_state`(각각 `future`/`stale` 두 신선도 사유로 parametrize). 쓸 수 없는 일봉 15봉(TR=4 → ATR 4)과 현재 장중 봉이 함께 있을 때, ATR을 채택했다면 부분익절선 `100 + 3*4 = 112`를 장중 고가 115가 관통해 `PARTIAL_SELL`이 나왔을 자리다. 실제로는 추천이 생기지 않고 `initial_atr`이 `None`으로 남으며 고정 손절선 `97.00`만 유지된다. 테스트 헬퍼 `_atr_candles(first_day=...)`로 일봉 창만 옮긴다.
  - migration roundtrip: `tests/services/paper_evaluation/test_migration.py`, `tests/services/paper_cohort/test_migration.py`는 `upgrade head → downgrade <옛 revision> → upgrade head`를 돌리고 두 파일 모두 이미 `kasset_paper_position_states`를 drop 목록에 갖고 있다. 새 revision은 `head`로 자동 포함되고 빈 테이블에서는 downgrade 거부 조건(NULL 행)이 성립하지 않으므로, 별도 migration 테스트를 새로 만들 필요가 없었다(누락 없음).
- 기존 `test_intraday_exit_fires_when_entry_is_newer_than_daily_history`(일봉 최신 `+1일`, now `+3일 1시간` → 신선)와 `test_stale_daily_history_does_not_block_the_intraday_exit`(state가 이미 ATR 보유)는 신선도 gate 도입에도 계약이 그대로다.

### 유지된 제약·남은 위험
- 틱 즉시 손절이 아니다. 완료 bucket → 다음 평가 → 다음 집행을 기다리는 기존 sweep 주기를 그대로 쓰므로 -3% 정확 체결은 보장되지 않는다.
- 기존 보유분 중 이미 -3% 아래인 종목은 다음 평가에서 즉시 전량 손절 추천이 나온다. 사용자가 인지·승인한 결과다.
- trailing으로 바닥보다 낮은(더 넓은) 자리에 있던 손절선이 바닥으로 올라오면, 그 청산의 `ExitKind`는 `TRAILING_STOP`이 아니라 `STOP`으로 보고된다. 바닥이 기준 손절선이 되었기 때문이며 손절 수준 자체는 더 타이트하다.
- **일봉 백테스트(`portfolio_backtest.py`)에는 이 바닥을 적용하지 않았다.** 사용자 승인 범위가 PAPER 보유분이고, 백테스트를 바꾸면 strategy promotion 근거·게이트가 함께 움직인다. 그 결과 라이브 PAPER 손절과 일봉 백테스트 손절 의미가 갈라진다. 이 divergence의 처리는 Main·사용자 판단 대상이다.
- **배포 순서 위험**: migration(`initial_atr` DROP NOT NULL)이 새 코드보다 먼저 적용돼야 한다. 코드가 먼저 뜨면 ATR 없는 보유분의 state INSERT가 `NOT NULL` 위반이 되고, `run_owner`의 예외 핸들러는 `(DecimalException, TypeError, ValueError)`만 잡으므로 `IntegrityError`가 그 owner의 이번 sweep을 중단시킨다. 기존 관리 중인 보유분은 항상 non-NULL을 쓰므로 영향받지 않는다.
- ATR이 없는 동안 그 보유분에는 부분익절·trailing·`TIME_STOP`이 없다. 고정 손절선만 작동하고, 일봉 15봉이 모이는 tick에서 ATR 판정이 살아난다.
- 운영 배포·운영 DB 수정·실주문은 하지 않았다. 독립 checker 1회와 Git 마감은 Main 검증 이후다. Android `HANDOFF.md` delta도 Main 증거 확보 후에 반영한다.

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

## 운영 배포 방식 (2026-09-05부터 자동, PR #56 `fde4d4e2`)
- **main merge → Test 워크플로 성공 → `.github/workflows/deploy-kasset.yml`이 운영서버 self-hosted runner(`kasset-prod`, systemd `actions.runner.gim47656-ship-it-KAsset-Trader-Core.kasset-prod`, 사용자 `ghrunner`, docker 그룹)에서 `deploy/kasset/deploy.sh <sha>`를 실행한다.** 승인 단계 없음 — merge가 승인이다. SSH 포트는 열지 않는다.
- `deploy.sh`는 기존 수동 절차와 동일: `git checkout <sha>` → `.env.kasset`의 `CORE_IMAGE_TAG/VCS_REF` 갱신(`.env.kasset.pre-<sha8>` 백업) → `compose build api` → `up -d api worker scheduler mcp ai-mcp` → `https://$KASSET_DOMAIN/health` 200 + 5개 컨테이너 새 이미지 확인(최대 180초) → 실패 시 이전 SHA로 롤백.
- **alembic/versions 변경이 포함되면 자동배포는 exit 2로 멈춘다.** `workflow_dispatch`에서 `allow_migration=true`로 수동 실행하면 `backups/kasset-pre-migration-*.dump.gz` 백업 후 `compose --profile migration run migration`을 돌리고 배포한다. DB는 자동 롤백하지 않는다.
- 롤백/재배포: Actions → Deploy → Run workflow에 `sha` 입력.
- `/opt/kasset-trader-core`는 `ghrunner` 소유로 바꿨다(root가 아닌 runner가 checkout·env 갱신·compose를 실행). 기존 root cron 백업(`deploy/kasset-db-backup.sh`, `/root/backups`)은 영향 없다.
- 저장소가 public이라 fork PR 워크플로는 외부 기여자 전원 승인 필수로 설정했다. 사용자가 fork network 이탈 후 private 전환 예정(Free 플랜에서는 branch protection·environment 승인이 비활성화되지만 위 자동배포 모델은 그것에 의존하지 않는다).

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 운영 broker 범위는 KR/US 실계좌·주문·체결의 Toss와 KR mock read-only 조회의 NH PLUG이며, KIS 미설정은 의도된 상태다. 역사 KIS ledger/read model은 보존하되 production runtime에는 연결하지 않는다. owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인 hash, 주문 idempotency, accepted-only ledger와 broker evidence fill을 보존하고 검증 목적으로 주문을 만들지 않는다.

## 2026-09-06 — 주말 과거 시세 급등락 재알림
- 운영 원인: SOXL `+9.2468%`의 원 시세는 KST `2026-09-05 08:59:59`인데, 미국 날짜가 바뀌는 9/5·9/6 13:00 KST에 각각 새 이벤트와 `sent` 푸시가 생성됐다. 가격 감시는 주말에도 10분마다 실행되며, 기존 코드는 시세 날짜와 무관하게 요청 시각의 시장 날짜로 이벤트·중복 키를 만들었다.
- 수정 계약: KRX/US는 관측 시세의 시장 현지 날짜가 현재 시장 날짜와 같은 경우만 신규 가격 이벤트·푸시 대상으로 삼는다. 보존된 알림 목록은 지우지 않으며, 저장 기록을 통한 새 푸시에도 같은 날짜 계약을 적용한다. 현재 `CLOSED` 여부나 임의 초 단위 유효기간을 추가하지 않아 같은 시장 날짜의 정상 시간외 알림은 유지한다. CRYPTO의 기존 일봉·날짜 동작은 변경하지 않는다.
- 작업 기준: `origin/main`의 `4a06870f`에서 분리한 `fix/weekend-stale-price-alerts`. 기존 기본 checkout의 미커밋 작업은 보존했다. 운영 DB는 원인 조사에서 읽기만 했고, 기존 잘못 생성된 알림은 삭제하지 않았다.
- 변경 파일: `app/extensions/kasset/daily_routine_service.py`, `app/extensions/kasset/fcm_push_service.py`, 기존 테스트 `tests/extensions/kasset/api/test_daily_routine.py`·`tests/extensions/kasset/test_fcm_push_dispatch.py`, 이 문서. 신규 테스트 파일·스키마·Android 변경은 없다.
- 회귀 증거: 수정 전 `HEAD`의 두 서비스 소스를 메모리에 로드하고 신규 회귀 두 건을 실행했다. 금요일 시세로 토요일 알림이 생성되는 실패와 저장 이벤트의 푸시 `sent=2` 실패를 각각 확인했다(`2 failed`, exit 1). 운영 DB가 아니라 기존 전용 `kasset-test-db`의 pytest 실행별 분리 DB를 사용했다.
- 최종 검증: `python -m pytest tests/extensions/kasset/api/test_daily_routine.py tests/extensions/kasset/test_fcm_push_dispatch.py -q --tb=short` → **25 passed**, exit 0. 기존 Pydantic deprecated-config 경고 2개만 남았다. 변경 Python 4파일의 `ruff check`·`ruff format --check`, 서비스 2파일의 `ty check --error-on-warning` 통과. 첫 실행은 원격 테스트 DB 스키마 준비 때문에 600초 제한에 도달했고, fail-fast 재실행에서 전날 시세를 쓰던 history-failure fixture 충돌을 확인한 뒤 당일 시세로 보완하여 최종 통과했다.
- 독립 `checker` 1회 **PASS**, CRITICAL/MAJOR/MINOR 0. INFO의 저장 이벤트 목록 직접 단언 추가 제안은 기존 stored-history 회귀와 실제 목록 조립 코드가 계약을 방어하므로 `REJECTED_WITH_EVIDENCE`로 종결했다. **Main 최종 판정: PASS.** 전체 저장소 테스트·GitHub CI·실제 단말 FCM·운영 배포는 실행하지 않았다.
- 배포 승인: 사용자가 운영 반영과 임시 원격 브랜치 생성·병합 후 삭제를 승인했다. `4a06870f`를 가리키는 원격 `fix/weekend-stale-price-alerts`를 생성하여 `git_finalize`가 요구하는 fetch 대상을 준비했다. 검증된 수정은 PR CI 통과 후 main에 병합하여 기존 자동배포 경로로 반영한다.

## 2026-09-05 — Android KR 종목정보 투자지표·52주 고저·외국인 보유율 공급 (PR #57, `b710fd5d` 자동배포 완료)
- 운영 DB 읽기 조사: `market_valuation_snapshots`는 KR 3,929행/US 10,493행, 최신 `snapshot_date=2026-08-29`, source는 양 시장 모두 `toss_openapi`뿐이다. KR 샘플 `005930/138040/180640`은 행과 `market_cap`만 있고 `per/pbr/roe/dividend_yield/high_52w/low_52w`는 모두 NULL이었다. `invest_kr_fundamentals_snapshots`는 0행, `investor_flow_snapshots`의 KR 행도 0행이라 `foreign_holding_rate` 최신 NULL 비율은 계산할 모수가 없었다. `kr_candles_1d.value`는 최신 5거래일 3,345행에서 NULL 0행이며, 세 샘플의 최근 400일 264~266행도 NULL 0행이었다.
- 원인: `market_summary.py`는 지표를 `market_valuation_snapshots`, 외국인 보유율을 `investor_flow_snapshots`, 거래대금을 `kr_candles_1d.value`에서 읽는다. Toss symbol-master 경로는 의도적으로 `market_cap`만 쓴다. Naver 지표/수급 builder와 TaskIQ task는 이미 있지만 일반 스케줄 등록과 commit이 각각 기본 OFF 설정에 묶여 있어 운영에는 Naver 행이 한 건도 없었다. 별도 TV Screener KR snapshot flow도 deployment registration이 유예됐고 운영 테이블이 비어 있어 fallback 원천이 아니다.
- 변경: `kasset.market_snapshots.kr.sync`를 KAsset worker가 이미 로드하는 `kasset_market_events_tasks`에 매 거래일 16:40 KST로 등록했다. 기존 KAsset bounded universe(보유 → 관심종목 → 최근 추천, 최대 50)를 한 번 해석해 기존 Naver valuation/investor-flow builder를 `commit=True`로 호출한다. 전체 KRX 크롤은 하지 않는다. `market_summary.py`는 KR 일봉을 최근 260개 읽고 snapshot의 52주 고저가 NULL일 때만 저장 일봉 `high/low`의 max/min을 사용한다. 거래대금 근사(`close*volume`)는 추가하지 않았다.
- 수동 1회 실행(배포 컨테이너): `/app/.venv/bin/python -c "import asyncio; from app.tasks.kasset_market_events_tasks import kasset_kr_market_snapshots_sync as run; print(asyncio.run(run()))"`.
- 검증: focused pytest `17 passed`; 변경 Python 경로 `ruff check`, `ruff format --check`, `ty check --error-on-warning` 모두 통과했다. 로컬 PostgreSQL 미기동 때문에 DB fixture가 섞인 최초 baseline 선택은 `ConnectionRefusedError: [WinError 1225]`였고, DB-free focused node로 분리해 검증했다.
- 배포 후 SQL: `SELECT DISTINCT ON (v.symbol) v.symbol,v.snapshot_date,v.source,v.per,v.pbr,v.roe,v.dividend_yield,v.market_cap,v.high_52w,v.low_52w,f.snapshot_date AS flow_date,f.foreign_holding_rate FROM market_valuation_snapshots v LEFT JOIN investor_flow_snapshots f ON f.market='kr' AND f.symbol=v.symbol WHERE v.market='kr' AND v.symbol IN ('005930','138040','180640') ORDER BY v.symbol,(v.source='naver_finance') DESC,v.snapshot_date DESC,f.snapshot_date DESC;`
- 배포 후 curl: `curl -fsS -H "Authorization: Bearer $KASSET_TOKEN" "$KASSET_API/api/v1/market/summary?market=KRX&symbol=005930"`.

- 배포 후: 운영 worker에서 builder를 1회 수동 실행해 9종목(보유·관심·최근 추천) 채움. 메리츠 PER 9.76·PBR 1.93·ROE 22.38·52주 149,800/98,000 — 토스와 일치. 같은 날 `kr_candles_1d`에 9/3·9/4 행이 ~620종목만 있던 것(PR #50 이전 KR sync 중단 잔재)을 `run_daily_candles_sync('kr')` 1회로 3,933종목 백필(실패 7: 특수 티커 + 유니버스에 잘못 섞인 `KOSPI`). 정기 cron은 월요일 16:40 KST 첫 실행.

## 2026-09-05 — Android 토스 구성용 시세 API (PR #55, `8c192b44` 자동배포 완료)
- `GET /market/candles`: `range` 1Y/5Y/ALL(DB 일봉 260/1300/2600), `1D`는 정규장 1분봉(count 400).
- 신규 `GET /market/summary`(당일 OHLCV·거래대금·전일대비거래량%·52주·시총·PER/PBR/ROE·배당수익률(비율)·외국인소진율, 없으면 null), `GET /market/investor-flow`(KR 최신 1건), `GET /market/fx?pair=USD-KRW`(`exchange_rate_service` projection). `market_summary.py` 신규.
- `GET /market/orderbook`: US 422 → 200 `availability=UNAVAILABLE, reason=US_DEPTH_NOT_PROVIDED`; KR에 `availability` 추가, WS payload 불변.
- `POST /orders/preview`: `maxQuantity`, `tickSize`(KR), `normalizedLimitPrice`, `priceAdjusted`(기존 `tick_size.py` 재사용). 제출 경로·검증 불변. 신규 route는 `paths.py` allowlist 등록.
- 검증: focused pytest 45 passed, ruff/ty. Android 소비 측은 KAsset-Trader `c24d2417`.

## 2026-09-05 — 진입·리스크 1차 묶음 (PR #54, `7ccb115c` 운영 배포 완료)
BUY 측 관문·수량 조정만 추가했다. SELL·손절·kill switch·ORDER_COUNT의 SELL 의미론은 변경하지 않았다. 기존 SHADOW 모듈·테이블(`shadow_loss_streak.py`, `shadow_high_watermark.py`, `kasset_shadow_*`)은 계산·저장에 재사용하고 활성 정책은 별도 production 모듈로 분리했다. `shadow_manifest` activation 의미는 그대로다.

- **LOSS_STREAK** (`automation/loss_streak_gate.py`): 전역 — 최근 90분 손절 3회 → 신규 BUY 60분 차단. 종목별 — 같은 정규장 세션 손절 2회 → 그 종목 세션 종료까지 차단. 손절 사유는 `position_manager.ExitKind`의 `STOP/STOP_GAP/TRAILING_STOP/TRAILING_STOP_GAP`에서 파생(문자열 중복 정의 없음). 일반 exit가 끼면 streak 리셋. lock 저장은 관측 전용이며 streak과 활성 lock은 매 평가마다 `PaperTrade` 사실에서 재계산한다. evidence `lossStreak`(`kasset.loss-streak-gate.v1`).
- **ACCOUNT_STATE** (`automation/account_state_gate.py`): 통화 장부(KRX/KRW, US/USD)별로 세션 시작·peak·현재 평가금액을 계산. `profit_ratio ≥ 0.5×daily_goal` 또는 `peak_drawdown ≥ 0.5×max_daily_loss` → `STAGED_REDUCTION`(BUY 수량 ×0.75 후 lot 내림); `profit_ratio ≥ daily_goal` → `EXIT_ONLY`(BUY 차단, SELL 기존 경로). 임계는 owner risk preset에서 파생. 상태는 기존 HWM 테이블에 upsert. evidence `accountState`(`kasset.account-state.v1`). `position_sizing`에 `account_state_multiplier`(≤1, 초과 ValueError) 입력 추가.
- **No-Chase** (`automation/intraday_triggers.py`): **BUY 방향에만** pivot buffer·extension cap 적용 — ORB/VWAP 돌파는 기준가×(1+0.002) 이상에서만 `triggered`, 기준가×(1+0.02) 초과는 `BLOCKED/too_extended`. SELL(보유 청산) 트리거는 기존 판정(정확 돌파, cap 없음) 그대로. BUY 세션 시가 갭이 `max(1.0×ATR14, 3%×전일종가)` 이상이면 `BLOCKED/gap_up_no_chase`. `previous_close`는 **현재 세션 거래일보다 이전인 마지막 시장 현지 일봉**만 사용(당일 partial 일봉 오인 방지), `session_open_price`는 첫 분봉 timestamp가 `opens_at`과 일치할 때만 — 둘 중 하나라도 없으면 gap 검사만 `unavailable`. `IntradayTriggerDecision.valid_until = as_of+30분`은 recommendation `valid_until`을 축소한다(같은 cycle 소비에서는 만료되지 않음). 새 status `BLOCKED`. evidence `noChase`(`kasset.no-chase.v1`) + `validUntil`.
- **Hard Risk 순서**: `DAILY_MAX_LOSS → ACCOUNT_STATE → LOSS_STREAK → BUDGET → POSITION → ORDER_COUNT → AI_SHADOW → DAILY_GOAL`. 새 관문 두 개는 **계산 불가 시 PASS + evidence unavailable + WARNING**(기존 관문이 뒤에서 안전망), 확정 차단만 fail-closed.
- 회귀 테스트: `test_loss_streak_gate.py`(shard-4), `test_account_state_gate.py`(shard-2), `test_intraday_triggers.py`(+10, 기존 long/short fixture는 2% cap 안쪽 가격으로 조정), `test_position_sizing.py`, `test_vertical_slice.py`, `test_ai_trading_policy.py`(우선순위) 갱신.
- 검증: 변경 경로 `ruff check/format`, `ty check app/extensions/kasset/automation --error-on-warning` 통과, focused pytest(mock) 177 passed. 로컬 Postgres가 없어 DB fixture 테스트는 CI에서 검증(1차 커밋 `2f53058f` CI 전체 통과). **독립 checker 1회: FAIL → MAJOR 4·MINOR 6 전부 ACCEPTED·수정.** MAJOR: (1) 집행 경로 `job.py._hard_risk`가 `account_state`를 넘기지 않아 EXIT_ONLY가 집행 시 무력 → 집행 직전 owner/market 재평가 후 전달; (2) HWM/lock 관측 저장 실패가 확정 차단을 PASS로 삼킴 → 계산/저장 예외 경계 분리, 저장 실패는 `persistFailed` evidence만; (3) no-chase buffer/cap이 SELL 청산 추천을 차단 → BUY 전용으로 복원; (4) `previous_close`가 당일 partial 일봉일 수 있어 gap-up 무표시 무력 → 세션 이전 일봉만 선택. MINOR: 게이트 자체 commit 제거(호출자 commit 1회), EXIT_ONLY를 `exit_only` 사유로 funnel 집계, `representativeMarket` 병기, 첫 분봉 결측 시 `session_open_unavailable`, HANDOFF 문구 2건.
- 운영 관찰(배포 후): 첫 미장·국장 사이클 evidence에 `accountState`·`lossStreak`·`noChase` 섹션이 채워지는지, `setup_selected>0`인 사이클에서 `trigger_failures`에 `too_extended`/`gap_up_no_chase`/`expired`가 집계되는지, HWM 테이블에 KRW/USD 장부 행이 세션마다 갱신되는지 확인한다.

