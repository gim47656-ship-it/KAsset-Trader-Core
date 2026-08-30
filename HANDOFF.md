# HANDOFF — KAsset Trader Core
갱신: 2026-08-30 (운영 DB migration·KR/US 100종목 일봉·기준지수 적재 및 fail-closed readiness 배포)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset Trader Core는 스크리너, 시세·뉴스·공시, 전략, AI 분석, PAPER/LIVE 주문 원장과 Android API를 제공한다. 현재 목표는 **PAPER에서 재현 가능한 추천·승격·주문·청산을 충분히 검증한 뒤 별도 승인으로 LIVE를 검토**하는 것이다. 일일 목표를 이유로 거래를 만들거나, 불완전한 이력·기업행동·PIT 근거를 실제 backtest 증거처럼 취급하거나, AI가 Hard Risk를 우회하면 안 된다.

정본 운용 계약:

1. **APPROVAL**: 추천 → 사용자 승인 → PAPER 주문.
2. **AUTO_PAPER**: persisted backtest candidate, exact strategy/version `PAPER_APPROVED`, 동일 strategy artifact fingerprint, submit 직전 Hard Risk·Kill Switch·owner scope 재검증을 모두 통과한 PAPER 주문만 자동 실행한다.
3. AI는 후보 factor·수량·stop·exit·backtest metrics를 만들거나 덮어쓰지 않는다. 추천 설명·검토만 담당한다.
4. 데이터 readiness는 252개 완료 세션, benchmark, PIT cohort, 상장·폐지와 기업행동 근거를 모두 fail-closed로 평가한다. minimum을 낮추거나 현재 universe를 과거 universe로 가장하지 않는다.
5. LIVE 주문은 별도 사용자 승인 전까지 열지 않는다.

## 전체 진행 상태
- **운영 배포 완료**: 운영 이미지 source ref는 `dc012816`, public `/health`는 `{"status":"ok"}`다. Alembic은 `20260830_kr_lifecycle_ca (head)`까지 적용됐다.
- **운영 백업 완료**: `/root/backups/kasset-daily/kasset-20260829T222212Z.dump.gz`, SHA-256 `e4eecec8da3261eea98887f9da38f00ee189e0379922638baeda4b9a29e4da00`.
- **운영 cohort 고정**: KR `67f1059ab7e3a370ab5b9dd89ec3991ad4860d7e4560004674b4f02dda917547`, US `fd27bf6f66e9f73f0f725e2284969c69425c0d804f74f20a4e537788c86a3d02`; 각각 active 100종목이다. US TQQQ/SOXL은 강제 연속성 멤버이며 readiness/promotion 표본에서는 제외된다.
- **일봉 적재 완료**: KR 99/100종목이 252봉 이상이고 신규 상장 `0126Z0`은 187봉이다. US 99/100종목이 252봉 이상이고 신규 상장 `SPCX`는 54봉이다. KOSPI와 SPY benchmark는 각각 400봉이다.
- **캘린더·OHLC 결함 제거**: stale XKRX의 2025-12-31, 2026-06-03, 2026-07-17, 2026-08-17, 2026-12-31 오분류를 shared calendar에서 교정했다. Yahoo 완료봉은 exact session/previous/current gate를 유지하고 metadata 가격 반올림만 상대 허용오차로 처리한다.
- **최종 운영 readiness**: KR은 거래일 누락 0, OHLC 이상 0, eligible 99다. US는 OHLC 이상 0, eligible 98이며 `SCCO`의 Yahoo 원천 2026-08-10 1봉 누락이 남아 있다. 양 시장 모두 신규 상장 history, 기업행동·상장폐지 PIT 근거, forward-only cohort, fallback-only source 때문에 promotion은 계속 차단된다.
- **PIT/기업행동 차단**: 운영 KIS token 발급이 HTTP 403이다. 알려진 현재 종목만으로 과거 상장폐지 universe를 복원할 수 없고 KRX licensed archive 자격도 없어, 검증되지 않은 데이터를 생성하지 않았다.
- **PAPER 자동화 신뢰경계 완료**: immutable strategy fingerprint, position cycle, claim lease/fencing, ambiguous submit reconciliation, selected AI shadow, benchmark window와 fold coverage를 구현했다.

## 이번 세션에서 한 일
- 운영 DB를 gzip dump로 백업하고 clone migration round-trip 뒤 운영 head까지 migration했다.
- Toss 최신 시가총액으로 KR/US 100종목 immutable cohort를 만들고, KR/US 일봉과 KOSPI/SPY benchmark를 최대 400봉 적재했다.
- `kasset_research_cohorts`, cohort members, KR lifecycle/corporate-action coverage schema와 fail-closed readiness를 추가했다.
- completed expected session, duplicate/future/stale/OHLC, adjusted-close, benchmark, lifecycle/PIT/corporate-action/fallback 근거를 시장별로 계산한다.
- XKRX stale 휴장일을 shared session calendar로 수렴시켜 watcher, session API, KIS cache, KR sync, daily read, screener, forecast가 같은 세션 계약을 사용하게 했다.
- Yahoo terminal NaN 완료봉은 provider metadata의 exact 정규장 종료, 직전 raw/adjusted close, current price, day bounds가 모두 결박될 때만 복구한다. BRK.A 실측 metadata 반올림은 `rel_tol=1e-6, abs_tol=0.01`로 처리하고 OHLC를 내부 정합 상태로 정규화했다.
- 홈 지수 API는 완료된 일봉을 제공한다. 운영 smoke에서 SPX -0.25%, NASDAQ -0.52%, RUT -1.39%, SOX -3.47%였고 DJI는 원천 부재를 fail-closed로 표시했다. SPX 1주 상세는 2026-08-24~08-28 일봉 5개, 마지막 close 7711.76이었다.

검증:

- 데이터/candle/session/readiness PostgreSQL 집중 스위트: **250 passed**.
- 최종 휴장·Yahoo·readiness 회귀: **48 passed**, 추가 2026-06-03 휴장 회귀 **43 passed**.
- `ruff format --check app/ tests/ research/ scripts/`, `ruff check ...`, `ty check app`: 통과.
- 전체 `pytest -q` 수집은 Windows의 기존 `fcntl` 부재와 frozen research source hash 때문에 실패했다. 이번 KAsset 변경과 무관하며 같은 실패를 반복하지 않았다.
- 운영 `/app/.build-vcs-ref`: `dc012816`; public health 통과.
- 운영 BRK.A 재백필: 400봉 upsert, Yahoo fallback, OHLC normalization 성공.
- 최종 운영 readiness: KR missing 0/anomaly 0/99 eligible, US missing 1/anomaly 0/98 eligible.
- 독립 checker: `FINAL: PASS / OWNER: CHECKER`, blocker 0, major 0.

주요 커밋:

- `edf50e13` 초기 fail-closed readiness
- `abb506d7` immutable cohorts와 benchmark
- `5b2784a8` 강제 멤버 제외·fail-closed 보강
- `003b3ed4` benchmark pagination·row count
- `b6e2b4e9` Yahoo 완료봉 복구
- `297eb39f` zero OHLC readiness
- `9f743b82` shared session calendar·Yahoo OHLC 정합
- `a1b600c0` 잔여 연말휴장·가격 반올림 보강
- `dc012816` 2026 지방선거 KRX 휴장 보강

## 다음 세션이 바로 할 일
1. KIS HTTP 403을 계정/허용 IP/앱 권한에서 해결하고, KRX 또는 라이선스된 PIT archive 자격을 확보한 뒤 상장폐지·기업행동 coverage를 다시 적재한다. 현재 데이터를 과거 PIT라고 승격하지 않는다.
2. `SCCO` 2026-08-10 일봉은 신뢰할 수 있는 2차 원천으로만 보강한다. 인접 봉 복제나 보간은 금지한다.
3. 신규 상장 `0126Z0`, `SPCX`는 실제 252개 완료 세션이 쌓이기 전까지 insufficient-history를 유지한다.
4. 현재 forward-paper cohort의 effective date 이후 충분한 기간을 수집해 persisted backtest candidate를 만들고, evidence/hash 검수 뒤에만 `promotion-approve`를 실행한다.
5. KRX 개장 중 APPROVAL 추천→승인→PAPER fill/reconcile을 검증한다. AUTO_PAPER는 승격 후 소액으로 duplicate submit, claim lease 회수, kill switch, partial/full exit, 재진입을 확인한다.
6. XKRX 공급자 봉과 expected session 집합의 주기적 drift 경보를 추가하고, 2026-12-31은 경과 뒤 실제 공급자 데이터로 재확인한다.

## 세션 이력
- 2026-08-30: 운영 migration, KR/US 100종목 cohort·일봉·benchmark 적재, calendar/Yahoo 복구와 readiness 실측 완료.
- 2026-08-30: PAPER promotion evidence/CLI, artifact fingerprint, position cycle, claim lease, AI shadow, migration CI gate 완료.
- 2026-08-29: 결정론적 PAPER 자동화·exact-version 승격 gate, 추천 시장·일일 횟수, AI 공급자·뉴스 경계 완료.
- 2026-08-29: DART 운영 수집·문서 fallback·일반 뉴스 AI 요약·5단계 PAPER 정책 완료.
