RECORD:
DATE: 2026-09-14
SCOPE: 추천 응답 스키마, 사유·점수 조회 500, strict 응답 모델, 날짜 fixture
PATHS: app/schemas/ai_recommendations.py, tests/schemas/test_ai_recommendations_schema.py, tests/test_rob559_symbol_order_history.py
STATUS: accepted

병합: PR #64 `bac03e621`(`d795569f6`, `80cbab29c`)로 `main`에 병합됐다.

## 2026-09-14 — 자동매매 사유·점수 조회 응답 스키마 복구
### 원인·변경
- 완료 목록 `GET /api/v1/ai/recommendations?status=RESOLVED&limit=50`은 저장된 BUY 근거의 `portfolio.positionSizing.accountStateMultiplier`, `hardRisk.accountState`, `hardRisk.lossStreak`를 strict 응답 모델이 미등록 필드로 거부하여 HTTP 500이었다. 서버/DB 장애나 사유 유실이 아니며, 한 BUY 행의 변환 실패가 SELL을 포함한 목록 전체를 막았다.
- `app/schemas/ai_recommendations.py`: 세 필드만 optional로 선언했다. multiplier는 기존 `DecimalText`, 상태·연속손실은 원본 versioned JSON snapshot을 보존하는 `dict[str, object]`다. 과거 필드 부재는 `None`으로 유지하며 가짜 multiplier를 만들지 않는다. 기존 `extra="forbid"`·응답 필터·별칭은 유지한다.
- `tests/schemas/test_ai_recommendations_schema.py`: BUY의 세 필드와 사유·점수 보존, 실제 `position_exit` + standalone `hard_risk` SELL 근거, 새 필드가 없는 과거 sizing 응답을 방어하는 회귀 3건을 추가했다. standalone SELL은 원래도 builder를 통과하며 top-level `hardRisk`가 아닌 `evidence` 배열에 근거가 남는 기존 계약을 보존한다.
- 추가로 PR 전체 CI를 막던 `tests/test_rob559_symbol_order_history.py`의 고정 날짜 `2026-06-14`를 같은 파일의 기존 관례인 `datetime.now(UTC)`로 바꿨다. 9/14에는 고정 fixture가 92일 전이 되어 실제 90일 조회 창 밖으로 밀렸고, 이 날짜를 쓰던 6개 테스트만 실패했다. 운영 조회 기간·주문 로직은 변경하지 않았다. 변경 파일은 source 1개, test 2개, 이 문서다.

### 검증·배포 경계
- 로컬 Windows에서는 Python test/lint/type/build를 실행하지 않았다. 서버의 전용 임시 checkout과 일회성 2 CPU/3 GiB container, 운영과 분리된 `kasset-test-db`의 run-owned test DB로 검증했다. 운영 환경파일·볼륨·DB 데이터는 테스트에 사용하지 않았다.
- base source + 새 회귀: schema **1 failed / 5 passed, exit 1** (`extra_forbidden` 3필드). 같은 원인의 임시 HTTP 재현도 **1 failed, exit 1**이었다.
- 수정 source: `python -m pytest tests/schemas/test_ai_recommendations_schema.py tests/routers/test_ai_recommendations.py -q --tb=short -p no:cacheprovider` → **32 passed / 16 warnings / 22.20s, exit 0**. 임시 HTTP 재현은 목록·상세 200과 사유·점수·세 필드 보존을 확인하여 **1 passed, exit 0**였다. 서로 겹치는 schema-only 6 passed는 총계에 합산하지 않는다.
- 변경 두 파일 `ruff check`, `ruff format --check`와 source `ty check`는 모두 exit 0이다. 기존 OpenDartReader/Pydantic 경고가 남는다. 임시 재현 모듈·checkout·container·run-owned DB는 검증 후 제거했다.
- 독립 checker는 스키마 변경에 PASS를 반환했다. PR #64 최초 CI `34806358442`는 별도 날짜 fixture 문제로 shard 1에서 **6 failed / 4194 passed / 13 skipped**였으며 `ci-required` 실패를 우회하지 않았다. 날짜 한 줄 수정 후 Main의 격리 `python -m pytest tests/test_rob559_symbol_order_history.py -q --tb=short -p no:cacheprovider`는 **10 passed / 14 warnings / 8.23s, exit 0**, 해당 파일 Ruff/format도 exit 0이다. 최초 bridge-IP 검증은 socket guard가 setup을 차단해 테스트를 실행하지 못했고, guard를 바꾸지 않고 test DB container의 loopback namespace를 사용하여 통과했다. 임시 checkout/container를 제거했으며 기존 test DB 7개는 유지됐다.
- 독립 검수와 GitHub PR/필수 CI를 거쳐 기존 `Deploy` workflow로 반영한다. 마이그레이션은 없으며 거래 설정을 변경하지 않는다. 최종 운영 SHA/배포 상태는 [Deploy 실행 기록](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/workflows/deploy-kasset.yml)을 정본으로 확인한다.
- 배포 후 확인 대상은 기존 활성 모바일 세션의 인증 경계를 유지한 GET-only 완료 목록·매수/매도 상세, `/health`, 서비스 SHA/상태다. 실제 주문·승인 POST나 강제 자동매매 sweep으로 검증하지 않는다.
