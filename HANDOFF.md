# HANDOFF — KAsset Trader Core
갱신: 2026-08-29 (DART 운영 수집·뉴스 AI 요약·5단계 PAPER 정책·완료 세션 지수 배포)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset Trader Core는 스크리너, 시세·뉴스·DART, 전략, AI 분석, PAPER/LIVE 주문 원장과 Android API를 제공한다. 현재 목표는 **PAPER에서 검수 가능한 실제 AI 추천과 자동운용을 충분히 검증하는 것**이다. 일일 목표를 이유로 거래를 만들거나 AI가 Hard Risk를 우회하면 안 된다.

정본 운용 계약:

1. **APPROVAL**: 추천 → 사용자 승인 → PAPER 주문.
2. **AUTO_PAPER**: PAPER에서만 자동 주문. 하루 목표는 advisory-only, 하루 최대손실은 새 주문을 차단하는 hard gate다.
3. 사용자는 `riskLevel`, `operatingBudget`, `dailyTargetRatePct`, `maxDailyLossRatePct`, `killSwitch`, `currency`를 저장한다. 거래 횟수·비중·동시보유·재진입·금액 한도는 `derivedLimits`로 서버가 계산한다.
4. Hard Risk 우선순위는 **loss > budget > position > trade count > AI > execution recheck > target**다.

위험 성향 preset:

|단계|목표%|최대손실%|종목비중%|동시보유|하루 매수|하루 주문|재진입|
|---|---:|---:|---:|---:|---:|---:|---:|
|1|0.3|0.5|10|3|1|2|1|
|2|0.5|1.0|15|4|2|3|1|
|3|0.8|1.5|20|5|3|5|1|
|4|1.2|2.5|25|5|5|8|1|
|5|2.0|4.0|30|6|8|12|2|

최소 AI 확신도는 전 단계 0.50이다.

## 전체 진행 상태
- **운영 배포 완료**: `fd70defa`가 `/opt/kasset-trader-core`에 배포됐다. API/MCP/worker/scheduler를 재생성했고 `/health`는 `{"status":"ok"}`다.
- **DART 운영 키 적용**: 키는 원격 `.env.kasset`에만 있고 권한은 600이다. 값은 소스·문서·Git에 없다.
- **DART 수집 완료**: 2026-08-27~29 범위에서 3,420건 삽입, 3건 스킵, 0건 업데이트. PostgreSQL bind 상한을 넘던 upsert를 500행 청크로 나눴다.
- **DART 요약 보강**: HTML 원문이 없으면 OpenDART `document.xml` ZIP을 제한 크기로 내려받아 XML 텍스트를 추출한다. 초기 13건과 추가 batch 12건을 요약했고 원문 미제공 건은 행별 실패로 격리했다.
- **일반 뉴스 AI 요약**: 일반 뉴스는 `NewsAnalysisResult.summary`, 공시는 검증된 `NewsArticle.summary`를 API가 분리해 제공한다. 외국어 본문이 없을 때는 제목 사실만 한국어 한 문장으로 번역하며 수치 추가·추론을 금지한다. 운영 미국 뉴스 batch 20건 중 18건 성공했고 저장된 18/18 분석 요약에 한글이 있음을 확인했다.
- **AI 추천 이름·설명**: `SymbolMaster`로 이름을 보강하고 전략 투표·headline/rationale을 한국어로 생성한다. Android가 코드나 원시 영문을 이유로 표시하지 않도록 응답 계약을 맞췄다.
- **PAPER 정책 완료**: 5단계 preset, 비율→금액 파생, 목표 advisory, 손실 hard gate, 별도 kill switch가 API/저장/실행 경로에 연결됐다. 현재 운영 사용자 상태는 **2단계 / 예산 1,000,000 KRW / 목표 0.5% / 최대손실 1.0% / APPROVAL / kill switch false**다.
- **시장 지표·지수 완료**: 홈에 WTI/BTC/미국10년물을 제공한다. 홈과 지수 상세가 같은 `get_latest_completed_regular_window_from_toss` 완료 세션을 사용하고, 미국 지수는 정확한 완료 세션 날짜가 없으면 직전 세션을 최신값처럼 내리지 않는다.
- **후속 한계**: 빈 watchlist owner의 live screener bootstrap, USD owner의 US 후보·일봉 hydration, 일부 기존 strategy evidence key/dead pipeline/owner predicate 정리가 남아 있다.

## 이번 세션에서 한 일
- `market_news.py`가 일반 뉴스의 최신 영속 `NewsAnalysisResult.summary`를 한 번에 읽고, DART/SEC는 검증된 공시 요약만 쓰도록 분리했다.
- `news_summary_service.py`, 제한 batch CLI/task, 수집 직후 자동 요약을 추가했다. 한국어, 문장 수, 투자권유, 원문 복제, 원문에 없는 수치를 검증해 실패 시 저장하지 않는다.
- DART 대량 저장 청크와 OpenDART 문서 ZIP/XML fallback을 구현했다.
- `AITradingSettingsUpdate`를 위험 성향과 세 입력 중심으로 clean cutover하고 `derivedLimits`를 응답 전용으로 만들었다. 기존 금액 설정은 비율로 이관해 손실·목표 금액을 보존한다.
- 추천 이름을 producer, vertical slice, API 응답 시점에서 `SymbolMaster`로 보강하고 전략 투표를 한국어로 생성했다.
- 시장 overview/detail의 지수 현재값을 동일한 최신 완료 정규장 세션에 고정하고 비주식 지표 3종을 추가했다.
- 운영 검증: DART 3일 수집 `inserted=3420, skipped=3`; 미국 뉴스 `summarized=18, failed=2`; `/health` ok.
- 테스트: 원격 격리 DB 변경 범위 **107 passed**, 시장 **46 passed**, DART 콘텐츠 **19 passed**, 뉴스 **6 passed**, 변경 경로 `ruff check` 통과. Android 최종은 255/255 tests와 assemble 성공.
- 독립 `checker`는 Core `237ab131..fd70defa`, Android 최종 diff와 운영/실기기 증거를 검수해 `FINAL: PASS`로 판정했다. Android의 전략투표 중복·미저장 draft 안전 finding은 교정 delta까지 같은 checker가 확인해 PASS를 유지했다.

## 다음 세션이 바로 할 일
1. KRX 개장 중 현재 시세로 APPROVAL 추천 → 사용자 승인 → PAPER 주문 → fill/reconcile 한 건을 검증한다.
2. AUTO_PAPER 소액 실행에서 중복 주문, 일일 손실 hard gate, kill switch, execution recheck를 실제 주문선으로 확인한다.
3. 본문 없는 한국 뉴스는 신뢰 가능한 원문 추출 공급자를 우선 보강한다. 제목만으로 원인·전망을 발명하지 않는다.
4. DART 요약은 비용 상한 batch로 추가 backfill하고 문서 미제공·손상 ZIP 실패를 통계로 유지한다.
5. 빈 watchlist bootstrap과 USD/US hydration을 구현하되 기존 watchlist 우선순위를 보존한다.
6. PAPER 결과의 비용 포함 수익률·MDD·손실 정지·중복 주문 검증 전에는 LIVE를 열지 않는다.

## 세션 이력
- 2026-08-29: DART 3,420건 운영 수집, 문서 fallback, 일반 뉴스 AI 요약, 5단계 PAPER 정책, 완료 세션 지수 배포.
- 2026-08-29: AI PAPER vertical, live 100후보 hydration, historical 추천과 AUTO fail-closed 검증.
- 2026-08-29: 지수 quote timestamp, SEC/DART AI 요약, market news keyset, 운영 backfill.
- 2026-08-29: 기간별 candle interval·session cutover, 뉴스/공시 파이프라인과 Android 연동.
- 2026-08-28: Cloudflare LAX 우회 제거, 직접 origin 경로와 Toss/NH 시세 복구.
