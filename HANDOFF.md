# HANDOFF — KAsset Trader Core
갱신: 2026-08-30 (내일 KRX 개장 직후 PAPER 실운용 인계)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset Trader Core는 스크리너, 시세·뉴스·공시, 전략, AI 분석, PAPER 주문 원장과 Android API를 제공한다. 사용자의 현재 목표는 **내일 KRX 개장 직후 모의투자(PAPER)를 실제로 돌리는 것**이다. 개발 단계 문구를 제거하고 운영 제품처럼 설명하라는 지시도 이번 로컬 변경에 반영했다.

정본 운용 계약:

1. **APPROVAL**: 추천 → 사용자 승인 → PAPER 주문.
2. **AUTO_PAPER**: persisted backtest candidate, exact strategy/version 승격, artifact fingerprint, submit 직전 Hard Risk·kill switch·owner scope 검사를 통과한 PAPER 주문만 자동 실행한다.
3. 소유자가 `promotion_bypass_enabled`를 켜면 AUTO_PAPER의 승격 근거 요구 하나만 면제한다. `AUTO_PAPER`, `trading_mode=PAPER`, kill switch, owner scope, Hard Risk는 면제하지 않으며 실행 근거를 `promotion_bypassed_by_owner`로 기록한다.
4. **LIVE 주문 경로는 없다.** `app/extensions/kasset/api/runtime_state.py`, `app/extensions/kasset/automation/policy.py`, `app/extensions/kasset/automation/job.py`, `app/schemas/ai_recommendations.py`가 PAPER에 고정돼 있다. `app/extensions/kasset/automation/consumer.py:197-198`은 `trading_mode == "LIVE"`를 `live_mode_forbidden`으로 명시 차단한다. LIVE를 설정하면 실거래가 아니라 자동매매가 멈춘다.
5. 데이터 readiness는 완료 세션, benchmark, 실제 PIT cohort, 상장·폐지와 기업행동 근거를 fail-closed로 평가한다. 라벨만으로 historical PIT를 인정하지 않는다.

## 전체 진행 상태

| 영역 | 로컬 상태 | 운영 상태 / 남은 확인 |
|---|---|---|
| PIT 승격 게이트 | 라벨만으로 P0 승격이 가능하던 경로를 닫음. blocker 5종과 기준일 방어 테스트 반영 | 미배포. 실제 historical PIT 근거가 없으면 계속 차단해야 함 |
| AUTO_PAPER override | `promotion_bypass_enabled` migration/API/감사 근거 구현 | 운영 DB에는 컬럼이 없음. 승인 후 migration 필요 |
| 지표 상세 API | 지표 8종 상세, `1D`, metadata, `supported_ranges`, GLOBAL/nullable currency 반영 | 미배포. KOSPI·KOSDAQ `1D` 미지원 계약 확인 필요 |
| 상태·환율 | `/system/status.migration_revision`, `cny_per_usd` 구현 | 미배포. Android의 DB revision과 CNYKRW는 배포 전 운영값을 받을 수 없음 |
| 제품 문구 | AI relay/status와 LIVE 거절 문구를 제품 문구로 정리 | 앱에서 보이던 `AI 기능은 이번 통합 단계에서 확장하지 않습니다.`는 이번 세션 전 로컬 코드에는 이미 없었고, 운영 `f3359102`의 `router.py:443,1051`에만 남아 있음 |
| 검증 | 변경 범위 테스트와 정적 검증 통과 | Windows 전체 테스트는 수집 단계 23건 오류로 중단. 별도 수정 진행 중 |

- 운영 Core는 commit/image `f3359102`, 운영 DB revision은 `20260830_news_translation`이다. 로컬 HEAD는 `1962f15a`이고 오늘 변경은 working tree에 미커밋 상태로 남아 있다. 코드와 migration 모두 운영에 배포하지 않았다.
- 공개 운영 `/health`는 HTTP 200이다. 이 결과는 기존 `f3359102`의 상태이며 오늘 로컬 변경의 운영 검증이 아니다.
- 아직 남은 운영 이슈는 KIS HTTP 403, SCCO 2026-08-10 1봉 누락, 신규 상장 `0126Z0`/`SPCX` history 부족, XKRX drift 경보, 번역 실값 생산 미검증, KRX 개장 중 APPROVAL→PAPER fill/reconcile 실장 실증이다.

## 이번 세션에서 한 일
- `app/services/daily_candles/readiness.py`와 `app/extensions/kasset/automation/promotion_evidence.py`에서 `evidence_scope='historical_pit'` 라벨만으로 승격되던 P0 경로를 닫았다. `list_date_coverage_incomplete`, `member_listed_after_cohort_start`, `delist_date_coverage_incomplete`, `point_in_time_unavailable`, `delisted_members_absent` blocker를 추가했다. 기준일은 `cohort.effective_date`로 강화했고 `_require_readiness` 방어분기 테스트 4종을 추가했다. 독립 검수 결과는 `VERDICT: PASS`였다.
- `promotion_bypass_enabled` 컬럼과 migration `20260830_kasset_promotion_bypass`를 추가했다. `POST /api/v1/ai/trading/promotion-bypass`가 소유자 override를 바꾸며, override 주문은 `promotion_bypassed_by_owner`를 남긴다. 이 기능은 승격 근거 외의 안전장치를 우회하지 않는다.
- `MarketIndexRange`에 `1D`를 추가하고 `MarketIndexSummary`에 `kind`, `unit`, `group`, `supported_ranges`를 넣었다. market에 `GLOBAL`을 추가하고 currency는 null을 허용한다. VIX, US10Y, WTI, BRENT, GOLD, BTC, DXY, ETH 상세를 열고 intraday timestamp를 보존했다.
- 지수 `1D`는 US yfinance 계열과 Upbit 크립토만 지원한다. KOSPI·KOSDAQ 지수 분봉 소스는 저장소 provider에 없다. Toss `/api/v1/candles`는 종목 계약이고, NH PLUG는 6자리 종목 현재가·호가만 허용하며, KRX는 구성종목 CSV, Naver는 `day/week/month`만 지원한다. 클라이언트가 범위를 추측하지 않도록 심볼별 `supportedRanges`를 내려준다. history 소스가 없는 KR 국채 6종은 상세 allowlist에서 제외했다.
- `/system/status`가 `alembic_version`을 읽기 전용으로 조회해 `migration_revision`을 채우도록 했다. `exchange_rate_service.py`에는 CNYKRW 계산용 `cny_per_usd`를 추가했다.
- AI relay/status와 `runtime_state.py`의 개발 단계 문구를 제품 문구로 정리했다. 앱에서 보인 `AI 기능은 이번 통합 단계에서 확장하지 않습니다.`는 이번 세션 전 로컬 코드에는 이미 없었으며, 현재 운영 `f3359102`에만 남아 있다.

검증:

- Core 변경 범위 테스트: 통과.
- `ruff check`: 통과.
- `ruff format --check`: 통과.
- `ty check`: 통과.
- 전체 테스트는 Windows 수집 단계에서 23건 오류로 중단됐다. 원인은 두 갈래다. `research/kr_backfill/adjacent_window_predicate.py`의 source-freeze guard가 원시 바이트를 hash해 CRLF checkout과 고정 digest가 달라지고, 일부 테스트가 `fcntl`과 `signal.SIGHUP` 같은 POSIX 전용 의존을 import한다. LF로 정규화한 바이트의 hash는 고정 digest와 일치하므로 source 내용 변경 문제가 아니다. 이 Windows 호환 수정은 별도 작업으로 진행 중이다.

## 다음 세션이 바로 할 일
1. **사용자의 배포 승인을 먼저 확인한다.** 승인 전에는 운영 서버 접속, 운영 DB 변경, 배포를 하지 않는다. 시작 기준은 운영 Core `f3359102`, 운영 DB `20260830_news_translation`, 로컬 HEAD `1962f15a`와 미커밋 working tree다.
2. Windows 전체 테스트 수집 호환 수정 결과를 합친 뒤 Main이 변경 파일과 migration을 검토하고 Core 변경을 커밋·푸시한다. 전체 회귀 결과에서 로컬 변경으로 생긴 실패가 없는지 확인한다.
3. 승인 후 운영 `/health`와 현재 revision을 확인하고 **운영 DB full backup을 먼저** 만든다. 서버의 `/usr/local/sbin/kasset-db-backup.sh`(저장소 사본 `deploy/kasset-db-backup.sh`)를 쓴다. 이 스크립트는 `docker exec kasset-trader-db-1 pg_dump`로 `/root/backups/kasset-daily/`에 덤프를 만들며, 03:30 KST cron이 같은 경로에 일일 덤프를 남긴다. 복원은 `kasset-db-restore.sh <덤프>`다. `20260830_kasset_promotion_bypass`는 `ADD COLUMN ... DEFAULT false NOT NULL` 단일 확장이고 `alembic heads`가 단일 head임을 확인했으므로 구버전 코드와 호환된다.
4. 배포는 서버에서 compose로 한다. 운영은 `root@100.73.186.78`(Tailscale, 공인 `175.45.201.51`) Naver Rocky Linux 8.8의 `/opt/kasset-trader-core`, compose project `kasset-trader`이며 서비스는 db·redis·api·worker·scheduler·mcp·caddy다. 순서는 `docker compose --env-file .env.kasset -f docker-compose.kasset.yml build api` → `up -d api worker scheduler mcp` → migration 적용 → `alembic_version`과 새 컬럼 기본값 `false` 확인이다.
   - **SSH 접속**: NCP 콘솔(`https://aitestbed.kr/my-studio/cloud/apply`, 인스턴스 `144429515`)에서 받은 PEM이 `C:/Users/kkmin/.ssh/kasset_ncp.pem`에 있고, 추가로 `C:/Users/kkmin/.ssh/id_ed25519`(주석 `kasset-deploy-home-pc-kkmin`) 공개키를 서버 `authorized_keys`에 등록해 두었다. 따라서 `ssh root@100.73.186.78`이 옵션 없이 동작한다. Tailscale SSH는 세션 재라우팅 경고 때문에 **켜지 않았다**(`tailscale set --ssh`는 `--accept-risk=lose-ssh` 필요). 사무실 PC용 3번째 경로는 아직 미확보이며 같은 방식으로 공개키를 추가하면 된다.
   - **코드 전달**: 저장소는 public이라 서버가 인증 없이 `git fetch origin` 된다. GitHub `refs/heads/main`은 `1962f15a`로 로컬 HEAD와 같다. 주의: 로컬 `remote.origin.fetch` refspec이 `kasset-integration` 하나로 좁혀져 있어 `origin/main` 원격추적 ref가 갱신되지 않는다. `git rev-parse origin/main`을 믿지 말고 `git ls-remote origin refs/heads/main`으로 확인해라. 서버는 detached HEAD로 특정 SHA를 checkout해 운영한다.
   - **`.github/workflows/deploy-macos-native.yml`과 `scripts/deploy-native.sh`는 이 프로젝트의 경로가 아니다.** 상위 `auto_trader` 포크 잔재이고 그 MacBook 호스트는 tailnet에 없다. 그 경로로 배포하지 마라.
5. `/health`, `/system/status.migration_revision`, 브로커 `implemented`/`mode`, CNYKRW, 지표 8종 상세와 심볼별 `supportedRanges`, `POST /api/v1/ai/trading/promotion-bypass`의 owner scope·감사 근거를 확인한다. worker/scheduler 기동과 오래된 개발 단계 문구 제거도 확인한다.
6. Android 실기기에서 거래 API 4종, AI 상태, DB revision, LIVE 선택의 HTTP 409 사유와 PAPER 원복, 홈 CNYKRW와 지표 상세 range를 재확인한다. KOSPI·KOSDAQ에는 `1D`가 없어야 한다.
7. KRX 개장 중 APPROVAL 추천 승인부터 PAPER 주문, fill, position, reconcile까지 추적한다. AUTO_PAPER override를 사용한다면 승격 근거만 면제되고 kill switch·AUTO_PAPER·PAPER mode·owner scope·Hard Risk는 계속 적용되는지 증명한다.
8. KIS HTTP 403, SCCO 누락 봉, `0126Z0`/`SPCX` history, XKRX drift, 신규 영문 뉴스 번역 실값은 별도 미종결로 유지하고 실측 결과를 기록한다.

## 세션 이력
- 2026-08-30: PIT 승격 게이트 fail-closed 강화, owner promotion bypass, 지표 상세 API, migration revision, CNYKRW와 제품 문구 교정. 모두 로컬 미커밋·미배포.
- 2026-08-30: Android 설정·거래 모드·지표 상세·홈 격자 구현과 SM-S926N 실기기 검증 완료.
- 2026-08-30: 운영 Core `f3359102`, DB `20260830_news_translation`, 뉴스 번역 wire와 NH PLUG cache 이관 완료.
- 2026-08-30: 영문 뉴스 번역 제목/발췌, KAsset API 연결, NH PLUG owner cache·process lock 구현.
- 2026-08-29: 결정론적 PAPER 자동화와 exact-version 승격 gate, 추천 시장·일일 횟수, AI 공급자·뉴스 경계 구현.
