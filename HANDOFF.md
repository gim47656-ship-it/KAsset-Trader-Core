# HANDOFF — KAsset-Trader-Core

갱신: 2026-08-27 (Android 추천 검토 API 기능 브랜치 구현·로컬 검증)

## 프로젝트 개요와 사용자가 원하는 방향

이 저장소는 KAsset Trader Android가 연결할 실제 Core다. Android의 `추천` 탭은 AI 추천을 누적 조회하고 근거를 검토한 뒤 `APPROVED` 또는 `REJECTED`를 검토 기록으로만 저장한다. 추천 승인은 증권사 주문 승인이 아니며 주문 생성·미리보기·제출·정정·취소·라우팅·체결을 호출하지 않는다. 기존 브로커 게이트, 레저, reconcile, 스케줄러 동작은 보존한다.

사무실과 집에서 같은 Git 기능 브랜치를 이어서 작업하고, 실제 실행 환경은 Naver Cloud 서버의 기존 Core를 사용한다. 개인키 원문과 서버 secret은 저장소나 문서에 기록하지 않는다.

## 전체 진행 상태

- 완료: `feature/android-recommendation-review`에서 추천 조회·결정 API, PostgreSQL 모델·서비스·라우터·Alembic migration과 계약 테스트 구현.
- 완료: 로컬 임시 PostgreSQL에서 추천 계약 테스트 22개, 관련 인증 회귀 테스트 28개, Ruff와 ty 전체 검사 통과.
- 완료: 새 migration `20260827_ai_recommendations` 단독 upgrade → downgrade → upgrade 검증.
- 대기: Naver Cloud의 기존 Core 경로·브랜치·DB migration 상태 확인과 실제 배포. 배포 승인은 아직 없으며 수행하지 않음.
- 대기: 클라우드 Core에 저장된 추천으로 Android 실제 E2E 확인.
- 완료: 최종 독립 Checker `PASS`. 차단 finding 없음.

## 이번 세션에서 한 일

- `review.ai_recommendations`에 추천 사실 스냅샷과 단일 terminal 결정을 저장하도록 추가했다.
- `GET /api/v1/ai/recommendations?status=PENDING|RESOLVED&limit=50`을 추가했다.
- `POST /api/v1/ai/recommendations/{id}/decision`을 추가했다. 본문은 `{ "decision": "APPROVED|REJECTED" }`만 허용한다.
- 동일 terminal 결정 재전송은 같은 결과를 반환하고, 다른 결정은 `409 RECOMMENDATION_STATE_CONFLICT`로 거절한다.
- `APPROVED`는 BUY/SELL, 비어 있지 않은 rationale, 미래 validUntil에서만 허용한다.
- 결정 갱신은 `decision`, `decided_at`, `updated_at`만 변경하며 주문 계열 모듈을 import하거나 호출하지 않는다.
- 기존 API middleware가 Android Bearer token도 검증하도록 확장하고 기존 web session fallback은 유지했다.
- `Authorization` 헤더가 있으면 형식 오류나 무효 Bearer를 web session으로 우회하지 않고 401로
  거절하는 fail-closed 동작을 의도적으로 유지했다.

검증:

```text
uv run pytest tests/routers/test_ai_recommendations.py -q
→ 22 passed

uv run pytest tests/test_auth_middleware.py tests/middleware/test_auth_telegram_branch.py tests/test_middleware_auth_research_reports_ingest.py tests/test_news_ingestor_ingest_token_auth.py -q
→ 28 passed

uv run ruff check .
→ All checks passed!

uv run ty check
→ All checks passed!

uv run alembic heads
→ 20260827_ai_recommendations (head)

새 migration 단독 upgrade → downgrade -1 → upgrade head
→ 모두 exit 0
```

전체 Alembic chain을 extension 없는 임시 PostgreSQL에 처음부터 적용하는 검증은 기존 migration `87541fdbc954_add_kr_candles_timescale.py`가 TimescaleDB 2.8.1 이상을 요구해 중단됐다. 새 migration 자체의 왕복 검증은 통과했다.

Checker 비차단 관찰: 현재 recommendation producer가 없고 DB는 evidence가 배열인지만 강제한다.
향후 producer를 추가할 때 `RecommendationEvidence`와 같은 shape를 쓰도록 검증해야 하며, 그렇지
않으면 잘못 저장된 evidence 한 행이 목록 직렬화 500을 만들 수 있다.

## 다음 세션이 바로 할 일

1. Naver Cloud ACG에서 작업 장소의 공인 IP만 SSH 22에 `/32`로 허용한다.
   - 현재 사무실 공인 IP: `1.235.75.165/32`
   - 집에서는 `https://api.ipify.org`로 당시 공인 IP를 확인해 별도 `/32` 규칙을 추가한다.
   - `0.0.0.0/0 → 22`는 사용하지 않는다. IP가 바뀌면 기존 `/32`를 교체한다.
2. 각 PC에는 서로 다른 SSH key pair를 권장한다. private key는 각 PC 로컬에만 두고, 서버 `authorized_keys`에는 public key만 추가한다. 현재 사무실에서 확인된 로컬 key 후보는 `C:/Users/hanse/Downloads/ncp-aitestbed-user-084.pem`이며 원문은 절대 커밋하지 않는다.
3. `ssh -i <LOCAL_PRIVATE_KEY> <SERVER_USER>@175.45.20.51`로 접속해 `id -un`, `/etc/os-release`, Core 작업 경로, Git remote·branch·commit, 실행 서비스와 DB migration revision을 읽기 전용으로 먼저 확인한다. 서버 사용자와 Core 경로는 아직 미확인이다.
4. 서버 코드가 `gim47656-ship-it/KAsset-Trader-Core`와 같은 저장소인지 확인한 뒤, PR이 merge된 커밋만 배포한다. 서버에서 feature branch를 직접 운영 배포하지 않는다.
5. TimescaleDB가 있는 서버/동등 환경에서 `alembic upgrade head`를 적용하고, 추천 fixture를 저장해 Android에서 대기 목록 → 상세 → 승인/거절 → 처리됨 이동 → 재접속 영속화를 확인한다.
6. 추천 결정 전후 주문·브로커 레저 행 수가 변하지 않는지 확인한다.

현재 접속 차단 증거:

```text
ssh 175.45.20.51:22
→ Connection timed out (10초)

Whale의 OMP Browser Relay 탭 연결(재시도 포함)
→ Browser open timed out (30초)
```

## 세션 이력

- 2026-08-27: Android 추천 검토 API와 영속화 migration 구현, 로컬 PostgreSQL·인증 회귀 검증 완료. Naver Cloud SSH는 ACG timeout으로 대기.
