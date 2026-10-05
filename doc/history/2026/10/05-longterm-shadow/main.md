# KRX 장기 추세·재무성장 SHADOW

RECORD:
DATE: 2026-10-05
SCOPE: 장기 SHADOW 2후보, 주간 코호트, 20/60/120거래일 가상 성과, 벤치마크 초과수익
PATHS: app/extensions/kasset/automation/longterm_shadow.py, app/extensions/kasset/automation/longterm_shadow_service.py, app/models/kasset_longterm_shadow.py, alembic/versions/20261005_kasset_longterm_shadow.py, app/tasks/daily_candles_tasks.py, scripts/kasset_longterm_shadow.py, docs/runbooks/kasset-longterm-shadow.md
STATUS: accepted (미배포)

## 사용자 요구와 선택 근거

사용자가 장기전략 SHADOW 후보를 추천순으로 요청했고 1(장기 추세 추종)과 2(재무 품질·성장 + 추세 필터)를 승인했다. Main이 운영 DB를 읽기 전용으로 확인한 사실이 후보와 조건을 정했다.

- `kr_candles_1d`는 2025-01-07부터이며 보통주 2,420종목이 250세션 이상이다. ETF는 1,162개 중 2개만 250세션 이상이라 ETF 앙상블은 보류했다.
- `financial_fundamentals_snapshots`의 `roe`는 전 행 null이다. 분기 `discrete_revenue`·`discrete_net_income`·`filing_date`는 대부분 채워져 있다. 그래서 2번은 ROE 대신 공시일 기준 TTM 매출·순이익 성장으로 정의했다.
- `market_valuation_snapshots`는 8/29 전종목 1회 뒤 하루 5~15종목만 갱신돼(9/25 이후 갱신 23종목) PER/PBR 기반 가치·배당 후보는 수집 보완 전까지 쓰지 않는다.

월 1회 리밸런싱(20거래일 보유)을 매주 시작하는 격자형 코호트로 기록해 표본 축적 속도를 높였다. 코호트 간 보유 기간이 겹쳐 독립 표본이 아니다.

## 위임과 검수

Maker 1명(`LongtermShadow`, anthropic/claude-sonnet-5-5:high)이 구현·서버 격리 검증을 맡았다. 편집 전 체크포인트에서 Main은 #112 선례의 migration 체인 배선 6파일, 휴장일 서버 검증 실행, `filing_date <= S` 경계, `fundamentals_stale`(최신 분기 말이 S보다 200일 초과 이전) 추가를 승인했다. 상세는 [maker 기록](maker-LongtermShadow.md).

Main이 직접 확인한 diff 범위:

- 스윙 모듈은 품질 제외 추출·헬퍼 공개화·coverage의 `not_applicable` 처리만 바뀌었다. 같은 smoke DB에서 스윙 fingerprint `a501328c…f627`와 관측 결과가 기준 revision과 같았다([swing-invariance](evidence/swing-invariance.log)).
- 12-1 모멘텀은 `close[S-21]/close[S-252]-1`, SMA200 기울기는 20세션 전 SMA200과 비교한다. 재무는 `filing_date <= S` 행만, 최근 8분기 3개월 간격 연속을 요구한다.
- 코호트에 pending 구성원이 하나라도 있으면 그 코호트·horizon은 미성숙이고, 초과수익은 코호트와 벤치마크가 모두 mature일 때만 계산한다.
- 일봉 태스크는 플래그 기본 false, 스윙 다음 독립 실행이며 예외를 호출자에 던지지 않는다.

## 검증 증거

- [pytest](evidence/pytest-final.log): 신규 2 + 스윙 2(무수정) + 일봉 태스크, `91 passed`, `PYTEST_EXIT=0`. [migration·가드](evidence/pytest-migration-guards.log) `24 passed`.
- [정적 검사](evidence/static-final.log): Ruff check·format·ty exit 0.
- [alembic round-trip](evidence/alembic-roundtrip.log): upgrade→downgrade→upgrade exit 0.
- 격리 smoke(운영 시장자료 읽기 전용 복사): S=2026-10-02, 대상 2,478 중 평가 852. `trend_momentum` 통과 178·저장 20, `quality_growth_trend` 통과 44·저장 20(`fundamentals_stale` 36). 재실행 신규 0. report는 coverage `evaluated_with_signals`, 모든 horizon `pending`. [observe](evidence/smoke-observe-1.log), [report](evidence/smoke-report.log), [선정 종목](evidence/smoke-selected-signals.log).
- smoke에서 120거래일 forward 조회 창이 거래소 calendar 범위를 넘어 전 코호트가 `calendar_unavailable`이 되는 결함을 찾아 고쳤고 회귀 테스트를 추가했다.
- [격리 확인](evidence/isolation-cleanup.log): 운영 DB에 longterm 테이블 0개, 검증 DB·임시 디렉터리 정리.

서버 test 이미지에 git이 없어 `tests/ci`의 `test_cli_*` 9건은 서버에서 실행하지 못했다. GitHub Actions 결과로 확인한다. mature 코호트 집계는 합성 일봉 테스트로만 검증했다.
