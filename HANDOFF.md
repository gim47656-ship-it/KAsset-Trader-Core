# HANDOFF — KAsset-Trader-Core
갱신: 2026-08-31 (양시장 광역 후보·한국어 뉴스 gate·Trump 공식 Truth Social 수집을 운영 배포)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 안전 계약은 owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인·promotion, 주문 idempotency를 보존하는 것이다. LIVE 주문 경로와 안전장치 우회는 추가하지 않는다.

현재 데이터 제공 계약:
1. KR/US 후보는 watchlist에 한정하지 않고 양시장 전체에서 유동성·시가총액 기반 후보를 확보한다.
2. PAPER 포지션의 native 통화값은 바꾸지 않는다. 검증된 fresh 환율이 있는 항목만 기준통화로 합산한다.
3. 일반 뉴스는 한국어 요약과 한국어 제목이 모두 완성된 행만 API에 노출한다. 원문 fallback은 없다.
4. DART/SEC 공시는 기존 한국어 공시 요약 gate를 유지한다.
5. Donald J. Trump 공식 Truth Social 원문 게시물 중 시장 관련 항목만 한국어 요약 완료 후 노출한다.

## 전체 진행 상태
- `origin/main`은 PR #11 merge commit `3095a1a1ee68fd18786df689761c96e5e97d183c`이다.
- 운영 이미지는 `kasset-trader-core:3095a1a1`, image id `sha256:dd93ba4b15e9e6513b610c5b8673c6aabd6ddb094eda34ac6c2d828fa05aa0f7`이다.
- API, worker, scheduler, 거래 MCP, AI MCP가 같은 이미지로 재기동됐다. 내부 `/healthz`는 `{"status":"ok"}`이다. DB/Redis/Caddy는 재기동하지 않았다.
- 운영은 `TRADING_ENABLED=true`, `LIVE_TRADING_ENABLED=false`, `AI_PAPER_AUTO_EXECUTION_ENABLED=true`, owner 4 runtime은 `PAPER`다. Kill Switch, Hard Risk, promotion bypass는 바꾸지 않았다.
- KR/US AI recommendation cycle은 각 거래소 현지시각 정규장에 10분 주기다. PostgreSQL session advisory lock이 겹친 cycle을 차단한다.
- 양시장 광역 후보 운영 실측은 KR 94종목, US 100종목이다. US live fallback은 TradingView screener 시가총액 상위 보통주를 최대 100개까지 반환한다.
- 일반 뉴스 backfill은 5분마다 최대 20건을 처리한다. 행별 독립 commit, 최근 불완전 시도 6시간 backoff, 수집 회차당 최대 200건 제한을 적용한다.
- 2026-08-31 18:06 UTC 운영 DB 기준 Google News 1,643건 중 API 노출 가능 한국어 완성 110건, Truth Social 5건 중 2건이다. 불완성 Google News 1,533건과 Truth Social 3건은 API에서 숨기고 후속 batch 대상으로 남긴다.
- Truth Social은 `@realDonaldTrump`의 고정 account id/username/display name을 모두 확인한다. 10분 주기로 최근 20건을 읽고 reply/reblog/비시장 게시물을 제외한다.
- owner 4의 현재 PAPER 포지션은 0건이다. 자연 추천이 실제 보유를 만들기 전에는 미국 보유 시세 provenance 두 시점 관측 대상이 없다.

## 이번 세션에서 한 일
- US 후보 fallback을 watchlist 복제에서 TradingView 전체시장 후보로 교체했다. 보통주 필터, 시가총액 정렬, 심볼 정규화, 최대 100건 제한을 적용했다.
- Android 혼합통화 합계가 환율 한 건 부재로 전부 null이 되지 않도록 API 소비 계약과 테스트를 맞췄다.
- Google News·AI briefing·market news에 공통 `complete_korean_analysis` gate를 연결했다. 영어 원문 제목은 검증된 `translated_title`, 본문은 한국어 `summary`가 함께 있어야 노출된다.
- 기존 Google News를 제한 batch로 재처리했고 scheduler가 같은 작업을 계속 수행한다. 불완전 행을 억지 번역하거나 원문으로 대신 보여주지 않는다.
- Trump 공식 Truth Social 수집 서비스와 TaskIQ 10분 schedule을 추가했다. 시장 관련성은 게시물 본문과 링크 카드 제목/설명을 함께 판정한다.
- 공식 API의 Cloudflare 403을 재현했다. 서버·workstation·일반 browser User-Agent가 모두 403이었고, 프로젝트 기존 의존성 `curl_cffi`의 `impersonate="chrome146"` async session으로 200을 확인해 기본 transport로 적용했다.
- Truth 게시물 끝 URL이 번역 제목 숫자 검증을 깨뜨리던 문제를 수정했다. 저장 제목에서는 trailing URL만 제거하고 `article_content`에는 전체 게시물과 링크를 보존한다.
- integration test가 서비스 내부 commit 때문에 다른 test DB 상태를 오염시키던 결함을 `finally` delete/commit으로 격리했다.

검증:
- PR #9 GitHub Actions run `33417860033`: 광역 후보·한국어 뉴스·Truth 초기 구현 전체 CI 통과.
- PR #10 GitHub Actions run `33420018416`: Cloudflare transport hotfix 전체 CI 통과.
- PR #11 첫 run `33421267162`: Truth integration 행 누수로 `1 failed, 6719 passed, 5 skipped`; 원인 수정 후 run `33422078193`에서 lint/formatter/ty, migration, TaskIQ smoke, test 4 shards, frontend, security, `ci-required` 전부 통과.
- 운영 Truth 재수집 결과 `fetched=20`, `relevant=5`, `updated=5`; URL 제거 제목으로 갱신됐다.
- 운영 Truth 강제 1회 backfill 결과 `selected=5`, `summarized=2`, `failed=3`. 성공 2건은 한국어 제목·한국어 요약을 모두 갖고, 전체 Truth 저장 제목의 URL 포함 건수는 0이다.
- 운영 `news_analysis_results`의 기사별 중복 group은 0이다.
- 독립 checker 최초 판정은 `REWORK`였다. MAJOR 4건, 데이터계약 2건, 견고성 finding을 조치했다. SEC title finding은 `_title -> f"{form} — {detail}"` 보장 증거로 `REJECTED_WITH_EVIDENCE`; 나머지는 `ACCEPTED`. Main 최종 판정은 `FINAL: PASS`, `OWNER: MAIN`이다.

운영 배포:
- 배포 전 backup `/root/backups/kasset-daily/kasset-20260831T171628Z.dump.gz`, 5,216,526 bytes, SHA-256 `491ef07e69c845bb7d4dad7224ab2771d48e687b062200f56459d54642f575a6`; `gzip -t`, `pg_restore -l` 통과.
- server checkout은 `3095a1a1`로 fast-forward했다. API/MCP/AI MCP/worker/scheduler만 교체했고 DB/Redis/Caddy는 유지했다.
- GitHub required checks는 strict=true, contexts `ci-required`·`migration (PostgreSQL 15)`·`frontend`, GitHub Actions app id `15368`로 복원·검증했다.

## 다음 세션이 바로 할 일
1. Google News/Truth Social의 한국어 완료 건수와 실패 사유를 관측한다. 숫자·투자권유·한국어 검증을 완화하거나 원문 fallback을 추가하지 않는다.
2. 자연 PAPER 미국 포지션이 생기면 5초 이상 간격의 두 시점에서 native USD 평가, KRW 참고값, FX source/as-of/valid-until/stale을 대조한다.
3. 추천이 자동 실행되면 `cycle_trace_id`로 cycle→recommendation→execution event→order→trade→position을 owner 4 범위에서 조인한다. `LIVE_TRADING_ENABLED=false`는 유지한다.
4. GitHub `pull_request` Actions event가 0건인 원인을 수정해야 한다. 현재 branch protection은 정확히 복원돼 있다.
5. 사용자가 Android 기기를 잠금 해제한 상태에서 혼합통화 자산 합계와 환율 경고를 1회 육안 확인한다.

## 세션 이력
- 2026-08-31: 양시장 광역 후보, 한국어 뉴스 gate, Trump 공식 Truth Social 피드를 운영 image `3095a1a1`로 배포.
- 2026-08-31: 미국장 10분 AI cycle, 분산 single-flight, 검증된 USD 원화 참고 평가를 운영 배포.
- 2026-08-31: P0 cycle/실행 추적 원장, KRW/USD 성과 분리, 시세 provenance를 운영 배포.
- 2026-08-31: PAPER 실시간 평가·USD 자금·뉴스 동기화·AI malformed 응답 격리를 운영 배포.
- 2026-08-31: 국내 스크리너 KRX 세션 만료 fallback을 배포해 운영 150종목을 복구.
