# HANDOFF — KAsset Trader Core
갱신: 2026-08-30 (운영 관리자·복구 경계 배포와 2026-08-31 KRX PAPER 준비)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset Trader Core는 시세·뉴스·공시, 전략·AI 분석, PAPER 주문 원장과 Android API를 제공한다. 사용자의 최우선 목표는 **2026-08-31 KRX 개장부터 국내 모의투자 자동매매를 운영하는 것**이다.

정본 운용 계약:

1. 운영 소유자는 활성 trader `id=4` 한 명이다. `id=1`, `id=5`는 비활성화하고 refresh/device/browser 세션을 철회했다. 관리자 `kys@hsps.co.kr`는 별도 admin `id=6`이다.
2. 소유자 `id=4`는 `AUTO_PAPER`, runtime `PAPER`, 개인·전역 kill switch `OFF`다. `promotion_bypass_enabled=true`는 승격 근거만 면제한다. owner scope, fresh quote, Hard Risk, kill switch와 PAPER 고정은 우회하지 않는다.
3. LIVE 주문 경로는 없다. LIVE를 설정하면 실행되는 것이 아니라 `live_mode_forbidden`으로 차단된다.
4. 관리자 화면은 Tailnet 전용 `https://vm-naver-kasset.tail624c43.ts.net/admin/ops`다. 공개 sslip 및 API 직접 접근의 `/admin*`, `/web-auth*`는 403이어야 한다.
5. 유효한 최신 시세·전략 신호와 Hard Risk 통과가 없으면 주문하지 않는 것이 정상이다. 거래 건수를 만들기 위해 신호를 강제하지 않는다.

## 전체 진행 상태

| 영역 | 운영 상태 | 남은 확인 |
|---|---|---|
| Core 배포 | commit/image `6d6bf3cf`, DB `20260830_admin_recovery`, 서비스 6종 기동 | 2026-08-31 개장 중 실시간 실행 관찰 |
| PAPER 자동화 | owner 4 `AUTO_PAPER`/`PAPER`, bypass ON, kill switch OFF, PAPER 계좌 1개 | fresh quote와 적격 신호가 생길 때 주문·fill·position·reconcile 추적 |
| 국내 루틴 | 2026-08-31 `KR_ONLY`, 급등·급락·트럼프 정책·글로벌 금융 뉴스 4종 ON | 개장 후 수집·추천 결과 확인 |
| Toss PAPER | read-only preflight `accounts=1 holdings=0 prices=2` 통과 | 개장 중 실제 fresh quote 확인 |
| 관리자 경계 | OMP Relay 실제 Chrome 로그인과 `/admin/ops` 200, public/direct 403 | 없음 |
| 비밀번호 복구 | 코드·DB·세션 철회·TLS 호환 배포 완료 | fmcity SMTP가 현재 자격증명을 535로 거부. 올바른 mailbox/app password 또는 SMTP AUTH 활성화 필요 |
| Android | 최신 debug APK를 SM-S926N에 재설치하고 MainActivity process/frame 생성 확인 | 기기 잠금 때문에 최종 화면 육안 확인은 사용자가 잠금 해제 후 수행 |

- 배포 전 백업: `/root/backups/kasset-daily/kasset-20260830T123722Z.dump.gz`, 4,380,822 bytes, SHA-256 `7370d24ea470fa7a4a8f4ce049f31e6e705ef4c8bdf395a35c870c6c58025553`; `gzip -t`, `pg_restore -l` 통과.
- Caddy 403 원인은 IPv6 누락이 아니었다. 허용값에는 이미 `100.64.0.0/10 fd7a:115c:a1e0::/48`가 있었지만 Tailscale userspace forwarding이 Docker bridge에서 `172.18.0.1`로 SNAT됐다. Caddy를 host network로 옮겨 실제 Tailnet peer를 보존했다.
- 운영 대시보드의 readiness는 KR 승격 기준 미달을 경고한다. owner bypass는 의도적으로 켰지만 다른 안전장치는 유지된다.
- SMTP는 명시적 legacy 모드에서 TLS 1.0과 legacy renegotiation을 허용한다. 인증서 검증은 유지한다. 이 완화는 fmcity 교체 전까지의 운영 위험으로 인수했으며 다른 SMTP에는 기본 OFF다.

## 이번 세션에서 한 일

- 관리자 운영 대시보드, AI 사용량·자동매매 funnel·PAPER 포트폴리오·체결 대사·readiness·뉴스·승격 패널과 web login/password recovery를 구현·배포했다.
- 관리자 edge shared-secret, user row lock, reset token partial unique index, password reset 시 legacy refresh·Android device·browser session 철회를 적용했다.
- 운영 DB를 `20260830_admin_recovery`까지 migration하고 image `6d6bf3cf`로 api·worker·scheduler·mcp를 교체했다.
- 별도 admin `id=6`을 만들고 기존 중복 trader `id=1`, `id=5`를 비활성화했다. 활성 trader는 `id=4` 한 명이다.
- owner 4를 `AUTO_PAPER`/`PAPER`로 전환하고 bypass ON, kill switch OFF, `2026-08-31 KR_ONLY` 루틴 4종을 저장했다.
- Tailnet 403을 Caddy access log의 `remote_ip=172.18.0.1`로 재현하고 host network/loopback upstream으로 수정했다. 수정 뒤 Relay client `100.77.160.92`가 login/admin 200, public `175.45.201.51`이 web-auth 403이었다.
- fmcity의 legacy TLS handshake 실패를 `UNSAFE_LEGACY_RENEGOTIATION_DISABLED`로 특정하고 opt-in context에 `OP_LEGACY_SERVER_CONNECT`를 추가했다. TLS는 통과하지만 공급자는 SMTP AUTH `535`를 반환한다.

검증:

- 기존 final auth/mobile/edge: 71 passed.
- 기존 reset/concurrency/Google lock delta: 24 passed.
- 기존 automation/policy/vertical slice/ops: 48 passed.
- SMTP 회귀: 1 passed; `ruff check`, `ruff format --check`, `ty check` 모두 통과.
- Caddy 2.11.4 validate RC 0; Compose `network_mode=host`, published ports 없음.
- 운영 `/health` 200, migration `20260830_admin_recovery`, api/mcp healthy, worker/scheduler running.
- Toss preflight RC 0: `accounts=1 holdings=0 prices=2`.
- OMP Relay 실제 Chrome: 로그인 성공, `/admin/ops` 200, 화면에서 DB·자동매매·readiness 패널 확인.
- 공개 sslip `/web-auth/login` 403, `/health` 200; API loopback `/web-auth/login` 403; HTTP unclaimed ACME path 404.

## 다음 세션이 바로 할 일

1. 2026-08-31 KRX 개장 전 운영 대시보드에서 owner bypass 1명, global/owner kill switch OFF, Toss 연결을 확인한다. 관리자 URL은 `/admin/ops`부터 열어 로그인 후 원래 화면으로 복귀시킨다.
2. 09:05 KST 전후 국내 watchlist candle 수집, 09:10 추천 생성, 이후 5분 PAPER sweep을 로그와 대시보드에서 추적한다. 적격 신호가 없으면 무주문이 정상이며 임의 BUY를 만들지 않는다.
3. 첫 적격 추천이 생기면 추천 → PAPER order → fill → position → reconcile을 같은 owner 4 기준으로 확인한다. stale quote, Hard Risk, kill switch 또는 owner mismatch 차단은 유지한다.
4. fmcity mailbox의 정확한 SMTP/app password 또는 SMTP AUTH 활성화 여부를 사용자에게 받아 `.env.kasset`만 갱신한다. 실제 테스트 메일과 forgot-password 메일 수신을 확인한다. Secret은 Git·HANDOFF·로그에 남기지 않는다.
5. 사용자가 SM-S926N 잠금을 해제하면 최신 debug APK에서 홈·설정 서버 상태를 육안 확인한다. APK 설치와 MainActivity 실행 자체는 끝났다.
6. PIT historical cohort/기업행동 근거가 준비되면 정상 승격을 만든 뒤 owner bypass를 끈다.
7. KIS HTTP 403, SCCO 누락 봉, `0126Z0`/`SPCX` history, XKRX drift, 실제 뉴스 번역 생산은 별도 미종결로 유지한다.

## 세션 이력
- 2026-08-30: admin dashboard/password recovery 배포, Tailnet SNAT 403 수정, owner 4 AUTO_PAPER와 2026-08-31 KR 루틴 준비.
- 2026-08-30: PIT 승격 gate fail-closed, owner promotion bypass, 지표 상세 API, migration revision, CNYKRW 구현.
- 2026-08-30: Android 설정·거래 모드·지표 상세·홈 격자 구현과 SM-S926N 검증.
- 2026-08-30: 운영 뉴스 번역 wire와 NH PLUG cache 이관.
- 2026-08-29: 결정론적 PAPER 자동화와 exact-version 승격 gate 구현.
