# HANDOFF — KAsset-Trader-Core
갱신: 2026-08-31 (국내 스크리너 KRX 세션 만료 fallback을 운영 PAPER에 배포하고 장중 추천 흐름 검증)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 안전 계약은 owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인·promotion, 주문 idempotency를 보존하는 것이다. LIVE 주문 경로와 안전장치 우회는 추가하지 않는다.

후보별 Benchmark RS는 기존 Candidate Ranker의 활성 입력으로 연결돼 순위·추천에 영향을 줄 수 있다. First Pullback/NR7/Inside Day, High-Watermark, Loss-Streak, Soft Top-K/Sector Cap은 관찰용 SHADOW evidence만 계산한다. 이 SHADOW 기능의 활성값은 모두 기본 `false`, `promotionEligible=false`이며 주문 입력으로 사용하지 않는다. AI MCP sidecar도 내부 구독형 AI 실행만 제공하고 주문·DB·Redis·broker 도구를 노출하지 않는다.

## 전체 진행 상태
- Core 운영 배포 SHA: `1d3f6b20e1c217088fed91b83230604770310d40`(이 HANDOFF 커밋 전), `origin/main`과 일치.
- 운영 이미지: `kasset-trader-core:1d3f6b20`, image id `sha256:4b335ecb28190df1ff5deec49d4c8d08893270667cd8fe355bf80eb837b96dca`.
- 운영 migration은 `20260831_kasset_shadow_loss_lock (head)`까지 적용됐다.
- API, worker, scheduler, 거래 MCP, AI MCP가 `1d3f6b20` 이미지로 기동됐고 public API health가 정상이다.
- 운영은 `TRADING_ENABLED=true`, `LIVE_TRADING_ENABLED=false`, `AI_PAPER_AUTO_EXECUTION_ENABLED=true`, 모든 owner runtime은 `PAPER`다. 배포 과정에서 Kill Switch, Hard Risk, promotion bypass를 바꾸지 않았다.
- SHADOW 계산/evidence/fingerprint 분리와 비용 포함 backtest/walk-forward 검증은 완료했다. 신규 SHADOW 기능은 활성화하지 않았다.

## 이번 세션에서 한 일
- 관리자 AI 탭의 실제 집계 함수 `build_ops_dashboard()`를 운영 DB에서 실행해 AI 연결, 자동매매 funnel, runtime 안전 설정을 원문으로 확인했다.
- 국내 스크리너의 TradingView 조회 성공 뒤 KRX 공개 세션 만료 때문에 정규화 전체가 실패하던 문제를 고쳤다. KRX 세션이 만료되면 운영 DB의 활성 `kr_symbol_universe`로 종목 코드·이름·시장만 검증하고 TradingView 시세 결과는 유지한다.
- 후보별 60-session Benchmark RS: KOSPI/KOSDAQ/SPY 매핑, stale/future/missing 보수 처리, 추천 evidence 저장.
- KOSPI와 KOSDAQ 모두 일봉 writer/backfill에 연결했다. 후보별 benchmark와 시장 보고용 benchmark를 분리해 scale 혼합을 차단했다.
- 공통 runtime/backtest Setup 판정: First Pullback과 NR7/Inside Day, 미완료·미래 봉 제외, trigger/유효기간 계산.
- 계좌 일중 High-Watermark 및 연속 손실 잠금 SHADOW 상태·evidence·영속 모델 구현.
- Soft Top-K 목표배분, 매도 우선 예산, No-Trade Band, 변경폭 상한, Sector Hard Cap, ATR 기존 상한 결합 구현.
- `shadow_manifest.py`: 설정/evidence/activation schema version과 fingerprint, 전 활성 기본 `false`, `promotionEligible=false`.
- PAPER sweep 완료 로그와 후보 수·시장·source·ranked/actionable 수, AI review 거절 사유를 추가했다.
- `ai_mcp_sidecar`: bearer 인증, public health, 정확히 `run_skill` 하나, concurrency 429, 입력 schema 검증, 오류 redaction.
- DAY/PRE/AFTER 시세 등락 기준을 가장 최근 완료 정규장 종가로 고정했다. 정규장 종가가 없으면 등락값을 만들지 않는 fail-closed 계약이다.
- 신규 migration:
  - `alembic/versions/20260831_kasset_shadow_high_watermark.py` / revision `20260831_kasset_shadow_hwm`
  - `alembic/versions/20260831_kasset_shadow_loss_streak.py` / revision `20260831_kasset_shadow_loss_lock`

주요 변경 경로:
- 자동화: `app/extensions/kasset/automation/{benchmark_relative_strength,shadow_setups,shadow_high_watermark,shadow_loss_streak,shadow_selection,shadow_manifest}.py`, `candidate_ranker.py`, `vertical_slice.py`, `portfolio_backtest.py`, `strategy_promotion_service.py`, `job.py`
- 시세: `app/services/daily_candles/{benchmark_fetcher,sync_service}.py`, `scripts/backfill_daily_candles.py`, `app/extensions/kasset/api/krx_quotes.py`
- MCP: `app/extensions/kasset/ai_mcp_sidecar/`, `app/extensions/kasset/ai/mcp_provider.py`, `docker-compose.kasset.yml`, `.env.kasset.example`
- 모델/migration: `app/extensions/kasset/models.py`, 위 Alembic 파일 2개
- 테스트: 대응 `tests/extensions/kasset/**`, `tests/unit/services/daily_candles/**`
- 문서: `docs/kasset/AUTOMATION_BREAKOUT_CONTRACT.md`, `docs/kasset/AI_DUAL_PROVIDER.md`

검증 결과:
- 서버 격리 전체 automation 회귀: `361 passed, 13 warnings in 233.26s`. 테스트 컨테이너는 `--network container:kasset-trader-db-1`과 실행 전용 `test_db`만 사용했고 운영 DB에는 테스트를 실행하지 않았다.
- 최종 benchmark/promotion 집중: `55 passed`. 앞선 집중 검증: `232 passed`, `211 passed`.
- `uv run ruff check ...`, `uv run ruff format --check ...`, `uv run ty check`: 모두 통과.
- 국내 스크리너 집중 테스트 `52 passed in 52.98s`; 변경 파일 `ruff check`, `ruff format --check`, 전체 `ty check` 통과.
- Android 계약/앱 검증은 Android HANDOFF에 기록한다.
- 비용 비교: free final equity `96419.69200000`, paid `96331.43045924`, delta `88.26154076`, fees `85.36761726`, slippage `42.68292350`, trades `1`.
- walk-forward: folds `3`, mean test return `0E-8`, mean excess `0.00571999`, determinism hash `a22e9fba8521089b0a048f10adc94de2a7c4c91d5e9a220deb2a4fe000e03b31`.
- Shadow 기능 on/off의 주문 결과 동일성을 회귀 테스트로 확인했다. SHADOW evidence는 주문 수량·action·Hard Risk 판정 입력이 아니다.
- 최종 독립 검수: `FINAL: PASS`. KOSDAQ writer, 후보별 benchmark backtest, fingerprint, readiness 61 bars, 보고용/랭킹용 benchmark 분리까지 재검수했다.

운영 배포·실측:
- `1d3f6b20` 배포 뒤 운영 `ScreenerService.refresh_screening(market="kr", limit=10)`은 TradingView source로 국내 종목 150개를 읽고 삼성전자·대우건설 등 10개를 정상 반환했다. 배포 전 같은 호출은 `krx_session_expired`, 0개였다.
- 배포 전 DB backup: `/root/backups/kasset-daily/kasset-20260831T050324Z.dump.gz`, 4,611,593 bytes, SHA-256 `578e1454e2fc269ae66141a318425e3453d7087ce059cf354d6657d77791fa19`; `gzip -t`와 `pg_restore --list` 통과.
- KOSPI/KOSDAQ benchmark-only 백필: 각 400행, `2025-01-07`~`2026-08-28`, source `naver`. 현재 운영 범위는 Toss와 NH PLUG이며 KIS는 의도적으로 미사용이므로, KIS OAuth 403은 장애로 취급하지 않고 Naver fallback으로 완료했다.
- Public `/health`: `{"status":"ok"}`. `/api/v1/system/status`: AI configured/available, DB head 정상, `PAPER`, LIVE false.
- AI MCP: 내부 `/health` 200, 무인증 POST `/mcp` 401, 인증된 실제 `run_skill` 호출 `{"ok": true}`.
- 05:10 UTC 실제 추천 cycle: owners=1, candidates=100(KR 94/US 6), ranked=99, actionable=1. MCP가 terra/sol review를 실행했지만 `action_mismatch=1`이라 reviewed=0, recommendations=0, `no_ai_confirmed_signal`로 종료했다.
- 05:50 UTC 수동 자연 주기 검증도 candidates=100(KR 94/US 6), ranked=99, actionable=1, AI available이었다. AI가 전략 action과 불일치해 `action_mismatch=1`, recommendations=0으로 끝났고 이어 실행한 PAPER consumer는 owners=0/outcomes=0이었다.
- 05:52 UTC 관리자 AI 탭은 최근 24시간 AI 요청 22건/제공사 시도 22건/실패 0건, 추천 0건, PAPER 주문 0건을 표시했다. AUTO_PAPER·거래 기능·AI 자동실행은 켜짐, LIVE와 모든 Kill Switch는 꺼짐 상태다.
- 같은 배포 이후 새 추천 0, execution attempt 0, PAPER order 0. Kill Switch나 Hard Risk에서 거절된 것이 아니라 AI 확인 신호가 없어서 무주문이었다.
- 실제 TQQQ DAY_MARKET 응답은 정규장 종가 `71.89`를 기준으로 현재가 `71.36`, `-0.53`, `-0.74%`를 반환했다. KRX 정규장도 기존 전일 종가 기준을 유지했다.

외부 참고와 라이선스:
- First Pullback, NR7/Inside Day, High-Watermark, 손절 보호, Soft Top-K의 공개 개념만 clean-room 재구현했다.
- 참고 snapshot: stock-screener Apache-2.0 `22f96f6f11b03e54037e2937a58bdb6530e67bbe`, Lean Apache-2.0 `b692bf4788e8b54fc23bdcb5659666bf055ce89f`, Freqtrade GPL-3.0 `5fc5faeae7033ed5a83c1eecc8160828f5ee0d2e`, Qlib MIT `79633dd9506ea689e5400dea0197717b5b3d74b7`.
- 외부 코드·테스트·상수·문구·모듈 구조·fixture를 복사하지 않았다. GPL 코드도 포함하지 않았다.

## 다음 세션이 바로 할 일
1. 현재 운영 브로커·시세 범위는 Toss와 NH PLUG다. KIS는 의도적으로 설정하지 않았으며, 사용자가 범위를 바꾸기 전에는 KIS OAuth/account 복구 작업을 하지 않는다.
2. 다음 자연 발생 전략 신호에서 추천→AI 확인→PAPER order→fill→position→reconcile을 같은 trace로 확인한다. 현재 무주문 원인은 주문 차단이 아니라 전략 BUY와 AI verdict action 불일치다. 강제 BUY, 임계값 완화, Kill Switch/Hard Risk/promotion 우회는 금지다.
3. SHADOW 기능 활성화나 promotion은 이번 배포에 포함하지 않았다. 동일 데이터셋 backtest/walk-forward, 새 artifact fingerprint와 별도 PAPER promotion 승인을 거쳐야 한다.
4. 백필 CLI는 이미지에서 `cd /app && /app/.venv/bin/python -m scripts.backfill_daily_candles ...`로 실행한다. 파일 경로 직접 실행은 `sys.path` 때문에 `ModuleNotFoundError: app`이 난다.
5. 롤백: `/opt/kasset-trader-core/.env.kasset.pre-1d3f6b20`을 복원해 image tag/VCS ref를 `c9a24ee5`로 돌리고 canonical Compose의 `ai-mcp` profile을 포함해 재기동한다. DB 변경은 없으며 downgrade나 DB 삭제는 별도 승인 없이는 하지 않는다.

## 세션 이력
- 2026-08-31: 국내 스크리너 KRX 세션 만료 fallback `1d3f6b20` 배포, 운영 150종목 복구, 장중 추천·AI·PAPER consumer 실측.
- 2026-08-31: Core `c9a24ee5`와 migration/AI MCP를 운영 PAPER에 배포, benchmark 백필, 실제 시세·추천 cycle 검증.
- 2026-08-31: Benchmark RS Ranker 연결, Setup/Risk/Portfolio SHADOW, 내부 MCP sidecar 구현.
