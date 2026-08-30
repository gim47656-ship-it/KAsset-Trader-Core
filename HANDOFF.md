# HANDOFF — KAsset Trader Core
갱신: 2026-08-30 (재부팅 중단 뒤 운영 DB migration·Core 배포·NH PLUG 캐시 이관 복구)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset Trader Core는 스크리너, 시세·뉴스·공시, 전략, AI 분석, PAPER/LIVE 주문 원장과 Android API를 제공한다. 현재 목표는 **PAPER에서 재현 가능한 추천·승격·주문·청산을 충분히 검증한 뒤 별도 승인으로 LIVE를 검토**하는 것이다. 일일 목표를 이유로 거래를 만들거나, 불완전한 이력·기업행동·PIT 근거를 실제 backtest 증거처럼 취급하거나, AI가 Hard Risk를 우회하면 안 된다.

정본 운용 계약:

1. **APPROVAL**: 추천 → 사용자 승인 → PAPER 주문.
2. **AUTO_PAPER**: persisted backtest candidate, exact strategy/version `PAPER_APPROVED`, 동일 strategy artifact fingerprint, submit 직전 Hard Risk·Kill Switch·owner scope 재검증을 모두 통과한 PAPER 주문만 자동 실행한다.
3. AI는 후보 factor·수량·stop·exit·backtest metrics를 만들거나 덮어쓰지 않는다. 추천 설명·검토만 담당한다.
4. 데이터 readiness는 252개 완료 세션, benchmark, PIT cohort, 상장·폐지와 기업행동 근거를 모두 fail-closed로 평가한다. minimum을 낮추거나 현재 universe를 과거 universe로 가장하지 않는다.
5. LIVE 주문은 별도 사용자 승인 전까지 열지 않는다.

## 전체 진행 상태
- **운영 배포 완료**: Naver 운영 Core는 commit/image `1e0c19c3`로 API·MCP·worker·scheduler를 동일 배포했다. API와 MCP는 healthy, worker와 scheduler는 running이며 재시작 횟수는 모두 0이다.
- **운영 DB head 적용 완료**: `20260830_news_translation`이 `news_analysis_results`에 nullable `translated_title`, `translated_excerpt` 두 열을 추가했다. 기존 35행은 보존됐고 번역값 null이 정상이다.
- **NH PLUG 캐시 무발급 이관 완료**: `/opt/kasset-nhplug/token_cache.json`의 owner/base/만료를 내용 출력 없이 검증한 뒤 owner fingerprint 파일로 원자 이동했다. 파일 크기·mtime이 유지돼 신규 OAuth 발급은 없었고 mode는 `0600`이다.
- **뉴스 번역 운영 wire 확인 완료**: 인증된 `/api/v1/market/news`와 `/api/v1/ai/daily-routine`이 `translatedTitle`/`translatedExcerpt` 키를 반환한다. 실제 번역값 생성은 신규 영문 뉴스 분석 전까지 미검증이다.
- **중요 P0 미종결 — readiness 설명과 코드 불일치**: 현재 `selection_method='historical_pit'` 라벨만으로 PIT로 취급하고 promotion blocker는 `includes_delisted`와 list-date coverage를 필수로 보지 않는다. 이 경계를 고치기 전 promotion을 승인하면 안 된다.
- **기존 운영 데이터 상태 유지**: KR/US 각 100종목 cohort, KOSPI/SPY 400봉, KR eligible 99, US eligible 98이며 KIS 403, SCCO 1봉 누락, 신규 상장 history 부족은 그대로다.

## 이번 세션에서 한 일
- 로컬 PC 재부팅으로 끊긴 운영 작업을 재조사했다. 운영 서버와 PostgreSQL은 재부팅되지 않았고, 사고 직후 revision은 `20260830_kr_lifecycle_ca`, 대상 열 0개, `news_analysis_results` 35행, 대기 lock 0개여서 partial DDL이 없음을 확인했다.
- migration 전 full custom dump `/root/backups/kasset-daily/kasset-20260830T042232Z.dump.gz`를 새로 만들고 `gzip -t`와 `pg_restore -l`로 검사했다. 크기 4,381,905 bytes, SHA-256 `0faa24707ad969f8286082111487b2af6b41f2ab3d508a39d81604cd278908a8`.
- 원격 working tree를 detached `1e0c19c3`으로 고정하고 `kasset-trader-core:1e0c19c3` 이미지를 빌드했다.
- PostgreSQL transactional DDL로 `20260830_kr_lifecycle_ca -> 20260830_news_translation`을 적용하고 API·MCP·worker·scheduler를 새 이미지로 재생성했다.
- 유효기간이 77,580초 남은 legacy NH PLUG 캐시를 실 OAuth 호출 없이 owner 파일로 이관했다. legacy 파일은 사라지고 owner 파일 1개만 남았다.

검증:

- 운영 postcheck: revision `20260830_news_translation`; 두 신규 열은 `text`, nullable `YES`; 기존 35행 유지; 번역 non-null 0; 대기 lock 0.
- 컨테이너: API/MCP healthy, worker/scheduler running, image `1e0c19c3`, restart count 0.
- 공개 `https://175-45-201-51.sslip.io/health`: HTTP 200, `{"status":"ok"}`.
- 인증된 `GET /api/v1/market/news?limit=1`: HTTP 200, 1건, 두 번역 키 존재.
- 인증된 `GET /api/v1/ai/daily-routine`: HTTP 200, alert 1건, 두 번역 키 존재.
- NH PLUG owner cache: legacy 없음, owner 파일 1개, mode `0600`, 기존 size/mtime 유지.
- 구현 시점 집중 검증 108건과 `ruff`/`ty`는 이전 세션에서 통과했다. 운영 restore·downgrade와 실 NH OAuth POST는 비파괴 원칙상 실행하지 않았다.

## 다음 세션이 바로 할 일
1. 실제 영문 Reuters 등 신규 분석에서 번역 제목·발췌와 summary가 저장되고 두 KAsset API에 노출되는지 확인한다. 기존 row의 null은 정상이다.
2. readiness에서 라벨만으로 historical PIT를 신뢰하지 말고 실제 persisted constituent history, delisted 포함, list-date coverage를 promotion blocker로 결박한다. `includes_delisted=False` promotion-ready 테스트는 fail-closed 기대값으로 교정한다.
3. KIS HTTP 403, SCCO 2026-08-10, 신규 상장 `0126Z0`/`SPCX`, KRX APPROVAL→PAPER fill/reconcile, XKRX drift 경보는 기존 미종결 상태를 유지한다.

## 세션 이력
- 2026-08-30: 재부팅 중단 상태 확인, 운영 DB backup/migration, Core `1e0c19c3` 배포, NH PLUG 캐시 무발급 이관 완료.
- 2026-08-30: 영문 뉴스 번역 제목/발췌, KAsset API 연결, NH PLUG owner cache·process lock 구현; readiness PIT 미종결 재판정.
- 2026-08-30: 운영 migration, KR/US 100종목 cohort·일봉·benchmark 적재, calendar/Yahoo 복구와 readiness 실측.
- 2026-08-30: PAPER promotion evidence/CLI, artifact fingerprint, position cycle, claim lease, AI shadow, migration CI gate 완료.
- 2026-08-29: 결정론적 PAPER 자동화·exact-version 승격 gate, 추천 시장·일일 횟수, AI 공급자·뉴스 경계 완료.
