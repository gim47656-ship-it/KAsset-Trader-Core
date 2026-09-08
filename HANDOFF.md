# HANDOFF — KAsset-Trader-Core
갱신: 2026-09-08 (PAPER stop 시간 소급 방지 구현·격리 PG15 검증·checker closure PASS, CI·PR·배포 대기 / US 일봉·분봉 기존 복구 확인·추가 코드 변경 0 / 2026-09-07 -3% floor 운영 반영 이력 보존)

## 현재 목표·운영 상태
- 사용자 확정 전략은 장중 돌파 단기매매다. 손절 조건을 장중에 평가하고 손절·실현손실 자체가 다음 매수 후보를 막지 않도록 한다. 당일 강제청산은 추가하지 않는다.
- **2026-09-07 사용자 승인 변경**: 실제 체결 평단 대비 -3% 자동 손절 바닥을 도입했다. 아래 "임의의 고정 3% 손절은 추가하지 않는다"던 기존 제약은 이 명시 승인으로 대체됐다. 기존 보유분에도 적용하며 다음 평가에서 곧바로 매도가 나올 수 있음을 인지한 승인이다.
- 현재 실제 checkout/API/worker/scheduler는 모두 `584b462df16b1aa3e05ec1d2f449b529ee9f6cd6`다. PAPER stop 시간 소급 방지 변경은 이 SHA에서 분리한 `fix/paper-temporal-market-data` worktree에만 있으며 아직 commit·PR·CI·운영 배포하지 않았다. 2026-09-07의 `9fefab61e80a6ade8466669e75e06cdc975a95fb` -3% floor 배포는 아래 역사 기록이다.
- 기본 checkout과 다른 worktree의 사용자 변경은 건드리지 않았다. 이번 변경 파일과 이 HANDOFF는 `.worktrees/paper-temporal-market-data` 안에만 있다.

## 2026-09-08 — PAPER stop 시간 소급 방지·US 시세 가용성 확인 (`fix/paper-temporal-market-data`, 배포 대기)
### 문제와 확정 계약
- 2026-09-07 19:40 KST 장 종료 후 배포된 한진칼(`180640`) -3% floor `140747`가 다음 9/8 sweep에서 아직 미평가였던 9/7 일봉 `L=137000`보다 먼저 적용되면, 9/7 당시 유효했던 old stop `125342.85714286` 대신 새 stop으로 과거 청산을 발명한다. 분봉도 강화 시각 전부터 시작한 완료 bucket을 뒤늦게 읽을 때 같은 결함이 있었다.
- 현재 snapshot에는 `exit_levels_effective_at`, 이전 exact snapshot에는 `exit_level_history` JSONB를 둔다. history 항목은 `effectiveAt`, `initialAtr`, `initialStop`, `currentStop`을 decimal 문자열로 보존한다. floor·후발 ATR·일봉 trailing이 실제로 level을 바꿀 때만 snapshot을 남긴다. 지연 일봉 trailing은 그 봉에 유효했던 historical ATR/stop으로 계산해 **실제 session close 위치**에 정렬 삽입·같은 instant 병합하고, 그 뒤 모든 version과 최신 current에도 더 타이트한 stop을 전파한다. 따라서 close와 최신 floor activation 사이의 old valid trailing crossing도 보존하면서 현재 보호선은 낮추지 않는다.
- 일봉 `time_utc`는 deterministic signal/cursor label로 유지하되, stop version 선택·trailing activation·완료 여부는 `regular_session_bounds`가 준 KR/US 실제 정규장 open/close를 쓴다. 아직 닫히지 않은 현재 session/future label은 일봉 exit·ATR·trend에 쓰지 않는다. 분봉은 bucket 시작 시각에 유효한 snapshot을 쓴다. activation을 가로지른 bucket 내부는 OHLC만으로 전후 touch를 나눌 수 없으므로 old level을 쓰고, activation과 같거나 이후에 시작한 첫 온전한 bucket부터 new level을 쓴다.
- 미처리 일봉은 시간순으로 모두 replay한다. 앞선 partial은 후보로 보존하되 이후 old-stop full exit가 있으면 full exit가 우선하며, partial state는 기존대로 PAPER 실행 성공 전에는 확정하지 않는다. old full exit가 이미 성립한 run에서는 floor를 먼저 올려 recommendation/evidence를 오염시키지 않는다. signal evidence의 ATR/stop은 최신 state가 아니라 해당 관측에 실제 사용한 historical snapshot이다.
- migration 전 legacy 행의 두 새 컬럼이 모두 NULL이면 `updated_at`·migration 시각을 추론하지 않는다. 현재 stop/ATR snapshot이 과거부터 유효했던 것으로 해석하고, 첫 실제 강화에서 그 snapshot을 `effectiveAt=null` history로 남긴 뒤 새 snapshot을 정확한 `_now`부터 활성화한다.
- state가 없거나 현재 position cycle과 맞지 않아 최초 관리하는 보유분은 보호 snapshot을 `position.created_at`으로 소급하지 않고 정확한 manager `_now`부터 만든다. 최초 관리 전 일봉은 cursor만 진행하고 highest/exit를 발명하지 않으며, 최초 관리 전 시작한 분봉도 skip한다. floor/ATR은 no-data여도 즉시 저장되고 다음 valid bucket부터 보호한다. 임의 grace window, readiness/defer gate, 새 scheduler는 없다.
- `STOP_LOSS_FLOOR_RATIO = Decimal("0.97")`, 모든 BUY threshold·Toss gate·owner scope·kill/hard-risk·approval·idempotency 계약은 바꾸지 않았다. exit evaluator의 STOP exact-touch/gap, partial, TIME_STOP, TREND_BROKEN 정책도 바꾸지 않았다.

### 스키마·변경 파일
- 새 additive revision `20260908_kasset_stop_history`(`down_revision=20260907_kasset_optional_atr`)은 `kasset_paper_position_states.exit_levels_effective_at TIMESTAMPTZ NULL`, `exit_level_history JSONB NULL` 두 컬럼만 추가한다. 기존 행 UPDATE/backfill은 없다. `tests/_schema_bootstrap.py` version은 54다.
- 변경 파일: `app/extensions/kasset/automation/position_manager.py`, `app/extensions/kasset/automation/position_manager_service.py`, `app/extensions/kasset/models.py`, `alembic/versions/20260908_kasset_position_stop_history.py`, `tests/_schema_bootstrap.py`, `tests/extensions/kasset/automation/test_position_manager.py`, 이 문서.
- `intraday_data.py`, `vertical_slice.py`, `producer.py`, data job, BUY sizing/threshold, scheduler는 수정하지 않았다.

### US 일봉·분봉·reference sizing 조사
- 현재 runtime `584b462df` ancestry에는 US daily 단일-symbol 실패 격리 `954861d5`, non-Toss-master 제외 `04e3450f`, US ET-naive intraday timestamp 수정 `5a5f737f`와 회귀 `83f44174`가 이미 포함돼 있다. 9/3 CRWD가 `completedThrough=2026-08-31`이었던 원인은 당시 첫 Toss `stock-not-found`가 US universe sync 전체를 중단시킨 결함이었다.
- 현재 CRWD 9/1~9/4 일봉과 runtime `CompletedIntradayBars` 16개(Toss, `dataAsOf=2026-09-08 14:50Z`)를 확인했다. 추가 backfill·scheduler·fallback·gate·migration·운영 mutation은 필요하지 않아 US slice 코드 변경은 0개다.
- CRWD 9/1·9/2의 `ingested_at=2026-09-07 22:06:59Z`는 conflict upsert가 `ingested_at=now()`로 갱신한 최근 **재수집** 시각이지 최초 수집 증거가 아니다. 전체 universe 해당 일봉의 남은 최소 `ingested_at`은 `2026-09-04 15:53:32Z`다. 따라서 `ingested_at`만으로 9/3 당시 CRWD row 존재를 주장하지 않는다.
- CRWD reference `231`은 live quote가 아니라 네 strategy entry의 중앙값이다. 실제 sizing `4.0065`, trigger `213.81` 반사실 `11.6926`, fill `214.13` 반사실 `11.6751`로 reference가 더 보수적인 수량을 만들었다. market fill은 fresh quote, stop floor는 실제 `PaperPosition.avg_price`를 쓰므로 contract defect 증거가 아니며 producer/sizing은 수정하지 않았다.

### 격리 검증·checker closure
- locked deps와 격리 PostgreSQL 15 DB/container에서 최종 position-manager **64** + 영향 caller 4파일 **138**의 결합 실행은 **202 passed / 12 warnings / 153.14s**다. warnings는 기존 OpenDartReader `SyntaxWarning`이다. US data regression **20 passed / 13.59s**, migration 범위 **10 passed / 109.10s**도 유지된다. 실행 범위가 다른 US/migration 수치를 202에 합산하지 않는다.
- 변경 6파일 `ruff check`와 `ruff format --check` 통과, source 3파일 `ty check --error-on-warning` 통과, Alembic은 `20260908_kasset_stop_history` single head다.
- checker MAJOR 재현 test-only RED: delayed day1 close trailing `110` 뒤 최신 floor activation 전 day2 `L=100`이 `TRAILING_STOP @ 110`이어야 하는 pure/service JSON-restart 두 case가 수정 전 source에서 모두 signal/recommendation `None`으로 실패했다. **2 failed / 62 deselected / 9.82s, exit 1**.
- source 수정 후 manager/caller 결합 GREEN은 위 **202 passed / 153.14s, exit 0**이며 external HTTP/socket blocked는 0이다. 근거: `local://temporal-trailing-red.txt`, `local://temporal-trailing-green.txt`, `local://temporal-static-closure.txt`, `local://us-data-regressions.txt`, `local://temporal-migrations.txt`.
- 독립 checker 정확히 1회에서 delayed-trailing MAJOR를 제기했고 Main이 ACCEPTED·수정한 뒤 **동일 finding closure PASS**, 추가 material finding 없음으로 종결했다. GitHub Actions, PR, commit/push, 운영 migration/deploy는 아직 실행하지 않았다. 사용자는 최종 권고 반영과 배포를 승인했지만 Main review·PR/CI 뒤의 production mutation은 Main만 수행한다.

### 승인된 운영 반영 순서·rollback 금지
1. 현재 운영 DB full backup을 만들고 non-empty 및 `pg_restore --list`를 확인한다.
2. worker·scheduler를 먼저 정지하여 새 code/migration 확인 전 자연 sweep을 막는다.
3. 승인 SHA를 checkout하고 공용 image를 build한 뒤 `alembic upgrade head`로 `20260908_kasset_stop_history`를 적용한다.
4. `api mcp ai-mcp`만 먼저 올려 image/build SHA 일치와 health를 확인한다.
5. 그 뒤 `worker scheduler`를 같은 SHA로 올리고 다음 자연 sweep을 관찰한다. 검증 목적으로 수동 PAPER 주문이나 강제 sweep은 만들지 않는다.
- 새 컬럼 downgrade는 temporal provenance를 삭제한다. 새 행/history가 생긴 뒤 이 revision을 모르는 구버전 image로 rollback하면 같은 과거 소급 결함을 재도입하므로 **구버전 rollback과 Alembic downgrade를 금지**하고, 실패 시 worker·scheduler 정지를 유지한 채 이 revision을 이해하는 image로 roll-forward한다. 자동 rollback 경로도 쓰지 않는다.

## 2026-09-08 — PAPER/Toss 외 broker 표면 제거 (`cleanup/remove-nhplug` 소스 정리·검증 기록, 미병합·미배포)
### 범위와 결과
- 대상은 PAPER/Toss가 아닌 모든 주문 provider 표면이다: NH PLUG, KIS(live·mock·WebSocket·reconcile), Kiwoom(KR·US mock), Alpaca paper(+paper-cohort·paper-evaluation·us-dual-paper), Binance Spot/Futures/Demo scalping. client·transport·service·job·task·router·MCP tool·script·smoke·runbook을 삭제했고, 남은 주문 실행 표면은 Toss live와 KAsset PAPER 모의뿐이다.
- 작업 위치는 base `60f725373446ec9cd01f7d4e5a33f9e307db6e6c`에서 분리한 branch `cleanup/remove-nhplug`, worktree `.worktrees/cleanup-remove-nhplug`다.
- **기본 checkout(`main`/`e4b6043`)은 수정하지 않았고 기존 사용자 `HANDOFF.md` 미커밋 변경을 그대로 보존했다.** 이 문서 갱신도 worktree 쪽에만 했고, 두 HANDOFF 병합은 Main이 판단한다.

### 보존 예외 (지우지 않은 것)
- **역사 데이터·모델·스키마**: `kasset_broker_credentials`, `symbol_master`, KIS/Kiwoom/Alpaca/Binance ledger DB 모델·테이블·행, 모든 Alembic revision, `account_mode`/source enum 값은 그대로다. 과거 원장은 원래 provenance로 계속 읽힌다. migration 변경은 0건이다.
- **역사 문서**: `docs/plans/`, `docs/superpowers/`, `docs/archive/`, `blog/`, `docs/contracts/`의 과거 기록은 이름이 남아 있어도 고치지 않았다(예: `docs/contracts/rob-1271-upbit-futures-boundary.md`의 서명 서술은 당시 증거 인용이라 보존). 현재 운영 문서만 실행 가능한 removed-provider 절차를 갖지 않도록 정리했다. 이미 있던 5줄 "운영 종료" 묘비 runbook은 저장소 규약이므로 유지하고, 삭제된 `scripts/_archive_kis/` 언급만 정정했다.
- **공개 시세 경로**: 인증·서명 없는 Upbit/Binance public market-data와 historical data 경로는 유지했다. private broker credential·주문 실행 표면만 제거 대상이다. Upbit 공개 클라이언트는 서명하지 않으므로(`app/services/brokers/upbit/client.py`에 jwt/Authorization 참조 0건) Main 승인 아래 `Settings.upbit_access_key`/`upbit_secret_key`와 private rate-limit 엔트리(`GET /v1/accounts`, `/v1/order`, `/v1/orders/closed`)만 제거하고 public `GET /v1/ticker` rate와 `UPBIT_BUY_AMOUNT`/`UPBIT_MIN_KRW_BALANCE`/`UPBIT_RATE_LIMIT_*`는 남겼다.
- 살아 있는 gate와 정책 계약은 손대지 않았다: `PAPER_EXECUTION_*`, `PAPER_VALIDATION_*`, `WATCH_AUTO_EXECUTE_MOCK_ENABLED`, `order_approval_hash_mode`, `TOSS_*`(API·live mutation·auto-reconcile·fill poll), `EXECUTION_LEDGER_COMMIT_ENABLED`, `config/trading_policy.yaml`의 content_hash 대상 내용.

### 설정·부트스트랩
- 예시·부트스트랩은 삭제된 provider의 가짜 credential을 더 이상 설정하지 않는다(`env.example`, `env.prod.example`, `deploy/kasset/env.example`, `deploy/kasset/compose.yaml`, `docker-compose.prod.yml`, `scripts/setup-test-env.sh`).
- **Settings 필수 env는 3개로 줄었다**: `SECRET_KEY`(32자+대소문자+숫자 검증), `DATABASE_URL`, `OPENDART_API_KEY`. 나머지는 모두 default가 있고 live mutation gate는 전부 default off다.

### 검증 상태 (독립 checker closure 포함)
- Android: `gradlew.bat :app:testDebugUnitTest :app:assembleDebug --console=plain`은 390 tests·failure/error/skip 0, `assembleDebug` 성공이다(`artifact://140`). 사용자 승인 후 SM-S926N(`192.168.0.148:37707`)에 기존 data를 유지한 채 `com.kasset.trader.debug` versionCode 10000/versionName `0.1-qa`를 `adb install -r`로 설치했고 `Success`를 확인했다(`artifact://679`). `MainActivity`는 `Status: ok`, cold start 522ms였으며 기존 로그인·AI픽·PAPER 자동 운용 화면이 표시됐다. 앱 PID는 유지됐고 해당 PID의 `AndroidRuntime:E`는 없었다. 설정·자산·종목·호가 실기기 경로는 확인하지 않았고 주문·설정 변경도 하지 않았다.
- Frontend: 최초 전체 687 tests, 최종 account selector 집중 32 tests, type-check·build가 통과했다. 로컬 브라우저에서 실제 인증 후 현재 account selector와 삭제 route 동작을 확인했다.
- Core: Windows contract subset 1429 tests가 failure/error/skip 없이 통과했고 Linux POSIX 전용 152 tests도 통과했다. 전체 최초 Windows 실행은 17282 passed / 400 failed / 65 errors / 29 skipped로 clean run이 아니며, 후속 실행들은 서로 겹치므로 합산 총계를 만들지 않는다. OS 실패 범위에 남은 4건은 base `60f72537`에서도 같은 test name과 같은 uv wrapper `CalledProcessError`로 재현된 baseline/environment 문제다. 아래 검증 기록에 원본 XML·후속 범위·제약을 적었다.
- 이 제거 branch는 `main`에 merge하거나 운영에 deploy하지 않았다. 운영은 위 `9fefab61` 배포 상태 그대로이며, 실주문·실 Toss 주문·운영 DB 접근/변경은 없었다. 독립 checker 1회와 동일 review의 3개 findings closure는 PASS였고, Main은 세 finding을 모두 ACCEPTED로 종결했다.

## 2026-09-07 — PAPER 자동 손절 -3% 바닥 (구현·로컬 검증 완료, CI·운영 미반영)
### 승인 범위
- 대상은 KR/US PAPER 관리 보유 **전체**(기존 보유분 포함)다. 유효 손절선은 `max(실제 체결 평단 * Decimal('0.97'), 기존 손절선)`이다.
- 정확히 -3%에서의 체결을 보장하지 않는다. 손절선 아래에서 시가가 형성되면 기존 `STOP_GAP` 계약대로 그 시가를 참조가로 쓴다. 참조가는 추천값이며 체결가 보장이 아니다.

### 작업 기준
- `origin/main`의 `e3680671bb19c323f62d5fcac18efdcb0c91c7b7`(PR #60)에서 분리한 `task/paper-auto-stoploss`, worktree `.worktrees/paper-auto-stoploss`. 최종 커밋은 `9fefab61e80a6ade8466669e75e06cdc975a95fb`이며 `git_finalize`가 이 branch의 `origin/main` tracking에 따라 **`main`에 직접 push**했다(별도 PR 없음).
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
- **사용자 승인 후 실행한 수동 단계 배포 절차**: 사용자가 "단계별 배포 진행"으로 승인했고 Main만 실행했다. cwd `/opt/kasset-trader-core`(`KASSET_REPO_DIR`), compose 규약은 `deploy.sh:25`와 동일하게 `docker compose -f docker-compose.kasset.yml --env-file .env.kasset`.
  1. DB 백업: `docker exec kasset-trader-db-1 pg_dump -U kasset -d kasset --format=custom`(`deploy.sh:80-87`과 동일 규약, 빈 파일이면 중단). 산출물 `backups/pre-stoploss-9fefab61/database.dump` 93,355,726 bytes, `pg_restore --list` 2,498행. `pg_dump`의 트랜잭션 일관 full dump이므로 서비스 정지 전에 떴다.
  2. worker·scheduler 정지: `compose stop worker scheduler`. 자동 sweep이 새 코드로 도는 시점을 이후 단계까지 막는다.
  3. `.env.kasset` 백업(`backups/pre-stoploss-9fefab61/`에 함께 보관) → 대상 SHA checkout → `CORE_IMAGE_TAG`/`VCS_REF` 갱신(`deploy.sh:58-61`과 동일).
  4. 이미지 빌드: `compose build api`(`deploy.sh:76`과 동일 규약. 5개 서비스가 같은 `kasset-trader-core:${CORE_IMAGE_TAG}` 이미지를 공유한다).
  5. migration: `compose --profile migration run --rm -T migration`(= `alembic upgrade head`, `docker-compose.kasset.yml:154-164`).
  6. `compose up -d --no-build api mcp ai-mcp`만 올린다(`ai-mcp`는 `profiles: ["ai-mcp"]`이므로 서비스명을 명시해 활성화한다). `https://$KASSET_DOMAIN/health` 200과 `docker ps`의 `kasset-trader-core:<sha>` 태그로 SHA 일치를 확인한다.
  7. 그 다음에야 `compose up -d --no-build worker scheduler`로 신규 SHA를 올리고, 다음 자연 사이클의 실행 로그를 확인한다.
  8. 자동 롤백 스크립트(`deploy.sh` rollback 경로)는 쓰지 않는다. 실패 시 worker·scheduler 정지를 유지한 채 `SELECT count(*) FROM kasset_paper_position_states WHERE initial_atr IS NULL`을 read-only로 확인한다. 0건이면 `compose --profile migration run --rm -T migration alembic downgrade <이전 revision>` 후 이전 이미지로 되돌릴 수 있다. 1건 이상이면 구버전 반영을 금지하고 nullable을 이해하는 버전으로 roll-forward한다(migration의 downgrade 자체도 NULL 행이 있으면 `RuntimeError`로 거부한다).
- 결과: 아래 "운영 배포 결과"대로 2026-09-07 10:40:51 UTC(19:40:51 KST) 반영을 완료했다. 자동 롤백 스크립트는 쓰지 않았다.

### Git 마감·CI·자동배포 차단·운영 배포 (2026-09-07)
- `git_finalize`가 `task/paper-auto-stoploss`의 `origin/main` tracking에 따라 **`main`으로 직접 push**했다. 마감 커밋 `9fefab61e80a6ade8466669e75e06cdc975a95fb` (`fix(kasset): protect paper holdings with three-percent stop floor`), 부모 `e3680671`. PR은 만들지 않았다.
- main CI **Test [`34107488770`](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/34107488770) 전체 success**(`gh run watch` exit 0). PostgreSQL 15에서 migration roundtrip, test shard 1~4, `ci-required` 집계까지 포함한다.
- 자동 **Deploy [`34108177065`](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/34108177065) 실패(의도된 차단)**. 원문 근거: `ALLOW_MIGRATION=0` → `alembic/versions 변경이 포함된 배포다. 자동배포는 건너뛴다.` → 변경 목록에 nullable migration 파일 → `exit 2`. `deploy.sh:51-55`가 `git checkout`·`.env.kasset` 갱신·build 이전 단계에서 막았으므로 운영 서버의 checkout·env·이미지·DB는 그대로다. 운영 배포는 일어나지 않았다.
- 그래서 운영 반영은 `workflow_dispatch(allow_migration=true)`가 아니라 위 수동 단계 절차로 했다. 사용자가 "단계별 배포 진행"을 승인하고 checker MAJOR2 closure를 수용한 뒤 Main만 실행했다.
- **운영 배포 결과(2026-09-07 10:40:51 UTC / 19:40:51 KST)**: API·worker·scheduler·MCP·AI MCP 5개의 image와 build SHA가 모두 `9fefab61`로 일치, restart 0.
  - 실제 순서는 `pg_dump` 완료 → worker·scheduler 정지 → checkout·build였다. 백업은 트랜잭션 일관 full dump이므로 "정지 상태에서 떴다"는 표현은 정확하지 않다. 산출물 `backups/pre-stoploss-9fefab61/database.dump` 93,355,726 bytes, `pg_restore --list` 2,498행, env 백업 동일 디렉터리.
  - migration `20260907_kasset_optional_atr` 적용 후 `initial_atr`의 `is_nullable=YES`와 `initial_atr IS NULL` **0건**을 확인했다.
  - `api`·`mcp`·`ai-mcp` 3개 healthy를 확인한 뒤 마지막에 `worker`·`scheduler`를 재개했다. 승인된 순서 그대로다.
- **배포 후 read-only 스모크(19:43:50 KST, `transaction_read_only=on`)**: 실제 배포된 `_state_from_row` + `apply_stop_loss_floor`로 실보유 4건의 유효 손절선을 계산만 했다. KRX·US 모두 **CLOSED** 상태였다.
  - `138040` 평단 135,100 / 3주: 저장 손절선 `119607.14285714` → 유효 `131047`.
  - `180640` 평단 145,100 / 2주: 저장 `125342.85714286` → 유효 `140747`.
  - `CRWD` 평단 214.13 / 4주: 저장 `176.69106567` → 유효 `207.7061`.
  - `UBS` 평단 55.60 / 44주: 저장 `53.63499957` → 유효 `53.932`.
  - 4건 모두 `derived_already_persisted=false`다. 장외라 기존 state의 DB 손절선은 아직 갱신되지 않았고, 다음 정규장 평가에서 적용·저장된다.
  - 별도 no-ATR 순수 스모크는 `initial_atr=None`과 손절선 `97`(평단 100 기준)만 확인한 것이다. 이 스모크에서 청산 신호 유무는 검증하지 않았다.
- **배포 후 자연 sweep(19:45:00 KST) 확정**: `19:45:00.003` scheduler `Sending kasset.paper_automation.run` → `19:45:00.005` worker `Executing` → `19:45:00.038` `sweep done owners=0 outcomes=[]`. Main의 원격 확인 명령은 exit 0이었다. sweep 자체는 정상 완료했다.
  - 같은 창(19:42~19:45)의 worker 로그에 기존 개별 Toss 404 경고 1건(`0106J0`, 19:43)이 있다. sweep 동작과 무관한 기존 유형의 경고이며, "ERROR 0건"이라고 주장하지 않는다.
  - 이 sweep은 `owners=0`이므로 실보유 손절선의 DB 갱신·손절 추천을 만들지 않았다. **다음 정규장에서의 실제 손절 적용·추천·체결은 아직 미관측이다.**

### 검증 결과 (2026-09-07)
- Main 최종 검증(MAJOR1 수정이 모두 들어간 현재 소스): 집중 스위트 `tests/extensions/kasset/automation/test_position_manager.py` **55 passed / 1 deselected**(6.18s, 외부 HTTP·socket 차단 0건, exit 0), `ruff check` all passed, `ruff format --check` 5 files unchanged, `ty check --error-on-warning` 실행코드 3파일 all passed(exit 0), 무네트워크 스모크 통과.
- 독립 checker 1회 FAIL → Main 판정: MAJOR1(쓸 수 없는 일봉에서 ATR 생성·후발 채움 금지) ACCEPTED 후 수정 완료, MINOR(문서) ACCEPTED 후 반영. MAJOR2(nullable row 생성 이후 구버전 롤백 불가)는 코드가 아니라 배포 절차 문제로, 위 수동 단계 절차로 대응하고 Main이 그 closure를 수용했다(사용자 승인 포함).
- 순수 함수 pre/post 회귀(무DB·무네트워크): 저장 손절선 70000(-30%)·장중 시가 99000·저가 96000 → 수정 전 `청산 없음`, 수정 후 `STOP @ 97000.00`(핵심 RED→GREEN). trailing 105000은 전후 모두 `TRAILING_STOP @ 105000`. 손절선이 정확히 바닥(97000)이면 전후 모두 `STOP @ 97000`. 저장 진입가 100000·실제 체결 평단 110000이면 유효 손절선 `106700.00`.
- ATR 부재 스모크: `atr=None / initial_stop=current_stop=97.00` → 장중 저가 96에서 `STOP @ 97.00`, 장중 고가 132에서 신호 `None`(가짜 익절 없음), `bars_held=10` 일봉에서 `TIME_STOP` 없음, `adopt_initial_atr(4)` 후 stop `97.00` 유지(88로 넓어지지 않음), `adopt_initial_atr(0.5)` 후 `98.5`.
- `python -m py_compile` 변경 6파일 통과. `alembic heads` = `20260907_kasset_optional_atr (head)` 단일 head.
- CI로 해소된 항목: PostgreSQL 15 기반 main CI에서 `test_closed_cycle_survives_position_delete_as_audit`, migration roundtrip(`tests/services/**/test_migration*.py`, `upgrade head → downgrade → upgrade head`), `tests/extensions/kasset/test_multi_user_migration_guards.py`, test shard 1~4가 모두 통과했다. 로컬에 PostgreSQL·docker CLI가 없어 남겨두었던 미검증 항목은 이 CI 성공으로 해소됐다.
- **Main 최종 판정: FINAL PASS.** 판정 근거의 범위는 위에 적은 것뿐이다 — 로컬 집중 스위트·lint·type, main CI Test 전체 success(PG15 migration roundtrip 포함), 순수 함수 pre/post 회귀, 배포 후 read-only 계산 스모크, 19:45:00 자연 sweep(`owners=0 outcomes=[]`)의 정상 완료. 이 판정은 소스와 배포 절차에 대한 것이며, **다음 정규장의 실제 손절 적용·추천·체결 관측은 포함하지 않는다.**
- 남은 확인: 다음 정규장 sweep에서 실보유 4건의 유효 손절선이 state에 저장되는지, 그리고 손절 도달 시 추천·집행이 계약대로 나오는지. 운영 DB에는 승인된 migration만 적용했고 실주문·강제 사이클은 없다.
- **롤백 주의(유효)**: 운영에 nullable 컬럼이 적용됐다. 이후 문제가 생기면 구버전 이미지로 되돌리지 않는다. `initial_atr IS NULL` 행이 0건일 때만 `alembic downgrade` 후 이전 이미지가 가능하고, 1건 이상이면 nullable을 이해하는 버전으로 roll-forward한다(migration downgrade 자체도 NULL 행이 있으면 `RuntimeError`로 거부한다). 백업은 `backups/pre-stoploss-9fefab61/database.dump`다.
- 임시 시연물 `.worktrees/paper_stop_floor_demo.py`, `.worktrees/_baseline_pr60/`, `.worktrees/paper_stop_floor.diff`는 Main 승인으로 제거했다(결과 원문은 위 항목들에 보존). task worktree·branch 정리는 Main이 수행한다.

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

