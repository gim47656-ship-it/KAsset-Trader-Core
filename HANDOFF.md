# HANDOFF — KAsset Trader Core
갱신: 2026-08-30 (영문 뉴스 번역 발췌·NH PLUG 토큰 중복발급 방지 및 readiness 미종결 재판정)

## 프로젝트 개요와 사용자가 원하는 방향
KAsset Trader Core는 스크리너, 시세·뉴스·공시, 전략, AI 분석, PAPER/LIVE 주문 원장과 Android API를 제공한다. 현재 목표는 **PAPER에서 재현 가능한 추천·승격·주문·청산을 충분히 검증한 뒤 별도 승인으로 LIVE를 검토**하는 것이다. 일일 목표를 이유로 거래를 만들거나, 불완전한 이력·기업행동·PIT 근거를 실제 backtest 증거처럼 취급하거나, AI가 Hard Risk를 우회하면 안 된다.

정본 운용 계약:

1. **APPROVAL**: 추천 → 사용자 승인 → PAPER 주문.
2. **AUTO_PAPER**: persisted backtest candidate, exact strategy/version `PAPER_APPROVED`, 동일 strategy artifact fingerprint, submit 직전 Hard Risk·Kill Switch·owner scope 재검증을 모두 통과한 PAPER 주문만 자동 실행한다.
3. AI는 후보 factor·수량·stop·exit·backtest metrics를 만들거나 덮어쓰지 않는다. 추천 설명·검토만 담당한다.
4. 데이터 readiness는 252개 완료 세션, benchmark, PIT cohort, 상장·폐지와 기업행동 근거를 모두 fail-closed로 평가한다. minimum을 낮추거나 현재 universe를 과거 universe로 가장하지 않는다.
5. LIVE 주문은 별도 사용자 승인 전까지 열지 않는다.

## 전체 진행 상태
- **뉴스 번역 구현 완료·미배포**: 영문 우세 제목과 최대 4,000자 원문 범위를 한국어 제목/발췌로 번역해 `NewsAnalysisResult`에 저장한다. summary와 번역은 분리한다. 숫자·단위·언어·길이·schema를 검증하되 번역만 부적합하면 해당 필드를 null로 버리고 검증된 summary는 보존한다.
- **신규 DB head**: `20260830_news_translation`은 `news_analysis_results`에 nullable `translated_title`, `translated_excerpt` 두 열만 추가하는 reversible migration이다. 운영 DB는 아직 이전 head `20260830_kr_lifecycle_ca`이므로 배포 전 migration 승인이 필요하다.
- **KAsset API 연결 완료**: `/market/news`와 `/ai/daily-routine`은 최신 분석 row의 `translatedTitle`/`translatedExcerpt`를 bulk query로 제공한다. 기존 row와 가격 alert는 null, 원문 URL은 유지된다.
- **NH PLUG 재발급 원인 판정**: 가장 유력한 원인은 VPS `/opt/kasset-nhplug` 영속 캐시가 Mac native `~/.nhplug`로 인계되지 않은 것이다. 추가 확정 결함은 공용 env key와 사용자 vault key가 단일 파일을 덮어쓰는 구조, blue/green cold miss의 process lock 부재다.
- **NH PLUG 코드 교정 완료·미배포**: 기본 캐시는 owner fingerprint별 파일로 분리하고 기존 native 단일 cache를 무발급 원자 이관한다. POSIX는 owner별 `flock` 뒤 cache를 다시 확인해 동일 owner 동시 발급을 1회로 제한한다. 실 NH OAuth는 사용자 알림을 다시 유발할 수 있어 호출하지 않았다.
- **중요 P0 미종결 — readiness 설명과 코드 불일치**: 현재 `selection_method='historical_pit'` 라벨만으로 PIT로 취급하고 promotion blocker는 `includes_delisted`와 list-date coverage를 필수로 보지 않는다. 기존 테스트도 `includes_delisted=False`인데 promotion ready를 기대한다. 이전 HANDOFF의 “상장폐지·PIT를 모두 fail-closed 평가” 문장은 실제 코드보다 강했고, 이 경계를 고치기 전 promotion을 승인하면 안 된다.
- **기존 운영 데이터 상태 유지**: KR/US 각 100종목 cohort, KOSPI/SPY 400봉, KR eligible 99, US eligible 98이며 KIS 403, SCCO 1봉 누락, 신규 상장 history 부족은 그대로다.

## 이번 세션에서 한 일
- `NewsAnalysisResult`와 migration에 nullable 번역 제목·발췌를 추가했다. 기존 row는 backfill 없이 null을 유지한다.
- 공용 MCP-first structured AI transport를 재사용해 한글이 없는 영문 제목/본문만 번역한다. 본문은 모델 전달 전에 4,000자로 제한하고 번역 발췌는 6,000자 상한, 숫자·단위 누락/추가 금지, 한국어 출력 조건을 적용한다.
- 일반 뉴스와 daily routine의 최신 분석 row를 `created_at DESC, id DESC`로 한 번에 읽어 summary와 두 번역 필드가 서로 다른 row에서 섞이지 않게 했다.
- `$5 billion`과 `50억 달러`처럼 값이 같은 영문·한국어 scale 표현을 기준값으로 정규화한다. 잘못된 번역 필드는 null로 격하해 summary·sentiment 저장과 다음 배치 idempotency를 지킨다.
- NH PLUG 기본 token cache를 raw key가 아닌 owner fingerprint별 파일로 분리했다. 유효한 legacy 단일 cache는 OAuth POST 없이 owner 파일로 이동하고 이관 실패 시에도 검증된 token을 재사용한다.
- Mac/Linux에서는 owner cache별 process lock을 획득한 뒤 cache를 재검사한다. mode 교정 실패는 읽을 수 있는 token을 버리지 않고, lock 인프라 실패는 재발급으로 우회하지 않고 typed configuration error로 fail-closed한다.
- 재조사에서 readiness의 historical PIT label 신뢰와 promotion blocker 누락을 확인했다. 이번 요청 범위에서는 거래 승격 로직을 바꾸지 않았으며 P0로 명시했다.

검증:

- 번역·daily routine·market news·AI briefing·NH PLUG auth/client/orderbook 집중 스위트: **108 passed**.
- Android와 맞춘 KAsset wire는 `translatedTitle`/`translatedExcerpt` camelCase로 검증했다.
- 변경 Python `ruff format`, `ruff check`, 전체 `ty check app`: 통과.
- `alembic heads`: `20260830_news_translation (head)`.
- 독립 checker 최초 판정은 `REWORK`(blocker 0, major 3)였다. M1 번역 검증이 summary까지 폐기하는 문제와 M2 한글+Latin 브랜드 제목 오판은 `ACCEPTED` 후 수정했다. M3 lock 실패 시 process-lock을 포기하라는 제안은 중복 OAuth·secret 경계 약화 때문에 `REJECTED_WITH_EVIDENCE`하고, 대신 raw `OSError`를 typed fail-closed 오류로 바꿨다. 관련 최종 집중 테스트 108건이 통과했다.
- 실 NH OAuth, 운영 migration, 운영 배포는 실행하지 않았다.

## 다음 세션이 바로 할 일
1. 사용자 배포 승인을 받은 뒤 운영 DB backup/precheck → `20260830_news_translation` migration → Core native 배포 순서로 적용한다.
2. NH PLUG 운영 검증 전에 Mac의 legacy/owner cache 존재·권한·만료를 **내용 출력 없이** 확인한다. 유효 cache가 있으면 배포 후 owner 파일 이관을 확인하되, 만료/부재 상태에서 실 API를 호출하면 다시 발급 알림이 갈 수 있으므로 사용자 승인 없이 호출하지 않는다.
3. 실제 영문 Reuters 등 신규 분석에서 번역 제목·발췌와 summary가 저장되고 두 KAsset API에 노출되는지 확인한다. 기존 row는 재수집/재분석 전까지 null이 정상이다.
4. readiness에서 라벨만으로 historical PIT를 신뢰하지 말고 실제 persisted constituent history, delisted 포함, list-date coverage를 promotion blocker로 결박한다. `includes_delisted=False` promotion-ready 테스트는 fail-closed 기대값으로 교정한다.
5. KIS HTTP 403, SCCO 2026-08-10, 신규 상장 `0126Z0`/`SPCX`, KRX APPROVAL→PAPER fill/reconcile, XKRX drift 경보는 기존 미종결 상태를 유지한다.

## 세션 이력
- 2026-08-30: 영문 뉴스 번역 제목/발췌, KAsset API 연결, NH PLUG owner cache·process lock 구현; readiness PIT 미종결 재판정.
- 2026-08-30: 운영 migration, KR/US 100종목 cohort·일봉·benchmark 적재, calendar/Yahoo 복구와 readiness 실측.
- 2026-08-30: PAPER promotion evidence/CLI, artifact fingerprint, position cycle, claim lease, AI shadow, migration CI gate 완료.
- 2026-08-29: 결정론적 PAPER 자동화·exact-version 승격 gate, 추천 시장·일일 횟수, AI 공급자·뉴스 경계 완료.
