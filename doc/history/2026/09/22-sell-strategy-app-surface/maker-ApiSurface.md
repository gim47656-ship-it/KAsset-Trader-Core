# 추천 조회 500 오류와 만료 항목 노출 (ApiSurface)

RECORD:
DATE: 2026-09-22
SCOPE: ai-recommendations, android-api, response-schema, decimal-text, position-sizing-evidence, valid-until-expiry
PATHS: app/schemas/ai_recommendations.py, app/extensions/kasset/automation/position_sizing.py, tests/schemas/test_ai_recommendations_schema.py, tests/routers/test_ai_recommendations.py
STATUS: accepted

## 무엇을 고쳤나

### 1. 지수 표기 Decimal이 추천 조회를 500으로 떨어뜨리던 문제

`review.ai_recommendations.evidence` JSONB에 저장된 수량 문자열이 `"2.5E+2"` 형태로 남아 있으면
`RecommendationResponse` 검증이 `DecimalText`의 정규식 `^-?[0-9]+(?:\.[0-9]+)?$`에서 터졌다.
운영 로그의 오류 위치 `portfolio.positionSizing.caps.0.quantity`와 정확히 일치한다.

원인은 `PositionSizeCap.as_evidence()`가 `str(Decimal)`을 쓴 것이다. `Decimal` 나눗셈 결과는
지수 표기로 남을 수 있고(`Decimal("2.5E+2")`), `str()`은 그 표기를 그대로 낸다. 저가주 `005940`
추천에서 처음 그 형태가 나왔고 그때부터 목록·상세가 전부 500이 됐다.

두 경로를 함께 고쳤다.

- **응답 경로(필수)**: `app/schemas/ai_recommendations.py`에 `_plain_decimal_text()`를 두고
  `DecimalText = Annotated[str, Field(pattern=...), BeforeValidator(_plain_decimal_text)]`로 묶었다.
  지수 표기 문자열만 `format(Decimal(v), "f")`로 평문 표기로 되돌리고, 값은 바꾸지 않는다
  (`2.5E+2` → `250`). 평문도 지수 표기도 아닌 입력은 그대로 통과시켜 기존 검증 오류를 유지한다.
  최상위 필드용 `_validate_decimal_text()`도 같은 헬퍼를 거치게 했다. 이쪽은
  `field_validator(mode="before")`라 Annotated 검증보다 먼저 돌기 때문에, 한쪽만 고치면 여전히
  거절된다(`entryPrice='1.38E+5'` 실패로 실제 확인 — base 로그 참조).
  **이미 저장된 운영 행이 재배포·데이터 수정 없이 그대로 200이 되는 건 이 응답 경로 수정 덕분이다.**
- **저장 경로(재발 방지)**: `app/extensions/kasset/automation/position_sizing.py`에 `_decimal_text()`를
  추가하고 `PositionSizeCap.as_evidence()`·`PositionSizingResult.as_evidence()`의 모든 Decimal
  직렬화를 `str()` → `format(value, "f")`로 바꿨다. 저장소 다른 곳(`service.py`, 스키마)이 이미
  쓰던 `format(Decimal(x), "f")` 관례와 같다.

수치 의미는 바뀌지 않는다. `format(d, "f")`는 반올림·절삭 없이 표기만 고정소수점으로 바꾼다.
`2.5E+2`와 `250`은 같은 `Decimal`이다(테스트에서 `quantity == Decimal("250")`로 고정).

### 2. 만료된 PENDING 추천 노출 — 코드 변경 없음, 가정 정정

브리프는 `valid_until`이 지난 추천이 앱 PENDING 목록에 나온다고 보고 수정을 요구했다.
**서버에서 재현되지 않았다.** `app/services/ai_recommendations/repository.py:68-74`에
`or_(valid_until IS NULL, valid_until > now)` 필터가 이미 있고, 운영 컨테이너 `/app`에 배포된
소스도 동일했다(운영 HEAD `a2d0b1517` = 작업 base). 운영 DB 읽기 전용 조회에서
`pending_all=12, pending_visible=0`으로 12건 전부 이미 제외된다. 필터는 2026-08-27 이후 계속
존재했으므로 관측 구간(2026-09-22) 내내 목록 API가 만료 추천을 내보낸 적이 없다.

Main에 정정 보고했고 "코드 변경 없이 회귀 테스트로 닫아라"로 승인받았다. 기존 테스트가
`expired-pending` 행을 seed하면서도 `limit=1` 때문에 제외를 증명하지 못하던 것이 실제 빈틈이라,
그것을 증명하는 테스트를 새로 넣었다.

`valid_until IS NULL`인 PENDING 행이 목록에 남는 기존 계약은 그대로 뒀다
(`tests/routers/test_ai_recommendations.py`의 nullable 테스트가 이미 고정하고 있다).
운영에 그런 행은 0건이다.

## 앱에 옛 항목이 보인 현상 — 어디까지 서버에서 증명되나

- **증명됨**: 서버 목록 API는 만료 추천을 내보내지 않는다(필터 상시 존재 + `pending_visible=0` +
  회귀 테스트). 500 해소 후에도 만료 추천이 목록에 들어갈 경로는 없다.
- **증명됨**: 2026-09-22 11:20 KST부터 목록·상세가 전부 500이었으므로, 그 이후 화면에 보인 항목은
  서버의 성공 응답에서 온 것이 아니다.
- **미확인**: 사용자가 본 "예전에 닫았던 항목"이 앱 로컬 캐시의 잔상인지, 아니면 "이미 청산한
  종목에 대해 새로 나온 추천"을 가리킨 것인지는 서버에서 판별할 수 없다. 후자라면 후보 생성
  쪽(CandidateFlow/ExitStrategy 소유)의 문제이고 이 조각 범위 밖이다. 앱 캐시 가능성은 앱
  저장소가 이 PC에 없어 확인하지 못했다.

## 무엇을 하지 않았나

- `evidence[]` 원본 JSON은 손대지 않았다. 감사 기록이므로 저장된 `"2.5E+2"`가 그대로 나간다.
  타입이 붙은 `portfolio` 투영만 평문으로 나간다(테스트에서 양쪽 다 고정).
- 운영 DB를 쓰지 않았다. 조회만 했다. 기존 행을 UPDATE로 정규화하는 선택지는 쓰지 않았다 —
  응답 경로 수정으로 충분하고, 승인·거절 이력을 건드리지 않는 쪽이 맞다.
- DB 스키마·alembic migration 없음. wire 필드 이름·구조·별칭·`extra="forbid"` 유지. 앱 재배포 불필요.
- staging·commit·push 없음. 배포·sweep·실주문 없음.

## 검증

저장소 규약 14에 따라 로컬 Windows에서 Python test/lint/type을 돌리지 않았다. 서버
`root@100.73.186.78`의 격리 checkout `/tmp/kasset-apisurface-20260923`과 일회성 컨테이너
(2 CPU / 3 GiB, `ghcr.io/astral-sh/uv:python3.13-bookworm`), 운영과 분리된 `kasset-test-db`의
실행별 DB를 썼다. 운영 checkout `/opt/kasset-trader-core`, `.env.kasset`, 운영 DB/볼륨은
사용하거나 수정하지 않았다.

| 검사 | 결과 | 증거 |
| --- | --- | --- |
| `python -m pytest tests/schemas/test_ai_recommendations_schema.py tests/routers/test_ai_recommendations.py -q --tb=short -p no:cacheprovider` (cwd `/work`) | **36 passed, exit 0** | [evidence/apisurface-pytest.txt](evidence/apisurface-pytest.txt) |
| 수정 전 소스 + 새 테스트 (역증명) | **3 failed / 1 passed, exit 1** | [evidence/apisurface-baseline-pytest.txt](evidence/apisurface-baseline-pytest.txt) |
| `ruff check` / `ruff format --check` (4파일), `ty check --error-on-warning` (실행 코드 2파일) | 모두 **exit 0** | [evidence/apisurface-static-checks.txt](evidence/apisurface-static-checks.txt) |
| 운영 읽기 전용 조회 | 위 본문 근거 | [evidence/apisurface-prod-readonly.txt](evidence/apisurface-prod-readonly.txt) |

역증명이 중요하다. base 소스에서 새 테스트가 낸 오류는 운영 500과 같은 locator
(`portfolio.positionSizing.caps.0.quantity`), 같은 메시지, 같은 입력값(`'2.5E+2'`)이다.
반면 만료 제외 테스트는 base에서도 통과했다 — 만료 쪽은 애초에 결함이 아니었다는 뜻이다.
schema bootstrap DB 1개, socket guard 차단 0건, 외부 HTTP 차단 0건. 남은 경고 4건은
기존 Pydantic v2 deprecation이다.

## 추가한 테스트

- `test_build_recommendation_response_reads_stored_exponent_decimals` — 운영 저장값 `"2.5E+2"`가
  타입 투영에서 `"250"`으로 나가고, 최상위 `entryPrice`의 `"1.38E+5"`도 `"138000"`이 되며,
  원본 `evidence[]`는 저장된 그대로 남는다.
- `test_position_size_cap_evidence_is_plain_decimal_text` — 저장 시점이 다시 지수 표기를 쓰지 않는다.
- `test_stored_exponent_quantity_still_serves_list_and_detail` — 목록·상세 둘 다 200.
- `test_pending_list_excludes_recommendations_past_valid_until` — 만료 행 제외, 경계
  (`valid_until == now`)는 제외 쪽. `limit=50`이라 기존 테스트처럼 가려지지 않는다.

## 남은 경계

- 배포 후 앱에서 추천 목록·주문 근거 상세가 열리는지는 사용자 눈으로 확인해야 한다
  (열기 → 추천 목록 → 항목 선택 → 주문 근거. 통과 기준: 500 없이 화면이 뜨고 수량이 `250`처럼
  평문으로 보임). 서버에서 관측 가능한 부분(HTTP 200, 검증 통과, 수치 보존)은 위에서 닫았다.
- 종목명 대신 종목코드가 노출되는 건은 이 조각 소유가 아니다.
- 다른 생산 경로(`vertical_slice.py` 등)가 만드는 Decimal 문자열도 같은 지수 표기 위험이 있지만,
  응답 경로 정규화가 `DecimalText` 필드 전부를 덮으므로 500으로는 이어지지 않는다. 저장 시점
  정규화는 이 조각이 소유한 `position_sizing.py`에만 적용했다.
