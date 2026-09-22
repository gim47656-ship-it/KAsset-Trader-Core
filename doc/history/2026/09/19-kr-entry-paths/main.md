RECORD:
DATE: 2026-09-19
SCOPE: KR 진입 경로, First Pullback, NR7/인사이드 데이, PAPER 런타임, promotion evidence
PATHS: app/extensions/kasset/automation/portfolio_backtest.py, app/extensions/kasset/automation/shadow_setups.py, app/extensions/kasset/automation/vertical_slice.py, app/extensions/kasset/automation/promotion_evidence.py, tests/extensions/kasset/automation/test_portfolio_backtest.py, tests/extensions/kasset/automation/test_shadow_setups.py, tests/extensions/kasset/automation/test_vertical_slice.py, tests/extensions/kasset/automation/test_producer.py, tests/extensions/kasset/automation/test_consumer.py, tests/extensions/kasset/automation/test_job.py
STATUS: accepted

병합: PR #67 `d1283a8f0`(`2f1bd8020`)로 `main`에 병합됐다. 본문의 "배포 대기" 표기는 작성 시점 기준이다.

## 2026-09-19 — KR Breakout / First Pullback / NR7 비교 및 PAPER 런타임 연결
### 변경 계약
- `PortfolioEntryPath`는 `breakout-baseline`(기본), `first-pullback`, `nr7-inside-day` 세 arm을 닫힌 어휘로 구분한다. `run_portfolio_backtest`, `run_walk_forward`, `run_portfolio_diagnostics`는 선택한 진입 경로만 바꾸고 기존 sizing·next-open 체결·비용·포지션 상한·`position_manager` 청산을 공용으로 쓴다. 기본 호출과 명시적 `breakout-baseline` 결과·determinism hash는 같다.
- `evaluate_shadow_setup_entry`는 기존 First Pullback/NR7 component 계산을 재사용한다. First Pullback은 기존 `confirmed`와 pullback pivot stop을, NR7/Inside Day는 직전 완료 setup high를 다음 완료 봉 종가가 넘은 경우와 setup low stop을 반환한다. 비돌파 arm을 `StrategyFamily.BREAKOUT`으로 표기하지 않는다.
- `run_entry_path_comparison`은 KR 후보·동일 source/config/fold를 세 번 독립 실행한다. promotion raw payload의 additive `offlineEntryPathComparison`은 arm identity, 공통 source hash/후보/fold, 공용 risk·cost·position-manager 설정, 각 arm의 기존 backtest/walk-forward metric을 보존한다. KR 후보가 없는 US-only evidence에는 이 KR 전용 필드를 만들지 않으며, mixed evidence에는 KR 후보만 비교한다. legacy payload에서 빠진 execution contract 기본값도 비교 payload와 같은 의미로 재생한다.

### PAPER 런타임
- `vertical_slice.py`는 `KR_ONLY` 범위를 유지한 채 ranker를 통과한 KR 후보의 완료 봉에 대해 First Pullback과 NR7 detector를 평가한다. 두 경로는 Daily Setup 선정과 ORB/VWAP/RVOL 장중 돌파 trigger에 독립적으로 BUY 후보를 만들며, 기존 Breakout은 종전 Daily Setup·장중 trigger 계약을 그대로 쓴다. 미국 종목은 detector 평가 대상이 아니다.
- 한 종목에서 여러 경로가 동시에 발동해도 `breakout-baseline` → `first-pullback` → `nr7-inside-day` 순으로 합쳐 한 사이클에 추천 하나만 만든다. 합쳐진 후보도 기존 loss/account-state gate, portfolio sizing, hard-risk, BUY cooldown, AI/news 비차단 review, 추천 저장을 한 번만 통과한다. 별도 주문 실행기를 만들지 않았고 기존 PAPER consumer가 같은 `ai-rec:<recommendation_id>` 관계로 주문을 이어받는다.
- 추천 `rationale`과 `evidence.ai_vertical_slice`에는 한국어 경로명, `triggeredEntryPaths`, detector별 근거, reference/stop/유효시각을 남긴다. detector-only 추천은 Breakout 전략군·투표를 추천 evidence와 AI payload에 싣지 않으며, 실제 `breakout-baseline`이 함께 발동한 경우에만 기존 Breakout family/votes를 유지한다. 따라서 기존 추천·주문 상세와 그 추천에서 이어진 포지션·청산·손익 조회 경로가 같은 추천 ID와 진입 경로 근거를 보존한다. 앱/API 스키마를 바꾸지 않았다.

### 범위·검증 상태
- 설정·migration·scheduler·threshold·`same_time_rvol`·Toss/live·UI·운영 DB/상태는 변경하지 않았다. 배포와 강제 sweep, 수동 PAPER 주문도 수행하지 않았다.
- Windows 로컬 Python, Ruff, ty, formatter, project-wide suite는 실행하지 않았다. 서버의 `/tmp/kasset-entry-path-20260919` 전용 checkout과 일회성 2 CPU/3 GiB container, 운영과 분리된 `kasset-test-db`의 실행별 DB에서 아래 집중 명령을 실행했다. 운영 checkout `/opt/kasset-trader-core`, `.env.kasset`, 운영 DB/볼륨은 사용하거나 수정하지 않았다.
  ```text
  python -m pytest tests/extensions/kasset/automation/test_portfolio_backtest.py tests/extensions/kasset/automation/test_shadow_setups.py tests/extensions/kasset/automation/test_vertical_slice.py tests/extensions/kasset/automation/test_producer.py tests/extensions/kasset/automation/test_consumer.py tests/extensions/kasset/automation/test_job.py -q --tb=short -p no:cacheprovider
  ```
- 최종 formatted source 재검증 결과는 **199 passed / 12 warnings / 257.84s, exit 0**다. schema bootstrap은 실행별 DB 1개, socket guard·외부 HTTP 차단은 모두 0건이었다. 경고 12건은 기존 OpenDartReader `SyntaxWarning`이며 원문은 `local://.kasset-entry-path-final-pytest.log`다.
- F1 attribution closure는 새 `/tmp/kasset-entry-path-rework` checkout에서 `python -m pytest tests/extensions/kasset/automation/test_vertical_slice.py tests/extensions/kasset/automation/test_producer.py -q --tb=short -p no:cacheprovider`를 다시 실행해 **46 passed / 14.71s, exit 0**을 확인했다. schema bootstrap DB 1개, socket guard·외부 HTTP 차단은 0건이며 원문은 `local://.kasset-entry-path-rework-pytest.log`다.
- 같은 최종 source와 exact 9 Python 파일에서 `uv run ruff check`와 `uv run ruff format --check`, exact 5 source 파일에서 `uv run ty check --error-on-warning`을 실행했고 세 명령 모두 **exit 0**이다. 원문은 `local://.kasset-entry-path-ruff-check.log`, `local://.kasset-entry-path-ruff-format-check.log`, `local://.kasset-entry-path-ty-check.log`, 상태 요약은 `local://.kasset-entry-path-static-status.txt`다.
- 배포 후에는 다음 자연 KRX PAPER 사이클을 기다린다. 앱의 추천·주문 상세에서 한국어 경로 사유와 동일 추천 관계를 확인하고, 체결이 생긴 경우에만 그 포지션의 자연 청산·손익까지 같은 추천 ID로 추적한다. 검증을 위해 주문을 제조하지 않는다.
