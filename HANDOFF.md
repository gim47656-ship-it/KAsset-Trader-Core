# HANDOFF — KAsset-Trader-Core
갱신: 2026-09-02 (뉴스 요약 Luna API 긴급 중지와 장중 수집 상태 확인)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 운영 broker 범위는 KR/US 실계좌·주문·체결의 Toss와 KR mock read-only 조회의 NH PLUG이며, KIS 미설정은 의도된 상태다. 역사 KIS ledger/read model은 보존하되 production runtime에는 연결하지 않는다. owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인 hash, 주문 idempotency, accepted-only ledger와 broker evidence fill을 보존하고 검증 목적으로 주문을 만들지 않는다.

## 전체 진행 상태
- PR #26 merge `1e502ebf0712feb8ef9ccff77946003c7596a2ea`를 운영 배포했다. 운영 checkout, API/worker image, `/app/.build-vcs-ref`가 모두 이 SHA와 일치한다.
- API, analysis MCP, AI MCP sidecar의 container 내부 `/health`가 모두 `ok`다. `api`, `mcp`, `ai-mcp`, `worker`, `scheduler`, `db`, `redis`, `caddy`가 기동 중이다.
- migration head는 `20260902_screener_toss_source`다. `20260902_toss_report_scopes`와 Toss screener source migration은 additive하게 적용됐다.
- 배포 전 DB full custom archive는 `backups/pre-toss-cutover-20260901T191938Z/database.dump`에 저장했다. SHA-256은 `2d3af1b360b6e9fc4658af924dccb46bd16d0b7faa448572049ee80c97dddafe`이며 `pg_restore --list`와 checksum 검증을 통과했다.
- 활성 KR/US live 계좌·주문·정정·취소·체결 조회는 Toss다. 기존 KIS 요청은 Toss로 우회하지 않고 broker I/O·원장 mutation 전에 `provider_unsupported`로 fail-closed한다.
- KR/US live 시세는 Toss 공용 채널만 사용하고 실패 시 저장 PAPER 일봉으로 강등한다. legacy `broker=NH` 시세 요청도 Android 호환을 위해 수용하되 NH PLUG를 호출하지 않는다. NH PLUG native bridge는 KR mock 계좌·잔고·보유 read-only allowlist만 사용하며 주문 기능이나 MCP mutation surface는 없다.
- Toss 주문은 owner scope, 최신 sellable preflight, approval hash, idempotency, accepted-only ledger와 broker evidence 기반 fill booking을 유지한다. Toss fill poller는 공용 `AsyncSessionLocal`을 사용하며 KIS session factory 의존이 없다.
- Upbit accepted limit order reconcile은 `market=crypto`, `broker=upbit`로 고정 복구됐다. equity/KIS 입력은 kernel·broker I/O 전에 fail-closed한다.
- 미국 장중 OHLCV는 Toss 분봉·집계 경로를 사용한다. 2026-09-01 19:40/19:50 UTC 정규장 cycle은 후보 100, rank 84, evaluated/actionable 3까지 정상 진행했고 `intraday_trigger_not_satisfied`로 무추천 종료했다. 배포 전 반복되던 KIS token·US candle sync·intraday provider 오류는 배포 후 관찰 로그에 없었다.
- 20:00 UTC 장 마감부터 AI recommendation cycle이 실행되지 않아 정규장 gate가 작동했다. 배포 이후 Toss live order 0건, execution fill 0건이다.
- PR #26 GitHub Actions run `33565440250`에서 lint, security, PostgreSQL migration, TaskIQ worker/scheduler smoke, test shard 1~4, intraday, Alpaca, frontend, `ci-required`가 모두 성공했다. 최종 integration-risk 독립 검토는 `PASS`다.

- 2026-09-02 09:56 KST에 OpenAI 사용량 급등을 막기 위해 운영 AI route policy revision 8의 `summary_luna`를 빈 배열로 바꿨다. 뉴스 수집 데이터는 유지하지만 뉴스·공시 AI 번역·요약 호출은 비활성이다.

## 이번 세션에서 한 일
- NH PLUG 현재가 endpoint가 운영 read-only smoke에서 HTTP 400을 반환한 원인을 provider 수용 실패로 확정하고, 활성 quote resolver·Android NH adapter·저수준 client·smoke CLI에서 NH quote runtime을 제거했다.
- 단일/배치 한국 시세와 legacy `broker=NH` 요청을 Toss live → 저장 PAPER 일봉으로 통일했다. Toss 실패 시 자동 PAPER 주문이 저장 일봉을 stale로 차단하는 기존 guard는 유지했다.
- 운영에서 삼성전자 `005930` Toss 시세 `255000 KRW`, `as_of=2026-09-01T10:59:59+00:00`를 read-only 실측했다. NH 계좌·잔고 smoke는 `rsp_cd=00000`, mock 계좌 3건으로 회귀 없이 통과했으며 검증 주문은 만들지 않았다.
- PR #26을 merge하고 exact SHA image build, app service 재기동, health·migration head·SHA 검증을 수행했다.
- runtime KIS 호출을 전수 조사해 활성 MCP registry, 계좌·보유·현금·시세·주문·정정·취소·체결, screener enrichment, scheduled task와 운영 script 배선을 Toss/NH PLUG 계약으로 전환했다. 역사 ledger와 dormant adapter만 남겼다.
- 미국 장중봉의 KIS 의존을 제거하고 Toss 1분봉 pagination·5분 집계 경로를 공용화했다. cancellation 전파, 완료된 정규장 봉, provenance와 양쪽 실패 fail-closed 테스트를 추가했다.
- 뉴스 health query의 aware/naive subtraction을 naive UTC cutoff로 정규화하고 회귀 테스트를 추가했다.
- KIS-only metric과 intent는 묵시적 Toss 전환 대신 `provider_unsupported`로 닫았다. PAPER/owner/kill switch/hard risk/approval/idempotency 경계는 유지했다.
- 최종 checker finding에 따라 Upbit crypto accepted-order reconcile을 복구하고 Toss fill poller의 KIS session factory 의존을 제거했다. 문서의 watch auto-execute 계약을 Android `PaperOrderFacade` owner-scoped `db_simulated`로 바로잡았다.
- PR #24를 merge하고 DB 백업, image build, 두 migration, app service 재기동, health·SHA 검증을 수행했다.
- 미국 정규장 19:40/19:50 UTC cycle과 20:00 UTC 마감 전이를 read-only 관찰했다. 실주문이나 검증 주문은 만들지 않았다.
- OpenAI Platform Responses 로그 1,727건 중 화면에 로드된 최근 160건을 확인했으며, 160건 모두 뉴스 payload와 `gpt-5.6-luna` 호출이었다.
- 운영 `review.ai_call_events` 최근 2시간 집계에서 OpenRouter 442회가 전부 `kasset_news_summary/news-summary`였다. 성공 157회 1,049,618 tokens, 실패 285회 265,145 tokens로 합계 1,314,763 tokens였다.
- 운영 worker를 재시작해 진행 중이던 뉴스 요약 호출을 종료했다. DB `kasset_ai_runtime_config`는 `revision=8`, `summary_luna=[]`다.
- worker 재기동 완료 시각인 09:56:33 KST 이후 10:00:02 KST까지 AI 호출 원장 신규 행과 소비 token은 모두 0이었다.

## 다음 세션이 바로 할 일
1. 뉴스 요약은 사용자가 비용 정책을 정하기 전까지 다시 켜지 않는다. 재개 시 5분마다 최대 20건을 처리하는 현재 schedule을 그대로 복구하지 말고, 기사 선별·일일 상한·입출력 토큰 상한을 먼저 확정한다.
2. 정상 미국장 cycle을 계속 관찰하되 `intraday_trigger_not_satisfied`를 provider 실패로 오판하지 않는다. 검증 주문은 만들지 않는다.
3. `app/services/filled_orders_service.py::_toss_fill_timestamp`의 파싱 불가 timestamp 1건이 해당 fetch window 전체를 실패시키는 low-severity fail-safe 동작을 per-order skip으로 좁힐지 별도 변경으로 검토한다.
4. startup의 passlib/bcrypt `__about__` 경고와 yfinance cache 경고는 health를 깨지 않지만 의존성·권한 정리 후보로 남아 있다.
5. 제외 종목 `0126Z0`, `SPCX`, `SCCO`, 성과 미달 candidate와 historical point-in-time cohort 근거는 실제 데이터·성과 조건을 채울 때만 복귀·승격한다.

## 세션 이력
- 2026-09-02: OpenAI와 OpenRouter 로그에서 뉴스 요약 토큰 급증을 확인하고 운영 `summary_luna` route를 비활성화했다.
- 2026-09-01: KR/US 시세를 Toss 단일 live provider로 전환하고 NH PLUG quote runtime 제거 후 운영 배포.
- 2026-09-01: KIS production runtime을 제거하고 Toss/NH PLUG로 전환해 운영 배포, 미국 정규장·마감 관찰까지 완료.
- 2026-09-01: CI critical path를 9분 37초에서 5분 43초로 줄이고 HANDOFF-only fast path를 fail-closed로 활성화.
- 2026-09-01: 종목별 readiness와 양시장 benchmark calendar를 수정하고 197종목 Forward PAPER backtest 결과를 반영.
- 2026-09-01: Toss 분봉·시장지표, Forward PAPER 승격 경계, FCM 실기기 종단 경로를 운영 배포.
