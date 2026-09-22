RECORD:
DATE: 2026-09-08
SCOPE: PAPER stop 시간 소급, exit level history, 정규장 경계, US 시세 가용성
PATHS: app/extensions/kasset/automation/position_manager.py, app/extensions/kasset/automation/position_manager_service.py, app/extensions/kasset/models.py, alembic/versions/20260908_kasset_position_stop_history.py, tests/_schema_bootstrap.py, tests/extensions/kasset/automation/test_position_manager.py
STATUS: accepted

병합: PR #62 `d6ee70e63`(`d12934951`)로 `main`에 병합돼 이후 배포 기준 SHA `d6ee70e6`에 포함됐다. 본문의 "배포 대기" 표기는 작성 시점 기준이다.

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
