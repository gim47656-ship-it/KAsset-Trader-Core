# HANDOFF — KAsset Trader Core
갱신: 2026-08-29 (결정론적 PAPER 자동화·승격 gate, 추천 시장·일일 횟수, AI 공급자 경로 코드 완료)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset Trader Core는 스크리너, 시세·뉴스·공시, 전략, AI 분석, PAPER/LIVE 주문 원장과 Android API를 제공한다. 현재 목표는 **PAPER에서 검수 가능한 실제 AI 추천·주문·청산을 충분히 검증한 뒤 별도 승인으로 LIVE를 검토**하는 것이다. 일일 목표를 이유로 거래를 만들거나 AI가 Hard Risk를 우회하면 안 된다.

정본 운용 계약:

1. **APPROVAL**: 추천 → 사용자 승인 → PAPER 주문.
2. **AUTO_PAPER**: exact strategy/version `PAPER_APPROVED` 승격과 submit 직전 재검증을 통과한 PAPER 주문만 자동 실행한다. LIVE 경로는 별도 승인 전까지 열지 않는다.
3. 사용자는 `riskLevel`, `operatingBudget`, `dailyTargetRatePct`, `maxDailyLossRatePct`, `customMaxBuysPerDay`, `customMaxSellsPerDay`, `killSwitch`, `currency`를 저장한다. 비중·동시보유·재진입·사용자 횟수 상한은 `derivedLimits`로 서버가 계산한다.
4. 추천 범위 `KR_ONLY|US_ONLY|KR_US`는 후보만 필터링한다. PAPER 주문은 설정 통화·예산·보유량·Hard Risk에서 다시 제한한다.
5. Hard Risk 우선순위는 **loss > budget > position > order count > AI > execution recheck > target**다. 최대손실은 신규 매수를 막되 위험을 줄이는 SELL은 그 조건만으로 막지 않는다.

위험 성향 기본 `매수/매도/전체 주문`:

|단계|목표%|최대손실%|종목비중%|동시보유|하루 매수|하루 매도|하루 주문|재진입|
|---|---:|---:|---:|---:|---:|---:|---:|---:|
|1|0.3|0.5|10|3|1|1|2|1|
|2|0.5|1.0|15|4|2|1|3|1|
|3|0.8|1.5|20|5|3|2|5|1|
|4|1.2|2.5|25|5|5|3|8|1|
|5|2.0|4.0|30|6|8|4|12|2|

최소 AI 확신도는 전 단계 0.50이다.

## 전체 진행 상태
- **코드 완료·push — `39319729`**: 결정론적 candidate ranker, ATR/regime/liquidity/lot 포지션 크기, 보유 포지션 청산, next-bar portfolio backtest와 walk-forward·비용 stress·regime/period·turnover·counterfactual 진단을 구현했다.
- **승격 gate 완료**: AUTO_PAPER는 recommendation의 단일 strategy identity와 전역 exact-version `PAPER_APPROVED` 행이 일치해야 선정·claim된다. submit 경계는 승격 행을 잠그고 다시 검사한다. 자동 seed는 없다.
- **추천 시장·일일 횟수 완료**: owner별 `KR_ONLY|US_ONLY|KR_US`를 후보에 적용하고 기존 보유는 범위 밖이어도 계속 관리한다. 기본 매수·전체 주문 수는 이전 운영값을 보존하며 사용자가 side별 서버 상한 안에서 횟수를 정할 수 있다.
- **AI 공급자 경로 완료**: 복잡한 검토는 MCP → direct API → OpenRouter, 뉴스·공시 요약은 direct API → OpenRouter다. 미설정·전송·timeout·5xx만 fallback하고 4xx·거절·형식·스키마·안전 실패는 닫는다.
- **뉴스 원문 경계 보강**: Google News redirect마다 공개 IP·robots·응답 크기를 검사한다. RSS description은 실제 본문 요약으로 저장하지 않는다.
- **Android 대응 완료·push — `66a739b4`**: 시장 범위 3분할 선택과 하루 매수·매도 스테퍼가 같은 wire 계약을 사용한다.
- **운영 미배포**: 이번 코드와 `20260829_kasset_*` 세 마이그레이션은 운영에 적용하지 않았다. 운영 상태는 이전 `fd70defa`다.
- **검증 유예**: 사용자의 요청으로 Android 실기기 화면 검증은 미뤘다. 외부 provider 실호출, 운영 252봉 충족, 장중 PAPER 체결도 미검증이다.
- **의도적 차단**: promotion 등록·승격 운영 도구가 아직 없으므로 AUTO_PAPER는 `strategy_promotion_required`로 닫힌다. 검증 증거와 사용자 결정 없이 DB 행을 자동 생성하지 않는다.

## 이번 세션에서 한 일
- 추천 후보를 50~100개까지 결정론적으로 순위화하고 hard exclusion, factor evidence, 최신성·lookahead 경계를 추가했다.
- 전략 합의와 AI review 뒤 포지션 크기를 위험예산·종목비중·owner 예산·평균 거래량·거래대금 중 최솟값으로 계산하고 시장 lot으로 내린다. SELL은 실제 보유량을 넘지 않는다.
- position manager를 신규 후보보다 먼저 실행해 partial/trailing/gap/time/trend 청산 추천을 만든다. 데이터 예외는 owner/market/symbol/예외 타입을 warning으로 남긴다.
- portfolio backtest는 완성 봉으로 신호를 만들고 다음 봉 시가로 체결한다. 수수료·slippage·walk-forward·1x/2x/3x 비용 stress·종목 제거·추가 지연 진단과 deterministic hash를 제공한다.
- AUTO_PAPER의 선정·claim·submit 전 exact-version promotion을 검사하고 row lock으로 suspension race를 막았다. kill switch·owner scope·idempotency·PAPER facade를 유지했다.
- `recommendationMarketScope`, `customMaxBuysPerDay`, `customMaxSellsPerDay`, `maxSellsPerDay`, `maxCustom*`, `sellsToday` API·DB·Android 계약을 연결했다.
- 기존 기본 주문 수보다 높아졌던 중간 구현을 되돌려 1단계 `1/1/2`부터 5단계 `8/4/12`까지 매수·전체 상한을 보존했다. 일일 최대손실은 위험 감소 SELL만 예외로 한다.
- AI route를 feature별로 분리하고 strict JSON 검증·availability-only fallback·MCP URL/세션 경계를 추가했다. 폐기 설정 별칭을 제거했다.
- 검증: 격리 PostgreSQL 집중 스위트 **388 passed**, formatter 뒤 position manager **10 passed**, 변경 범위 `ruff`·`ty` 통과, `alembic heads`는 `20260829_kasset_strategy_promotion` 단일 head다. Android는 **277 tests / 0 failures**와 assemble 성공.
- 독립 `checker` 최초 REWORK의 F2~F6과 SELL 손실 gate를 교정했다. F1 “US 추천 소멸”은 producer가 BUY와 recommendation ID를 유지하고 Android가 리스크 차단으로 표시함을 코드·테스트로 반증했다. 같은 checker 최종 판정은 `FINAL: PASS`다.
- 커밋·push: Core/API 계약 `39319729`, Android/API 계약 `66a739b4`. 운영 배포·provider 실호출·실기기 검증은 하지 않았다.

## 다음 세션이 바로 할 일
1. 사용자 승인 후 `39319729`와 Alembic head `20260829_kasset_strategy_promotion`을 운영 배포한다. 적용 전 DB backup과 현재 head를 확인한다.
2. 실제 MCP initialize/tools-call/SSE/세션 종료와 direct API/OpenRouter를 소량 호출해 fallback·429 fail-closed를 확인한다.
3. 운영 `kr_candles_1d`·`us_candles_1d`에서 후보별 252봉을 확인한다. 부족하면 데이터를 보강하고 minimum을 임의로 낮추지 않는다.
4. backtest evidence hash·threshold를 독립 검수한 뒤 strategy promotion을 명시적으로 등록·승격하는 운영 경로를 만든다. 자동 seed하지 않는다.
5. S24+에서 시장 범위·루틴·일일 횟수 UI 저장 왕복을 확인한다.
6. KRX 개장 중 APPROVAL 추천→승인→PAPER fill/reconcile을 검증한 뒤, AUTO_PAPER는 승격 후 소액으로 중복 주문·손실 정지·kill switch·청산을 확인한다.
7. 2단계 기본 매도 1건이 동시 청산을 막는지 실측하고 필요하면 `customMaxSellsPerDay`를 사용자 의도에 맞게 올린다.

## 세션 이력
- 2026-08-29: 결정론적 PAPER 자동화·exact-version 승격 gate, 추천 시장·일일 횟수, AI 공급자·뉴스 경계 완료.
- 2026-08-29: DART 3,420건 운영 수집, 문서 fallback, 일반 뉴스 AI 요약, 5단계 PAPER 정책, 완료 세션 지수 배포.
- 2026-08-29: AI PAPER vertical, live 100후보 hydration, historical 추천과 AUTO fail-closed 검증.
- 2026-08-29: 기간별 candle interval·session cutover, 뉴스/공시 파이프라인과 Android 연동.
- 2026-08-28: Cloudflare LAX 우회 제거, 직접 origin 경로와 Toss/NH 시세 복구.
