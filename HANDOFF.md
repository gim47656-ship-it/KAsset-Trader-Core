# HANDOFF — KAsset Trader Core
갱신: 2026-08-29 (AI PAPER vertical 운영 배포·historical smoke·독립검수 완료)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset Trader Core는 스크리너, 시세·뉴스·DART, 전략, AI 분석, PAPER/LIVE 주문 원장과 Android API를 제공한다. 현재 목표는 **PAPER에서 사용자가 검수 가능한 실제 AI 추천과 자동운용**이다. 일일 목표를 이유로 거래를 만들거나 Hard Risk를 AI가 우회하면 안 된다.

정본 운용 모드:

1. **APPROVAL**: 연속 스캔 → 신규 종목 발굴 → AI·전략 종합 추천 → 사용자 승인 → PAPER 주문.
2. **AUTO_PAPER**: 사용자가 운용예산·보수적 일일 목표·일일 최대손실·최대 거래수·종목당 비중·최대 동시보유를 정한다. AI가 발굴부터 매도까지 맡지만 목표수익은 advisory-only다.

정본 아키텍처:

```text
Existing Screeners → 50–100 candidates
→ Strategy Skills (Momentum / Breakout / MeanReversion / VolatilityTrend / additional)
→ Market Regime → Dynamic Strategy Ensemble
→ AI + News + DART → Candidate Ranking → Portfolio Manager
→ Budget / Loss / Trade-count / Weight Risk Gate
→ APPROVAL 또는 AUTO_PAPER → PAPER → validation → LIVE
```

정본 수치 계약:

- 전략: `MomentumStrategy`, `MeanReversionStrategy`, `BreakoutStrategy`, `VolatilityTrendStrategy`.
- ATR: `risk=max(ATR×1.5, price×1%)`; BUY stop=`entry-risk`, target=`entry+2×risk`.
- Backtest: next-bar 체결, fee 0.1%, slippage 0.05%, win rate·MDD·total return.
- Hard Risk 우선순위: **loss > budget > position > trade count > AI > target**.
- daily goal은 참고값이다. 이를 달성하기 위한 강제 주문·risk 완화는 금지한다.

## 전체 진행 상태
- **운영 배포 완료**: Core `main`의 최종 배포 commit은 `237ab131 fix: hydrate AI screener candidates`다. 선행 vertical commit은 `e94ae620 feat: add AI paper trading vertical`이다. `/opt/kasset-trader-core`에 배포했고 `/health`는 `status=ok`다.
- **실제 후보 발굴 완료**: 사용자 4의 live tvscreener 후보 100개를 수집하고 Toss 일봉 부족분 96개를 concurrency 6으로 보강했다. 전략평가 100/100이다.
- **AI 추천 완료**: 주말 현재시각 실행은 최신 일봉 2026-08-28이 전략 1일 freshness를 넘어서 정상 HOLD였다. 사용자가 확인한 대로 주말 실제 PAPER 주문은 불가능하므로 `2026-08-28T06:00Z` historical run을 사용했다. 후보 100, 전략평가 100, AI 검토 6, AI 실패 0, `VOLATILE` regime, 114.46초였다.
- **실제 검수 가능 추천**: `rec-2098fc28-4d1d-4af1-85c6-e7f2a8e961ab`, `003550`, BUY. Momentum BUY 0.95, MeanReversion HOLD, Breakout HOLD, VolatilityTrend BUY 0.95; regime weights 0.15/0.20/0.25/0.40; entry 114,700, stop 106,492.857143, target 131,114.285714; AI `gpt-5.6-sol`, confidence 0.64, bullish/bearish 64/36, risk HIGH; ranking 1/100, portfolio targetWeight 0.20, quantity 17.
- **AUTO_PAPER fail-closed 확인**: historical AUTO run이 위 추천을 claim하고 실행 직전 risk preview를 다시 계산했다. 현재/주말 PAPER 제약에서 `REJECTED`, `risk_preview_rejected:POSITION`, execution 0건으로 종료됐다. 추천은 `APPROVED/FAILED` 이력으로 남아 상세 API에서 검수 가능하다. 억지 주문이나 우회는 없었다.
- **운영 상태 복원**: API state는 APPROVAL/PAPER, budget 1,000,000 KRW, daily goal 5,000, max daily loss 10,000, max buys/orders 1, max allocation 20%, max holdings 2, max reentry 1, kill switch false다. `AI_PAPER_AUTO_EXECUTION_ENABLED=True`이나 사용자 mode가 APPROVAL이므로 자동 claim하지 않는다.
- **market event/공시**: `KASSET_MARKET_EVENTS_ENABLED=True`; SEC/DART AI backfill 11/11과 API 응답까지 완료했다. 실기기 Android 확인만 ADB offline으로 차단됐다.
- **후속 한계**: watchlist가 빈 owner는 `and ordered` guard 때문에 live tvscreener bootstrap을 못 한다. USD owner의 US 후보·일봉 hydration이 없다. strategy-level evidence의 weight/score가 enum name/value mismatch로 null일 수 있으나 상위 `strategyVotes`는 정상이다. 구 `MarketEventPipeline` dead code가 남았다. `load_paper_orders`는 owner-sourced recommendation ID를 쓰지만 명시 owner 술어가 없다.

Skill·외부 탐색 정책:

1. 저장소에 이미 있는 검증된 전략·서비스를 우선 재사용하고, 두 번째 병렬 구현을 만들지 않는다.
2. 공식 벤더/논문 또는 실제 코드·테스트·license가 확인되는 유지보수 오픈소스를 다음으로 검토한다.
3. GitHub/X 발견물은 **80/100 이상**만 이식 후보로 감시한다: 코드 공개·재현 가능성 20, 테스트/backtest 20, PAPER·Hard Risk 호환 20, 데이터 출처·freshness 15, 유지보수·license 10, secret/remote-exec 없는 통합 안전성 15. 점수 통과 후에도 sandbox, 비용 포함 backtest, 기존 계약 회귀 검증 전에는 자동 이식하지 않는다.

## 이번 세션에서 한 일
- `AiTradingService`, `AiTradingSettingsService`, recommendation/state API, producer task, automation task, Android-facing evidence payload를 연결했다.
- 기존 네 전략, regime별 dynamic ensemble, ATR stop/target, news/DART AI evidence, ranking, portfolio sizing, 6단계 Hard Risk를 하나의 추천 상세로 만들었다.
- tvscreener 후보를 실제 전략 입력으로 사용하고 부족한 일봉만 Toss에서 보강하도록 고쳤다. `CandidateRanking.total` 필수 필드 누락 회귀도 수정했다.
- focused validation: AI 관련 91 passed, ranking 회귀 1 passed, changed-path `ruff check` 통과. Android 전체 `:app:testDebugUnitTest :app:assembleDebug`도 BUILD SUCCESSFUL(44 tasks).
- 광범위 Core suite: 24,196 passed, 90 failed, 43 errors, 45 skipped, exit 1. 실패는 기존/환경 범위이며 이번 focused AI suite는 green이다. 이 숫자를 전체 통과로 표현하면 안 된다.
- final Diff와 검증 증거를 독립 `checker`가 1회 검수했고 `FINAL: PASS`, `OWNER: MAIN`으로 판정했다. 위 2개 MEDIUM(빈 watchlist, USD)과 3개 LOW(evidence key, dead pipeline, owner 술어)는 비차단 후속으로 `ACCEPTED`했다.

## 다음 세션이 바로 할 일
1. KRX 개장 중 APPROVAL 추천을 새로 생성하고 사용자 승인 → PAPER order → fill/reconcile까지 한 건을 실시간 시세로 확인한다. 이어 AUTO_PAPER 한 건을 별도 소액 한도로 확인한다.
2. 빈 watchlist owner도 50–100 live candidates에서 시작하도록 bootstrap guard를 제거하되 기존 사용자 watchlist 우선순위는 보존한다.
3. USD owner용 US screener와 daily candle hydration을 구현하거나 설정 UI에서 KRW-only 제한을 명시한다.
4. `StrategyVoteEvidence` weight/score key를 enum value 기준으로 고치고 payload 회귀 테스트를 추가한다.
5. dead `MarketEventPipeline`을 clean cutover로 제거하고 `load_paper_orders`에 명시 owner predicate를 추가한다.
6. Android S24+가 다시 온라인이면 AI픽 상세와 공시 backfill을 시각 검수하고 `adb -s 100.90.45.34:39259 shell cmd window user-rotation free`로 회전을 복원한다.
7. PAPER 결과의 비용 포함 수익률·MDD·일일 손실·중복 주문을 충분히 검증한 뒤에만 사용자의 별도 승인으로 LIVE를 검토한다.

## 세션 이력
- 2026-08-29: AI PAPER vertical 배포, live 100후보 hydration, historical 실제 추천과 AUTO fail-closed 검증. `e94ae620`, `237ab131`.
- 2026-08-29: 지수 quote timestamp, SEC/DART AI 요약, market news keyset, 운영 backfill 11/11.
- 2026-08-29: 기간별 candle interval·session cutover, news/disclosure 파이프라인과 Android 연동.
- 2026-08-28: Cloudflare LAX 우회 제거, 직접 origin 경로와 Toss/NH 시세 복구.
