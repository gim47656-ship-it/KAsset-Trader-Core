# 앱 자동매매·보유 보호 읽기 투영
RECORD:
DATE: 2026-09-27
SCOPE: 앱 자동매매 점검, owner cycle, 보유 보호 상태, API 계약
PATHS: app/extensions/kasset/api/router.py, app/extensions/kasset/api/paper.py, app/extensions/kasset/api/paper_schemas.py, app/schemas/ai_recommendations.py, app/services/paper_trading_service.py, tests/extensions/kasset/api/test_ai_trading_settings.py, tests/extensions/kasset/api/test_paper_regression.py, docs/API-CONTRACT.md
STATUS: partial

## 판단과 변경
- GET 상태의 `updatedAt`은 설정 저장시각으로 유지하고, 별도 조회시각과 owner별 최신 저장 automation cycle을 읽는다. 상태 게이트를 재평가하거나 저장하지 않는다.
- 현재 계좌 `NORMAL|STAGED_REDUCTION|EXIT_ONLY`는 저장된 추천 당시 증거나 SHADOW HWM만으로 현재라고 증명되지 않아 응답에 새 placeholder 필드를 만들지 않는다. 앱은 현재 미계측과 추천 당시 근거를 구분한다.
- PAPER 보유 조회는 기존 배치의 내부 position ID를 opt-in으로 읽고, owner/account/market/symbol/current cycle에 일치하는 저장 관리 기록만 `currentStop`과 `partialExitCompleted`로 보여 준다. 이전 기록은 `stale`, 기록 없음은 `missing`; `managementSavedAt`은 저장 시각이다. 최초 부분익절 가격은 현재 공개된 순수 규칙과 전략 버전을 결합한 신뢰 가능한 읽기 계약이 없어 제공하지 않는다.
- 기존 거래판단·주문 payload·게이트·설정 저장은 변경하지 않았다.

## 검증과 제한
- `git diff --check` (cwd `.tmp-verify/core-app-state`) exit 0.
- R3 Python 집중검사: 서버 `kasset-prod`의 격리 소스 `/tmp/kasset-app-state-01a0e071`, Docker image `kasset-app-state-test:01a0e071`, network `container:kasset-test-db`, `DATABASE_URL`/`AUTO_TRADER_TEST_DATABASE_URL`은 격리 PostgreSQL `test_db`. `/app/.venv/bin/python -m pytest tests/extensions/kasset/api/test_ai_trading_settings.py tests/extensions/kasset/api/test_paper_regression.py -q` exit 0, **63 passed, 2 warnings** (기존 Pydantic deprecated), test schema bootstrap 1 DB, external HTTP blocked 0 (`artifact://67`). 실제 DB fixture에서 다른 owner의 늦은 cycle을 배제하고, 다른 owner/account의 같은 심볼 및 이전 cycle을 현 보유 보호선으로 누출하지 않음을 확인했다.
- 같은 격리 소스 `/app/.venv/bin/ruff check app/extensions/kasset/api/router.py app/extensions/kasset/api/paper.py app/extensions/kasset/api/paper_schemas.py app/schemas/ai_recommendations.py app/services/paper_trading_service.py tests/extensions/kasset/api/test_ai_trading_settings.py tests/extensions/kasset/api/test_paper_regression.py` exit 0, `All checks passed!`. R2 `/app/.venv/bin/ty check` 같은 파일 exit 0, `All checks passed!`; R3 변경은 import 줄바꿈뿐이다.
- read-only HTTP smoke는 같은 image의 `FastAPI` `TestClient`로 GET `/api/v1/ai/trading/state`와 GET `/api/v1/positions?broker=PAPER`를 실제 라우팅했다(인증·DB는 격리된 대역, 실 데이터/주문 없음). 두 경로 모두 200, `updatedAt=2026-09-01T00:00:00Z`, `observedAt` UTC, `latestAutomationCycle:null`, `positions:[]`; exit 0. `passlib`/`bcrypt` 버전 경고가 stderr에 있었으나 route 결과는 정상이다.
- 환경 장애는 코드 실패가 아니었다: 최초 R2 image의 pytest exit 4 `ModuleNotFoundError: No module named 'pytest_asyncio'` (`tests/conftest.py:18`). image에 pyproject test 그룹을 Main이 격리 보충한 뒤 R3 pytest가 통과했다. Ruff 첫 R2 exit 1 `I001`은 수동 import 정렬 후 R3 exit 0. 첫 smoke는 필수 더미 `OPENDART_API_KEY`/`SECRET_KEY` 미설정으로 exit 1 (`artifact://70`); `tests/conftest.py:136-159`의 테스트 값만 주어 수정한 smoke는 exit 0.
- 운영 DB·프로세스·env는 사용하지 않았다. 실제 사용자 계좌·주문 관측과 project-wide 통합검사는 수행하지 않았고 Main이 소유한다. 사용자 앱에서는 운영 반영 전까지 optional 필드가 없을 수 있다.
