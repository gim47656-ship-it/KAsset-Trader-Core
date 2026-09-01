# HANDOFF — KAsset-Trader-Core
갱신: 2026-09-01 (KIS 없는 미국 장중봉 fallback·뉴스 timezone 경계 수정)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 운영 범위는 Toss와 NH PLUG이며 KIS 미설정은 의도된 상태다. owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인·promotion, 주문 idempotency를 보존하고 LIVE 주문 경로나 안전장치 우회를 만들지 않는다.

## 전체 진행 상태
- 최신 실행 코드: 종목별 readiness PR #19 merge `63e50518d8650744b1159c34cf8909a2bbaf0140`, 비동기 KR/US benchmark window PR #20 merge `8a6988731cdf119a178abeb77771405dd7e0ddfc`.
- 운영 이미지: `kasset-trader-core:8a698873`, digest `sha256:ea22ca5a88652f5829015f7b1298242dc954cf492d05aacd33c0b18b93aab978`. `/health`는 `{"status":"ok"}`이며 API/MCP/AI MCP/worker/scheduler가 모두 기동 중이다.
- migration `20260901_kasset_fcm_push` 적용 완료. FCM service-account는 운영 `.env.kasset`에 base64 secret으로만 저장했고 원본 임시 파일은 삭제했다. 저장소에는 secret이 없다.
- Android 실기기 활성 FCM 토큰 1건. 서버 제어 테스트는 FCM HTTP v1 `SENT`, HTTP 200이었고 SM-S926N에서 알림 표시와 알림 탭 진입을 확인했다.
- Toss 1분봉은 전체 3,938종목을 분당 20종목씩 순환하며 회당 최대 200봉을 영속화한다. 2026-09-01 운영 스모크는 20종목 중 18종목 성공, 3,600행 upsert. 2종목은 Toss가 수집 기준보다 2분 미래 봉을 반환해 fail-closed 됐다.
- KR 일봉 backfill 102/102, US 일봉 backfill 102/103 성공. readiness는 `promotionReady=true`, 총 197종목 적격이다. KR은 99/100이며 `0126Z0` 252봉 미달, US는 98/100이며 `SPCX` 252봉 미달과 `SCCO` expected trading day 누락만 제외했다.
- `forward_paper` backtest run 1은 정상 완료됐다. candidate 1은 `threshold_failed:excess_return`으로 `non_promotable`: total return `0.12023905`, excess return `-0.86947757`, profit factor `2.0465`, max drawdown `0.0469416`, walk-forward pass rate `0.25`.
- `promotion-status`는 `promotions=[]`. PAPER 자동화 스모크는 `enabled=true`, `owners=0`, `outcomes=[]`; 성과 임계치 미달 후보를 우회하지 않아 주문이 생성되지 않는 것이 정상이다.
- CI 병목 개선 PR #22 merge `7150232f55f16bc6ff7389777f5f75017bd521d8`. production trading code·DB schema·운영 배포는 변경하지 않았다.
- 미국 장중 OHLCV는 기존 DB/KIS reader를 우선 유지하고, 예외 또는 빈 결과일 때 운영 중인 Toss 1분봉·집계 경로로 fallback한다. Toss 성공 시 provenance는 `source=toss`다.
- `news_articles.article_published_at`의 naive 저장 계약에 맞춰 AI cycle 뉴스 health cutoff를 naive UTC로 바꿨다. DB schema·기존 writer·shadow-only 비관문 성격은 유지한다.

## 이번 세션에서 한 일
- 2026-09-01 10:13 ET 미국 정규장 운영 관찰에서 owner 4는 `AUTO_PAPER`, PAPER, owner/global kill switch OFF, promotion bypass ON이었지만 US 주문·USD 체결·미국 포지션·US 추천이 모두 0건이었다.
- 09:30~10:10 ET의 5개 cycle은 매번 후보 100개, rank 84개, 전략 평가 3개, actionable 3개까지 진행했으나 `GEV`, `AMAT`, `CRWD`와 benchmark `SPY`의 5분봉을 못 받아 `intraday_trigger_not_satisfied`로 종료됐다. `us_candles_1m`에도 이 네 심볼이 0행이었고, 현재 reader의 유일한 repair provider인 KIS는 운영에서 의도적으로 미설정이다.
- 같은 운영 Toss 자격으로 `SPY` 1분봉 5행, 5분봉 78행과 `GEV` 5분봉 43행을 read-only 실측해 미국 주식 분봉 제공 능력을 확인했다.
- `app/services/market_data/toss_ohlcv.py`의 공통 분봉 pagination·aggregation helper를 KR/US가 재사용하게 하고, `get_ohlcv`의 US intraday가 DB/KIS 성공을 우선한 뒤 Toss로 fallback하도록 연결했다. 양쪽이 비거나 실패하면 기존처럼 빈 결과 또는 `UpstreamUnavailableError`로 fail-closed한다.
- provider 우선순위, 빈 결과·예외 fallback, `source=toss`, 양쪽 실패, cancellation 전파 회귀 테스트를 추가했다. 주문·risk·promotion·completed regular-session filtering은 바꾸지 않았다.
- 운영 AI cycle의 뉴스 health query가 aware `self._now`를 naive timestamp 컬럼에 바인딩해 `TypeError: can't subtract offset-naive and offset-aware datetimes`를 내던 원인을 수정했다. naive UTC 24시간 cutoff 회귀 테스트를 추가했고, 같은 운영 SQL 기준 최근 뉴스는 KR 964건·US 256건이었다.
- PR #24에서 GitHub Actions required checks를 검증한다. 운영 이미지 빌드·배포와 주문 생성은 수행하지 않았다.

## 다음 세션이 바로 할 일
1. PR #24 병합 후 배포 승인을 받아 운영 이미지를 갱신한다. 배포 전후 이미지 SHA를 기록하고 `/health`와 worker/scheduler 기동을 확인한다.
2. 미국 정규장에서 실제 AI cycle이 Toss `source=toss` 완료 5분봉을 사용하고 `intraday_provider_unavailable`이 사라지는지 관찰한다. 신호 조건 미충족을 데이터 실패로 오판하지 않고, 검증 목적으로 주문을 만들지 않는다.
3. 뉴스 health 로그에서 aware/naive `TypeError`가 재발하지 않고 KR/US health가 입증되는지 확인한다.
4. 제외 종목 `0126Z0`, `SPCX`, `SCCO`와 candidate 1은 실제 데이터·성과 조건을 채울 때만 복귀·승격한다. 데이터 생성·복제나 임계치 우회는 금지한다.
5. historical point-in-time cohort, delisted member, corporate-action 근거는 여전히 미완성이다. Forward PAPER와 historical PIT 증거를 혼동하지 않는다.

## 세션 이력
- 2026-09-01: KIS 없는 미국 장중봉 Toss fallback과 뉴스 health timezone 경계를 수정.
- 2026-09-01: CI critical path를 9분 37초에서 5분 43초로 줄이고 HANDOFF-only fast path를 fail-closed로 활성화.
- 2026-09-01: 종목별 readiness와 양시장 benchmark calendar를 수정하고 197종목 Forward PAPER backtest를 운영 완료. 성과 미달은 무승격·무주문으로 보존.
- 2026-09-01: Toss 분봉·시장지표, Forward PAPER 승격 경계, FCM 실기기 종단 경로를 운영 배포.
- 2026-08-31: 양시장 광역 후보, 한국어 뉴스 gate, Trump 공식 Truth Social 피드를 운영 배포.
