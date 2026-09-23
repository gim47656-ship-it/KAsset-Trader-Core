# KRX 우선주 종목명 fallback (SymbolName)

RECORD:
DATE: 2026-09-23
SCOPE: kasset, paper-positions, closed-trades, krx-quotes, preferred-share, symbol-name-fallback, kr_symbol_universe
PATHS: app/extensions/kasset/api/paper.py, app/extensions/kasset/api/krx_quotes.py, tests/extensions/kasset/api/test_preferred_symbol_names.py, tests/extensions/kasset/api/test_market_quotes.py, tests/extensions/kasset/api/test_paper_regression.py
STATUS: accepted

## 무엇을 고쳤나

앱 보유·내역 화면에서 우선주가 한글명 대신 종목코드로 보였다. 원인은 서버가 `name=null`을
내려보낸 것이고, 앱은 이름이 없으면 종목코드를 그대로 렌더한다(앱 렌더링 자체는 정상).

`symbol_master`의 CHECK(`app/models/symbol_master.py:16`)는 KRX에 `COMMON_STOCK`/`ETF`만
허용하므로 우선주는 그 표에 **행이 없다**. 운영 DB 읽기 전용 확인에서
`000155`·`000157`·`005935`는 `symbol_master`에 없고, `kr_symbol_universe`에
`두산우`·`두산2우B`·`삼성전자우`로 `is_active=true`다
([evidence/symbolname-prod-readonly.txt](evidence/symbolname-prod-readonly.txt)).

이름 해석 두 경로에 **2차 fallback**을 넣었다. 둘 다 새 쿼리를 만들지 않고 기존 공용 서비스
`app/services/kr_symbol_universe_service.py:585`의 `get_kr_names_by_symbols(symbols, db=db)`를
그대로 쓴다(그 함수가 이미 `is_active` 필터와 `strip()`을 한다).

- `app/extensions/kasset/api/paper.py:666` `PaperAccountAdapter._position_names` —
  `positions`·`closed_trades` 두 표면이 공유하는 이름 해석. `SymbolMaster` 조회 결과에 없는
  **KRX** 심볼만 모아 1회 배치 조회로 채운다. US는 대상이 아니다.
- `app/extensions/kasset/api/krx_quotes.py:593` `_instrument_names` — 시세 응답의 종목명.
  같은 규칙으로 미해석 KRX 심볼만 2차 조회한다.

지킨 계약:

- `SymbolMaster`가 해석한 이름이 우선이다. 그 결과는 한 건도 바뀌지 않는다
  (`005930`·`005940`·`138040`은 `symbol_master`에 있고 이름이 `kr_symbol_universe`와 같다).
- `name`이 비었거나 `strip()` 결과가 심볼과 같으면 채우지 않는다. 종목코드를 `name`에
  복사하지 않는다. 이 규칙은 2차 조회 결과에도 똑같이 적용한다.
- 2차 조회 실패는 이름만 비운다. `_instrument_names`의 기존 `except` + `logger.warning`
  패턴을 따르고, `_position_names`도 같은 모양으로 감쌌다. `_instrument_names`에서 1차
  `SymbolMaster` 조회가 실패하면 기존 계약대로 `{}`를 반환하고 2차 조회로 넘어가지 않는다.
- 조회는 심볼 집합당 1회다. 포지션마다 도는 N+1이 없다.

새 helper 모듈은 만들지 않았다. 쿼리 자체는 기존 서비스 함수가 공용이고, 남는 차이는
"어떤 키가 미해석인가"와 로그 문구뿐이라 각 호출부에 두는 편이 배치상 깔끔했다
(순환 import 회피: `krx_quotes` → `paper` 방향만 존재한다).

## 손대지 않은 것

- `symbol_master` 스키마·CHECK·적재 경로, `alembic/versions`(새 파일 0), 앱 저장소.
- 추천(`ai_recommendations`) 경로, US 종목 이름 경로, 손익 집계 로직.
- wire 필드 이름·구조. 앱 재배포 불필요.
- `symbol_master.security_type` 분류(스크리너 유니버스 분모 의미).

## 기존 테스트 2건 수정 — 계약이 바뀐 곳

`symbol_master`가 못 푼 KRX 심볼이 있으면 이름 조회가 1회에서 **최대 2회**가 된다.
"이름 조회는 총 1회"를 구현 세부로 고정하던 두 테스트를 새 계약에 맞게 고쳤다.
해석 결과(응답 `name`)는 그대로다.

- `tests/extensions/kasset/api/test_market_quotes.py` `_FakeDb`가 `kr_symbol_universe` 조회를
  따로 세도록 확장(`universe_reads`). 배치 테스트는 `name_reads == 1` + `universe_reads == 1`,
  `symbol_master`가 해석한 경우는 `universe_reads == 0`, 어디에도 없는 심볼은
  `universe_reads == 1` + `name is None`을 고정한다.
- `tests/extensions/kasset/api/test_paper_regression.py`
  `test_positions_enrich_equities_in_one_market_aware_master_query`도 같은 방식으로
  `master_reads == 1` / `universe_reads == 1`을 본다.

## 추가한 테스트

`tests/extensions/kasset/api/test_preferred_symbol_names.py` (11건)

- `_position_names`: 우선주 3종이 `kr_symbol_universe`에서 채워지고 조회가 심볼 집합당 1회.
- `SymbolMaster`가 아는 심볼은 그 값이 이기고 2차 조회 대상이 아니다.
- 어디에도 없는 심볼과 이름이 코드뿐인 행은 채우지 않는다(null 유지).
- US 심볼은 KRX universe로 메우지 않는다.
- 2차 조회 실패에도 이미 해석한 이름이 남고 예외가 나가지 않는다.
- `_instrument_names`: 우선주 채움 / `SymbolMaster` 우선 / US 미대상 / 2차 조회 실패 내성.
- 응답 표면: `positions`·`closed_trades`의 `name`에 `두산우`·`두산2우B`·`삼성전자우`가
  실리고 `005930`은 `삼성전자`, 미해석 심볼은 `None`이다.

## 검증

저장소 규약 14에 따라 로컬 Windows에서 Python test/lint/type을 돌리지 않았다. 서버
`kasset-server`(`root@100.73.186.78`)의 격리 checkout `/tmp/kasset-symbolname-20260923`
(base `c1dad9060ada`, GitHub에서 clone) + 일회성 container + 운영과 분리된 `kasset-test-db`의
run-owned DB에서 실행했다. 운영 checkout `/opt/kasset-trader-core`, `.env.kasset`, 운영 DB/볼륨은
사용하지 않았고 운영 DB는 SELECT만 했다.

| 검사 | 결과 | 증거 |
| --- | --- | --- |
| 신규 테스트 집중: `pytest tests/extensions/kasset/api/test_preferred_symbol_names.py -q --tb=short -p no:cacheprovider` | **11 passed, exit 0** | [evidence/symbolname-pytest-focus.txt](evidence/symbolname-pytest-focus.txt) |
| 변경 범위 + 인접 모듈: `pytest tests/extensions/kasset/api tests/extensions/kasset/automation/test_job.py -q --tb=short -p no:cacheprovider` | **434 passed, exit 0** | [evidence/symbolname-api-pytest.txt](evidence/symbolname-api-pytest.txt) |
| 수정 전 소스 + 최종 신규 테스트 (역증명) | **5 failed / 6 passed, exit 1** | [evidence/symbolname-baseline-pytest.txt](evidence/symbolname-baseline-pytest.txt) |
| `ruff check` / `ruff format --check` (5파일) | 모두 **exit 0** | [evidence/symbolname-static-checks.txt](evidence/symbolname-static-checks.txt) |
| `ty check --error-on-warning` (실행 코드 2파일) | **exit 0** | [evidence/symbolname-static-checks.txt](evidence/symbolname-static-checks.txt) |
| 운영 DB 읽기 전용 관측 | 위 본문 근거 | [evidence/symbolname-prod-readonly.txt](evidence/symbolname-prod-readonly.txt) |

역증명이 이 변경의 핵심 증거다. base 소스에서 `positions` 응답 이름이 `[None, '삼성전자']`로
나온다 — 앱이 종목코드를 렌더하던 실제 증상과 같은 모양이고, `name=null`이 서버에서 온 것임을
확정한다. 실패 5건은 전부 우선주 해석 계약을 겨눈 것이고, 통과 6건은 이번 변경이 건드리지 않는
계약(`SymbolMaster` 우선, US 비대상, 조회 실패 내성)이라 base에서도 통과한다.

`kasset-server`는 2-vCPU 운영 호스트다. 이 실행은 2026-09-23 09:48–09:53 KST, 즉 KRX 정규장
중이었다. `docs/runbooks/server-pytest-runner.md`는 장중(08:50–16:20 KST) 실행을 피하라고
적고 있어 그 조건을 지키지 못했다. 완화책으로 runbook이 쓰는 `--cpus=1.0` 상한을 그대로
걸었고, 실행 전후 load average는 `1.20 → 1.30`(before/after)이었다. Main이 판단할 사실로 남긴다.

## 남은 경계

- 앱 화면에서 실제로 `두산우`처럼 한글로 보이는지는 사용자 눈으로 확인해야 한다
  (앱 열기 → 보유 또는 내역 → 우선주 종목. 통과 기준: 종목코드 대신 한글명).
  서버가 내려보내는 wire 값(`name`)까지는 위 테스트로 닫았다.
- 이 checkout은 `git clone`이라 `uv sync` 없이 배포 이미지 `kasset-trader-core:c1dad9060ada`의
  `/app/.venv`와 `kasset-pytest-deps-4e6329d1`(uv.lock 핀과 같은 pytest 9.1.1/pytest-asyncio 1.3.0)를
  썼다. `app` 패키지는 `PYTHONPATH=/work`로 내 checkout이 이긴다(`app: /work/app/__init__.py` 확인).
- `tests/extensions/kasset/automation/` 전체와 저장소 전체 스위트는 이 조각 범위가 아니라
  돌리지 않았다. `tests/extensions/kasset/automation/test_job.py`(krx_quotes를 import하는 인접
  모듈)는 함께 돌렸다.
- 커밋·브랜치·push·PR은 만들지 않았다. Main이 같은 워크트리에서
  `app/services/paper_trading_service.py`·`tests/test_paper_trading_service.py`를 동시에
  고치고 있어 커밋하면 변경이 섞인다. 마감은 Main이 한 브랜치로 묶는다.
  내 delta는 위 5파일 + 이 기록이다.
