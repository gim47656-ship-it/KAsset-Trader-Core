# HANDOFF — KAsset-Trader-Core
갱신: 2026-09-01 (CI critical path 단축·HANDOFF-only fast path 활성화)

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

## 이번 세션에서 한 일
- GitHub Actions 실측에서 PR test shard 1이 `9m37s`, 나머지가 `4m21s~6m05s`인 critical-path 불균형을 확인했다. 원인은 `test_portfolio_backtest.py`가 각 변조 케이스마다 동일한 8~14초 evidence payload를 다시 계산하는 구조였다.
- immutable baseline payload를 module fixture로 한 번만 계산하고 각 테스트가 `deepcopy`를 받아 격리되도록 바꿨다. 기존 `--dist=loadfile`은 과거 worksteal trial의 공유 DB 격리 실패 증거 때문에 유지했다.
- 최신 call/test duration을 GitHub Actions에서 다시 측정하고 4개 `ci_shards` manifest를 재균형했다. 최종 shard는 `5m41s`, `5m24s`, `4m17s`, `5m43s`로 가장 느린 shard가 3분 54초 줄었다.
- `HANDOFF.md` add/modify만 있는 변경은 Python/Node/PostgreSQL/Redis resource job을 건너뛴다. 테스트가 정확한 문언을 읽는 `docs/**`, `README.md`, `CLAUDE.md`, `AGENTS.md`와 임의 Markdown은 전체 CI를 강제한다.
- duration refresh는 `DURATIONS_REFRESH_TOKEN`이 없어도 측정·artifact 생성을 계속하고 auto-PR만 건너뛴다. 측정 job은 token preflight와 병렬로 시작한다.
- Discord webhook secret 부재는 중립 skip으로 처리하고, 실제 webhook HTTP 실패는 `curl --fail-with-body`로 red를 유지한다.
- TaskIQ smoke의 중첩 프로세스 준비 시간을 0.15초로 고정해 발생하던 CI race를 해당 케이스만 최대 2초 condition poll하도록 수정했다. 실패 종료·cleanup failure 검출은 그대로다.

검증:
- PR #22 최종 GitHub Actions Test run `33516535105`: required checks와 전체 workflow 통과, 총 `6m17s`.
- duration refresh run `33512897900`: token 없이 exit 0, authoritative collection·4-shard 측정·merged duration·rebalanced manifest artifact 생성, auto-PR만 skip.
- 독립 checker 최초 판정 `REWORK`: 문서 계약 파일이 docs-only로 오분류되어 테스트를 우회할 수 있음을 지적. `ACCEPTED` 후 fast path를 `HANDOFF.md` 하나로 제한했다.
- 같은 checker delta 재검증 `FINAL: PASS`, `OWNER: MAIN`. 문서 계약 경로는 모두 `ci_shared` 또는 `unknown`으로 fail-closed임을 확인했다.
- production 코드·거래 로직·DB migration 변경 없음. 운영 이미지 재빌드·배포 없음.

## 다음 세션이 바로 할 일
1. 제외 종목 `0126Z0`, `SPCX`, `SCCO`는 실제 데이터가 조건을 채울 때만 적격으로 복귀시킨다. 252봉 생성·복제나 expected-session 우회는 금지한다.
2. candidate 1은 excess return과 walk-forward 기준 미달이다. 새 데이터에서 모든 기존 성과 임계치를 통과하기 전에는 draft/approve하지 않는다.
3. historical point-in-time cohort, delisted member, corporate-action 근거는 여전히 미완성이다. Forward PAPER와 historical PIT 증거를 혼동하지 않는다.
4. 자연 ±5% daily routine alert가 발생하면 dedupe row와 실제 알림 제목·앱 내 해당 alert focus를 대조한다.
5. FCM secret 회전 시 `.env.kasset`만 갱신하고 원본 JSON·토큰·private key를 로그나 저장소에 남기지 않는다.

## 세션 이력
- 2026-09-01: CI critical path를 9분 37초에서 5분 43초로 줄이고 HANDOFF-only fast path를 fail-closed로 활성화.
- 2026-09-01: 종목별 readiness와 양시장 benchmark calendar를 수정하고 197종목 Forward PAPER backtest를 운영 완료. 성과 미달은 무승격·무주문으로 보존.
- 2026-09-01: Toss 분봉·시장지표, Forward PAPER 승격 경계, FCM 실기기 종단 경로를 운영 배포.
- 2026-08-31: 양시장 광역 후보, 한국어 뉴스 gate, Trump 공식 Truth Social 피드를 운영 배포.
- 2026-08-31: 미국장 10분 AI cycle, 분산 single-flight, 검증된 USD 원화 참고 평가를 운영 배포.
