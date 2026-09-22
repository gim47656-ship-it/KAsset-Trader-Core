RECORD:
DATE: 2026-09-08
SCOPE: broker 표면 제거, NH PLUG, KIS, Kiwoom, Alpaca, Binance, PAPER/Toss
PATHS: app/services/brokers/upbit/client.py, config/trading_policy.yaml, env.example, env.prod.example, deploy/kasset/env.example, deploy/kasset/compose.yaml, docker-compose.prod.yml, scripts/setup-test-env.sh
STATUS: accepted

병합: PR #61 `584b462df`(`d9fe42a7c`, `bc9af4785`)로 `main`에 병합됐다. 본문의 "미병합·미배포" 표기는 작성 시점 기준이다.

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
