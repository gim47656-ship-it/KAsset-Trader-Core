# HANDOFF — KAsset-Trader-Core
갱신: 2026-08-31 (자동매매 SHADOW 기능·내부 MCP sidecar 구현 및 로컬 검증)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 안전 계약은 owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인·promotion, 주문 idempotency를 보존하는 것이다. LIVE 주문 경로와 안전장치 우회는 추가하지 않는다.

후보별 Benchmark RS는 기존 Candidate Ranker의 활성 입력으로 연결돼 순위·추천에 영향을 줄 수 있지만, PAPER 주문 안전장치는 우회하지 않는다. First Pullback/NR7/Inside Day, High-Watermark, Loss-Streak, Soft Top-K/Sector Cap은 관찰용 SHADOW evidence만 계산한다. 이 SHADOW 기능의 활성값은 모두 기본 `false`, `promotionEligible=false`이며 주문 입력으로 사용하지 않는다. MCP sidecar도 내부 구독형 AI 실행만 제공하고 주문·DB·Redis·broker 도구를 노출하지 않는다.

## 전체 진행 상태
- 로컬 `main` 구현 기준 SHA: `1238f69c5899b8e40297e188e86c4cf194ad48fb`(HANDOFF 커밋 전).
- 기준 SHA: `22fa1180a7c34446cfae1340efeda968be53b647`; 기준 대비 33파일, `+9052/-76`.
- 운영 서버는 기준 이미지/SHA 상태를 유지한다. 이번 세션에서 배포·운영 migration·LIVE 전환을 하지 않았다.
- SHADOW 계산과 evidence 저장, fingerprint 분리, 비용 포함 backtest/walk-forward 검증 완료.
- 내부 FastMCP sidecar와 `ai-mcp` compose profile 구현. 외부 포트가 없고 기본 비활성이다.
- 전체 DB integration suite만 로컬 PostgreSQL/Docker 부재로 미실행이다. DB-free 집중 검증은 완료했다.

## 이번 세션에서 한 일
- 후보별 60-session benchmark RS: KOSPI/KOSDAQ/SPY 매핑, stale/future/missing 보수 처리, 추천 evidence 저장.
- 공통 runtime/backtest Setup 판정: First Pullback과 NR7/Inside Day, 미완료·미래 봉 제외, trigger/유효기간 계산.
- 계좌 일중 High-Watermark 및 연속 손실 잠금 SHADOW 상태·evidence·영속 모델 구현.
- Soft Top-K 목표배분, 매도 우선 예산, No-Trade Band, 변경폭 상한, Sector Hard Cap, ATR 기존 상한 결합 구현.
- 신규 migration:
  - `20260831_kasset_shadow_high_watermark.py` / revision `20260831_kasset_shadow_hwm`
  - `20260831_kasset_shadow_loss_streak.py` / revision `20260831_kasset_shadow_loss_lock`
- `shadow_manifest.py`: 설정/evidence/activation schema version과 fingerprint, 전 활성 기본 `false`, `promotionEligible=false`.
- PAPER sweep 완료 로그와 AI review 거절 사유(`provider_unavailable`, `ranking_unavailable`, `action_mismatch`, `low_confidence`, `expired`)를 추가했다.
- `ai_mcp_sidecar`: bearer 인증, public health, 정확히 `run_skill` 하나, concurrency 429, 입력 schema 검증, 오류 redaction.
- 문서에 활성화 전제·롤백·clean-room 경계를 기록했다. 외부 프로젝트를 포크하거나 코드를 복사하지 않았다.

주요 변경 경로:
- 자동화: `app/extensions/kasset/automation/{benchmark_relative_strength,shadow_setups,shadow_high_watermark,shadow_loss_streak,shadow_selection,shadow_manifest}.py`
- 기존 확장점: `candidate_ranker.py`, `vertical_slice.py`, `portfolio_backtest.py`, `job.py`, `models.py`
- 시세: `app/services/daily_candles/{benchmark_fetcher,sync_service}.py`
- MCP: `app/extensions/kasset/ai_mcp_sidecar/`, `docker-compose.kasset.yml`, `.env.kasset.example`
- 테스트: 대응 `tests/extensions/kasset/**`, `tests/unit/services/daily_candles/**`
- 문서: `docs/kasset/AUTOMATION_BREAKOUT_CONTRACT.md`, `docs/kasset/AI_DUAL_PROVIDER.md`

검증 결과:
- MCP route/sidecar 집중: `68 passed`.
- 신규 자동화·benchmark·fetch/sync 집중: `129 passed`.
- Candidate Ranker/HWM/Loss/Vertical/Sync 영향 범위: `81 passed`.
- Shadow selection/setup: `42 passed`.
- PAPER consumer: `22 passed`; producer/position sizing: `40 passed`.
- Position Manager: `24 passed, 1 deselected`; runtime/backtest/manifest: `5 passed`; recommendation/held-position: `3 passed`; 빈 sweep: `1 passed`.
- `uv run ruff check .`: 통과. `uv run ruff format --check .`: 4507 files formatted. `uv run ty check`: 통과.
- 비용 비교: free final equity `96419.69200000`, paid `96331.43045924`, delta `88.26154076`, fees `85.36761726`, slippage `42.68292350`, trades `1`.
- walk-forward: folds `3`, mean test return `0E-8`, mean excess `0.00571999`, determinism hash `a22e9fba8521089b0a048f10adc94de2a7c4c91d5e9a220deb2a4fe000e03b31`.
- 전체 DB integration 시도는 `localhost:5432` 연결 `WinError 1225`로 차단됐다. 로컬 Docker/psql도 없다. 운영 DB로 대체 검증하지 않았다.

외부 참고와 라이선스:
- stock-screener Apache-2.0 `22f96f6f11b03e54037e2937a58bdb6530e67bbe`
- Lean Apache-2.0 `b692bf4788e8b54fc23bdcb5659666bf055ce89f`
- Freqtrade GPL-3.0 `5fc5faeae7033ed5a83c1eecc8160828f5ee0d2e`
- Qlib MIT `79633dd9506ea689e5400dea0197717b5b3d74b7`
- 공개 개념만 clean-room 재구현했다. 코드·테스트·상수·문구·모듈 구조·fixture를 복사하지 않았다.

## 다음 세션이 바로 할 일
1. 로컬/격리 PostgreSQL을 준비해 `tests/extensions/kasset/automation/test_vertical_slice.py tests/extensions/kasset/automation/test_job.py` 전체 DB integration을 실행한다.
2. 배포 승인을 받은 뒤에만 신규 migration을 staging/PAPER 환경에 적용한다. 운영 배포나 migration은 별도 승인 전 금지다.
3. MCP를 쓰려면 내부 네트워크와 bearer secret을 설정해 `ai-mcp` profile만 켠다. 실패 시 profile을 끄고 기존 direct→OpenRouter 경로로 롤백한다.
4. SHADOW 활성화는 동일 데이터셋 backtest/walk-forward, 새 artifact fingerprint, PAPER promotion 승인 후 별도 코드 변경으로 한다. Kill Switch/Hard Risk/promotion bypass를 우회하지 않는다.
5. 운영 PAPER의 기존 KIS token Redis lock 실패와 장중 추천→주문→fill→position→reconcile을 별도 추적한다. 신호가 없으면 무주문이 정상이다.

## 세션 이력
- 2026-08-31: Benchmark RS Ranker 연결, Setup/Risk/Portfolio SHADOW, 내부 MCP sidecar, 검증·문서 완료. 운영 미배포.
