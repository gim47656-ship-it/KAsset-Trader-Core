# HANDOFF — KAsset-Trader-Core
갱신: 2026-09-01 (Toss 분봉·Forward PAPER 경계·FCM 실기기 종단 검증을 운영 반영)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset-Trader-Core는 Android KAsset Trader의 조회·추천·PAPER 거래·자동화 백엔드다. 운영 범위는 Toss와 NH PLUG이며 KIS 미설정은 의도된 상태다. owner scope, PAPER 고정, Kill Switch, Hard Risk, 승인·promotion, 주문 idempotency를 보존하고 LIVE 주문 경로나 안전장치 우회를 만들지 않는다.

## 전체 진행 상태
- 최신 실행 코드: PR #16 merge `b2dcab7785bd588f0c30bb8c8cabf766c07182f7`, 분봉 경계 후속 PR #17 merge `01ed075c3f67bd2d6b8ee64dbf5773aa9f012e8e`.
- 운영 이미지: `kasset-trader-core:01ed075c`, digest `sha256:acbe9f9a67243b8bdb2bc77e3fdf3cabd8ac0b537b38497916a85766b33370ad`. `/health`는 `{"status":"ok"}`.
- migration `20260901_kasset_fcm_push` 적용 완료. FCM service-account는 운영 `.env.kasset`에 base64 secret으로만 저장했고 원본 임시 파일은 삭제했다. 저장소에는 secret이 없다.
- Android 실기기 활성 FCM 토큰 1건. 서버 제어 테스트는 FCM HTTP v1 `SENT`, HTTP 200이었고 SM-S926N에서 알림 표시와 알림 탭 진입을 확인했다.
- Toss 1분봉은 전체 3,938종목을 분당 20종목씩 순환하며 회당 최대 200봉을 영속화한다. 2026-09-01 운영 스모크는 20종목 중 18종목 성공, 3,600행 upsert. 2종목은 Toss가 수집 기준보다 2분 미래 봉을 반환해 fail-closed 됐다.
- KR 일봉 backfill: 102/102 성공, Toss/Naver fallback. US 일봉 backfill: 102/103 성공. 신규 상장 `SPCX`만 55봉이라 252봉 조건을 채울 수 없다.
- readiness는 `promotionReady=false`. KR 99/100, US 99/100만 252봉 이상이며 historical point-in-time·상장폐지·corporate-action 근거도 미완성이다.
- `promotion-status`는 `promotions=[]`. PAPER 자동화 스모크는 `enabled=true`, `owners=0`, `outcomes=[]`; 승격 근거가 없으므로 주문이 생성되지 않는 것이 정상이다.

## 이번 세션에서 한 일
- Toss 1분봉 영속 저장소·TaskIQ schedule을 추가하고 `ON CONFLICT ON CONSTRAINT`가 운영 Timescale hypertable에서 실제 동작함을 `EXPLAIN`으로 확인했다.
- Toss batch가 분 경계를 넘을 때 바로 다음 1분 봉은 허용하고, 그보다 미래인 봉과 분류 불가 행은 거부하도록 보강했다. 한 행의 이상이 정상 행 전체를 버리지 않도록 정리했다.
- KOSPI/KOSDAQ 장중 지수를 Toss 시장지표 경로로 복구했다.
- historical 근거가 없는 backtest와 `forward_paper` cohort를 분리했다. whole-market unevidenced session, 잘못된 evidence scope, 기존 성과 임계치 미달은 promotion을 차단한다.
- FCM device token 등록·폐기 API, 세션 귀속 저장, Firebase HTTP v1 sender, retry/dedupe, 무효 토큰 폐기, 10분 price-alert dispatch를 구현했다.
- Android 선택 기능인 push endpoint의 401/미지원이 정상 로그인 세션을 지우지 않도록 계약을 보존했다.

검증:
- Core PR #16 GitHub Actions run `33480275637`: `ci-required`, lint, security, TaskIQ, PostgreSQL 15 migration, test shards 1~4 전체 통과.
- Core PR #17 GitHub Actions run `33481879936`: required checks 전체 통과. 별도 Discord notification job 실패는 코드 required check가 아니며 PR은 병합됐다.
- 운영 이미지 기동 로그 `Application startup complete`, 외부 `/health` 200 확인.
- 운영 DB FCM 토큰 1건 확인. 자연 ±5% 알림은 0건이어서 주문·신호와 무관한 `[TEST] 삼성전자 +5.0%` 제어 메시지 1건으로 Core→Firebase→실기기→앱 알림 탭 종단 경로를 확인했다.
- 독립 checker finding: F2/F3/F4/F5/F8은 `ACCEPTED` 후 수정. F1은 운영 Timescale `EXPLAIN` 성공, F6은 claim transaction을 send 전에 commit할 경우 영구 orphan이 생기는 계약, F7은 KR/US 양시장 24시간 schedule과 KST-day dedupe 근거로 `REJECTED_WITH_EVIDENCE`.

운영 배포:
- 배포 전 backup `/root/backups/kasset-daily/kasset-20260901T064000Z.dump.gz`, SHA-256 `6a1c05eef0bb47678bef13964435e43537651667f5975a3cc8dae9fad9d06b2d`.
- 운영 compose는 반드시 `docker-compose.kasset.yml`만 사용한다. 기본 compose와 함께 실행하면 DB/worker network가 분리된다.
- API/MCP/AI MCP/worker/scheduler를 `01ed075c`로 교체했다. DB는 기존 production volume을 유지했다.

## 다음 세션이 바로 할 일
1. Toss 미래 2분 봉을 반환한 종목 `439090`, `439260`의 provider timestamp를 관측한다. 범위를 임의로 넓혀 미래 데이터를 저장하지 않는다.
2. 신규 상장 `SPCX`는 실제 거래일이 252개 쌓이기 전까지 history blocker로 유지한다. 데이터 생성·복제는 금지한다.
3. historical point-in-time cohort, delisted member, corporate-action 근거가 준비될 때까지 promotion을 승인하지 않는다. `promotionReady=false`에서 무주문은 정상이다.
4. 자연 ±5% daily routine alert가 발생하면 dedupe row와 실제 알림 제목·앱 내 해당 alert focus를 대조한다.
5. FCM secret 회전 시 `.env.kasset`만 갱신하고 원본 JSON·토큰·private key를 로그나 저장소에 남기지 않는다.

## 세션 이력
- 2026-09-01: Toss 분봉·시장지표, Forward PAPER 승격 경계, FCM 실기기 종단 경로를 운영 배포.
- 2026-08-31: 양시장 광역 후보, 한국어 뉴스 gate, Trump 공식 Truth Social 피드를 운영 배포.
- 2026-08-31: 미국장 10분 AI cycle, 분산 single-flight, 검증된 USD 원화 참고 평가를 운영 배포.
- 2026-08-31: P0 cycle/실행 추적 원장, KRW/USD 성과 분리, 시세 provenance를 운영 배포.
- 2026-08-31: 국내 스크리너 KRX 세션 만료 fallback을 배포해 운영 후보를 복구.
