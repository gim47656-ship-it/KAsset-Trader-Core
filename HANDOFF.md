# HANDOFF — KAsset Trader Core
갱신: 2026-08-31 (PAPER 승인 실행·AI 라우팅·한국어 운영 화면 운영 배포)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset Trader Core는 시세·뉴스·공시, 전략·AI 분석, PAPER 주문 원장과 Android API를 제공한다. 최우선 목표는 **2026-08-31 KRX 장중부터 안전장치를 유지한 국내 모의투자 자동매매를 실제 운영하는 것**이다.

정본 운용 계약:

1. 활성 trader는 owner `id=4` 한 명이다. 별도 관리자는 `id=6`이다.
2. PAPER 실행 방식은 상호 배타적인 두 모드다. `APPROVAL`은 사용자가 추천을 승인하면 즉시 해당 owner의 PAPER 주문을 시도하고 구조화 결과를 반환한다. `AUTO_PAPER`는 승인 없이 5분 sweep에서 실행한다.
3. 두 모드 모두 runtime `PAPER`, owner scope, fresh quote, Hard Risk, kill switch를 우회하지 않는다. owner 4의 `promotion_bypass_enabled=true`는 승격 근거만 면제한다.
4. LIVE 주문 경로는 없다. `LIVE_TRADING_ENABLED=false`이며 LIVE 설정은 `live_mode_forbidden`으로 차단한다.
5. 유효한 최신 시세·전략 신호가 없으면 주문하지 않는 것이 정상이다. 거래 건수를 만들기 위해 신호를 강제하지 않는다.
6. 관리자 화면은 Tailnet 전용 `https://vm-naver-kasset.tail624c43.ts.net/admin/ops`다. Secret·API key·내부 URL·명령어는 화면/API에 노출하지 않는다.

## 전체 진행 상태

| 영역 | 운영 상태 | 남은 확인 |
|---|---|---|
| Core 배포 | commit/image `f667c9d5`, DB `20260830_ai_runtime_config`; api/mcp healthy, worker/scheduler running | KRX 장중 첫 적격 PAPER 체결 관찰 |
| PAPER 자동화 | 활성 owner 4 `AUTO_PAPER`/`PAPER`, bypass ON, owner/global kill OFF, PAPER 계좌 연결 | 추천→order→fill→position→reconcile 전 구간 실측 |
| 승인 주문 | 승인 API가 PAPER 실행을 시도하고 `paperExecution` 상태·사유·추천 ID·재실행 여부 반환 | Android에서 장중 추천 1건 승인 실측 |
| AI 라우팅 | 운영자 제어 4 lane, DB revision 1; 저장 시 optimistic revision·CSRF 적용 | 필요할 때만 대시보드에서 route 변경 |
| 관리자·스크리너 | 초보자용 한국어·반응형 화면 운영 배포, Tailnet Chrome 확인 | 없음 |
| 비밀번호 복구 | Gmail SMTP 설정과 실제 메일 발송 성공 | 없음 |
| Android | `f6a06064`; 승인 PAPER 결과 표시 구현 | SM-S926N 잠금 해제 후 최종 화면 확인 |

현재 AI 경로:

- 요약: `direct-api / gpt-5.6-luna` → `openrouter / z-ai/glm-5.3-flash`
- Luna 검토: 사용 불가 MCP는 runtime에서 건너뛰고 `direct-api / gpt-5.6-luna` → OpenRouter
- Terra 검토: 사용 불가 MCP는 건너뛰고 `direct-api / gpt-5.6-terra` → OpenRouter
- Sol 검토: 사용 불가 MCP는 건너뛰고 `direct-api / gpt-5.6-sol` → OpenRouter
- 최근 운영 성공 시각은 대시보드에 표시된다. 내부 호환 lane은 `operatorControllable=false`로 숨기고 저장할 때 보존한다.

운영 증거:

- 배포 전 백업: `/root/backups/kasset-daily/kasset-20260830T151305Z.dump.gz`, 4,493,381 bytes, SHA-256 `bc11cccd031e839c2f28483b01b7f7911f124ae30af473760a3ca107d3f6ddfb`; `gzip -t`, `pg_restore --list` 통과.
- image digest: `sha256:2229e388071cc4ea03c6277e3d847670a70a3525b9ab623ca6bb4e8e179b1592`.
- Compose 정본은 project `kasset-trader`, config `/opt/kasset-trader-core/docker-compose.kasset.yml`, env `/opt/kasset-trader-core/.env.kasset`다. 운영 명령은 `docker compose -p kasset-trader --env-file .env.kasset -f docker-compose.kasset.yml ...` 형식을 사용한다. 기본 `docker compose ps`는 다른 project 이름을 잡아 빈 목록을 보일 수 있다.
- 활성 owner SQL 실측: `id=4 | trader | PAPER | kill=false | bypass=true | AUTO_PAPER | paper_account=true`. 전역 kill `false`.
- 컨테이너 환경 실측: `TRADING_ENABLED=true`, `AI_PAPER_AUTO_EXECUTION_ENABLED=true`, `LIVE_TRADING_ENABLED=false`.

## 이번 세션에서 한 일

- 승인 추천이 즉시 안전한 PAPER 실행으로 이어지도록 Core 계약을 확장하고 Android가 구조화 실행 결과를 표시하도록 연동했다. 예외도 HTTP 500으로 새지 않고 `FAILED` 결과로 반환한다.
- `AUTO_PAPER`의 immutable cycle snapshot, fresh quote·Hard Risk·owner·kill·PAPER gate를 유지했다.
- AI route singleton DB, migration, 4개 운영 lane, provider/model/availability/recent success 조회와 CSRF·revision 기반 저장을 구현했다. Secret은 저장·반환하지 않는다.
- 운영 대시보드와 스크리너를 한국어·초보자 중심 반응형 화면으로 재구성했다. 비활성 admin을 거래 사용자로 잘못 세지 않도록 집계를 수정했다.
- Gmail SMTP 실발송을 확인하고 기존 fmcity 535 미종결을 닫았다.
- reviewer 1회에서 찾은 subscription mock 계약, 숨은 compat lane, admin 집계, 승인 실행 예외, 손상 정책 복구 UI, 미사용 context를 모두 수정했다.
- Core `f667c9d5`, Android `f6a06064`를 각각 main에 push하고 운영 DB migration·image 교체를 완료했다.

검증:

- Android `TraderApiContractTest` + `ReviewViewModelTest`: BUILD SUCCESSFUL.
- Core recommendation route 25 passed, PAPER 집중 3 passed, subscription 10 passed.
- Core 영향 범위 묶음 105 passed와 후속 rework 56 passed; ruff/format/ty 통과.
- 실제 브라우저: `/admin/ops` 1440px·390px에서 가로 overflow 없음, 콘솔 오류 없음, 운영 AI 4 lane과 실제 provider/model 확인.
- 실제 브라우저: `/screener` 1440px에서 한국어 렌더링, 가로 overflow 없음, 콘솔 오류 없음.
- 운영 `/health` 성공, migration head `20260830_ai_runtime_config`, image `f667c9d5`.
- 독립 검수 finding 6건을 모두 `ACCEPTED` 수정으로 닫음. `FINAL: PASS`, `OWNER: MAIN`.

## 다음 세션이 바로 할 일

### 2026-08-31 장중 운영 체크리스트

1. 08:50 KST 전 Tailnet에서 `/admin/ops`를 열고 활성 거래 사용자 1명, `AUTO_PAPER=1`, `APPROVAL=0`, Core 거래 ON, PAPER 자동 실행 ON, 전역/사용자 kill 0, LIVE 금지를 확인한다.
2. owner 4가 `AUTO_PAPER`, runtime `PAPER`, bypass ON, PAPER 계좌 연결인지 확인한다.
3. 09:05 전후 국내 watchlist candle 수집, 09:10 추천 생성, 이후 5분 PAPER sweep을 추적한다.
4. 첫 적격 신호에서 owner 4의 추천 → PAPER order → fill → position → reconcile을 같은 trace로 확인한다.
5. 신호 없음 또는 fresh quote 없음은 무주문이 정상이다. 임의 BUY나 안전장치 우회로 건수를 만들지 않는다.
6. Android 승인 흐름을 확인할 때만 `승인 후 주문(APPROVAL)`으로 바꾸고 후보 1건을 승인해 `paperExecution`과 PAPER 주문을 확인한다. 이후 무인 운용은 `자동 주문(AUTO_PAPER)`으로 되돌린다. 두 모드는 동시에 실행되지 않는다.
7. stale quote, owner mismatch, Hard Risk, kill, 예상하지 않은 LIVE, 중복·타 owner·설명되지 않는 주문이 보이면 우회하지 말고 즉시 kill을 켠다.

### 사무실 PC Tailnet·SSH 설정

1. Windows 사무실 PC에 Tailscale을 설치하고 같은 Tailnet 계정으로 로그인한 뒤 관리자 승인 상태를 확인한다.
2. PowerShell에서 `tailscale status`, `tailscale ping vm-naver-kasset`을 실행하고 관리자 URL을 연다.
3. 사무실 전용 키를 만든다. 개인키를 다른 PC에서 복사하지 않는다.
   `ssh-keygen -t ed25519 -a 64 -C "kasset-office-20260831" -f "$env:USERPROFILE\.ssh\kasset_office_ed25519"`
4. 공개키만 확인한다.
   `Get-Content "$env:USERPROFILE\.ssh\kasset_office_ed25519.pub"`
5. 기존 신뢰 PC 또는 Naver 콘솔에서 이 공개키 한 줄만 서버 `/root/.ssh/authorized_keys`에 추가한다.
6. `ssh -i "$env:USERPROFILE\.ssh\kasset_office_ed25519" root@100.73.186.78`로 접속한다. 최초 승인 전 ED25519 fingerprint가 `SHA256:DIjAP5O9l5088ynPC99h3WScdN/KpR1bceTCb07c+3g`인지 확인한다.
7. 서버의 Tailscale `RunSSH=false`이므로 이는 Tailnet 위 일반 OpenSSH다. Tailscale SSH quickstart를 그대로 적용하지 않는다. 실패 시 Tailnet 연결·ACL·공개키 등록부터 확인한다.

그 밖의 미종결:

- PIT historical cohort/기업행동 근거가 준비되면 정상 승격을 만든 뒤 owner bypass를 끈다.
- KIS HTTP 403, SCCO 누락 봉, `0126Z0`/`SPCX` history, XKRX drift, 실제 뉴스 번역 생산을 별도 추적한다.
- SM-S926N 잠금 해제 뒤 Android 최종 화면을 확인한다.

## 세션 이력
- 2026-08-31: PAPER 승인 실행·AI 라우팅·한국어 대시보드/스크리너 구현, 운영 image `f667c9d5` 배포와 브라우저 검증.
- 2026-08-30: admin dashboard/password recovery 배포, Tailnet SNAT 403 수정, owner 4 AUTO_PAPER 준비.
- 2026-08-30: PIT 승격 gate fail-closed, owner promotion bypass, 지표 상세 API, CNYKRW 구현.
- 2026-08-30: Android 설정·거래 모드·지표 상세·홈 격자 구현과 SM-S926N 검증.
- 2026-08-29: 결정론적 PAPER 자동화와 exact-version 승격 gate 구현.
