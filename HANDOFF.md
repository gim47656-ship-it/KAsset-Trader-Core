# HANDOFF — KAsset-Trader-Core
갱신: 2026-09-07 (장중 보호 청산·손절 후 매수 후보 유지, 로컬 수정 검증 완료·미배포)

## 현재 목표·운영 상태
- 사용자 확정 전략은 장중 돌파 단기매매다. 손절 조건을 장중에 평가하고 손절·실현손실 자체가 다음 매수 후보를 막지 않도록 한다. 임의의 고정 3% 손절·당일 강제청산은 추가하지 않는다.
- 운영은 `5c436eb02156d45aa458e244b969cf386475fc36`(PR #59). API·worker·scheduler·MCP·AI MCP 동일 이미지, `/health` 정상. 이번 수정은 `fix/intraday-exit`, `.worktrees/intraday-exit`에 있으며 운영 배포·강제 주문·운영 DB 수정은 하지 않았다. main merge는 자동배포이므로 별도 승인 필요.
- 기존 기본 checkout은 `main`/`e4b6043`와 사용자 `HANDOFF.md` 미커밋 변경을 그대로 보존했다. 중단된 새 worktree checkout만 복구했다.

## 2026-09-07 — 장중 보호 청산과 손절 이후 후보 검토
### 원인·수정
- 운영 보유 138040/180640의 일봉은 9/4에 머물렀다. 각각 `latest_at <= last_evaluated_at`/`latest_at <= entry_at` 때문에 청산 전체를 건너뛰었고 두 종목은 장중 갱신 대상인 관심종목에도 없었다.
- `automation/position_manager.py`: 일봉 상태 전이와 별개인 `evaluate_position_intraday`로 저장 손절·부분익절선 도달을 판단한다. 진입 전에 시작한 bucket 제외, 전량 손절이 앞선 부분익절보다 우선, bucket 종료 시각 기반 idempotency를 유지한다. 장중 봉을 일봉 보유일수로 세거나 일봉 trailing 커서에 쓰지 않는다.
- `automation/position_manager_service.py`: 공용 `load_completed_session_bars`로 모든 보유종목의 정규장 완료 5분봉을 읽는다. 중복·진입 이전·오래된·없는 일봉도 기존 state의 장중 보호를 막지 않는다. 신규 state는 ATR 근거가 없으면 생성하지 않는다. 일봉 부분익절보다 장중 전량 손절이 우선하며 미체결 부분익절의 기존 만료/대체 계약을 유지한다. 근거에 `evaluationHorizon`, `barPeriod`, `barSource`, `dataAsOf`를 기록한다.
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

