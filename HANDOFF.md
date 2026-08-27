# HANDOFF — KAsset-Trader-Core

갱신: 2026-08-27 (NH PLUG 토큰 영속 캐시 PR #3·계좌 유형 discovery)

## 프로젝트 개요와 사용자가 원하는 방향

이 저장소는 KAsset Trader Android가 연결할 실제 Core다. NH PLUG 연동은 현재 모의투자 Read-Only 단계이며 계좌목록·국내주식 잔고·보유종목·현재가만 조회한다. 주문·정정·취소·출금·이체는 구현하지 않는다. OAuth는 NH 공식 계약상 운영 `api.nhplug.com:8443`에서 발급하고 모의 데이터는 `moapi.nhplug.com:8443`만 호출한다. App Key, App Secret, Access Token, 전체 계좌번호는 로그·문서·Git에 기록하지 않는다.

별도 PR `feature/android-recommendation-review`에는 Android 추천 검토 API가 구현돼 있다. 추천 승인은 검토 기록일 뿐 증권사 주문을 생성하지 않는다.

## 전체 진행 상태

- 완료: `fix/nhplug-persistent-token-cache`에서 NH OAuth 메모리 전용 캐시를 프로세스 간 파일 캐시로 전환.
- 완료: 공식 참고 계약에 맞춰 만료 60초 여유, owner fingerprint, 원자적 교체, `700/600` 권한을 구현.
- 완료: `401` 또는 `IGW40043`에서만 정확히 1회 재발급·재시도하고 `429/IGW42902`에서는 재발급하지 않도록 구현.
- 완료: 기존 OAuth 두 path, mock read-only 세 path, redirect 차단, `NHPLUG_MOCK_ENABLED`, `acct_type=03` allowlist와 주문 금지를 유지.
- 완료: NH 관련 집중 테스트 77개와 변경 파일 Ruff·ty 검사 통과.
- 완료: 전체 Ruff·ty와 독립 Checker `PASS`. 전체 pytest는 기존 Windows 전용 collection 오류 23건으로 중단됐으며 NH 집중 테스트는 통과.
- 완료: commit `88a20afa`, branch `fix/nhplug-persistent-token-cache`, PR #3 생성.
- 차단: ACL 제한 로컬 파일의 정확한 credential로 OAuth와 `/n2/acctinfo`는 성공했다. 서로 다른 `acct_type=03` 모의계좌가 3개이고 모두 끝 네 자리가 `2097`이라 자동 선택하지 않았다. 사용할 전체 모의계좌번호 입력이 필요하다.
- 대기: Core 추천 PR #2 merge·Naver Cloud 배포와 Android live E2E. 배포 요청 전에는 실행하지 않음.

## 이번 세션에서 한 일

- NH 공식 참고 구현 `PLUG-OpenAPI/nhplug-sdk/snippets/auth/token_cache`를 읽고 현재 `NHPlugAuthClient`가 인스턴스 메모리만 사용해 프로세스 재시작마다 토큰을 재발급하는 원인을 확인했다.
- 기본 `~/.nhplug/token_cache.json`에 `base`, owner fingerprint, token, expiry만 저장하도록 구현했다. Raw App Key와 App Secret은 캐시에 저장하지 않는다.
- 캐시 parent/file을 POSIX `700/600`으로 제한하고 unique temporary file을 `fsync`한 뒤 `os.replace`로 교체한다.
- 다른 client/process가 실패 토큰과 다른 새 토큰을 이미 저장한 경우 새로 발급하지 않고 그 토큰을 재사용한다.
- 데이터 요청 본문을 한 번만 직렬화해 최대 두 번 같은 bytes로 보내며, 각 send 직전에 gate·host·path·account allowlist를 다시 검사한다.
- SDK 전체 의존성은 추가하지 않았다. 기존 static guard의 vendor SDK import 금지와 주문 endpoint 금지를 유지했다.
- Checker의 parent symlink와 raw response code 파싱 관찰은 모두 비차단이다. 전자는 동일 OS 사용자 전용 경로에서 권한을 `700`으로 강화하는 동작이고, 후자는 공식 샘플과 같은 비 JSON `IGW40043` 탐지를 보존하므로 변경하지 않았다.
- 최초 안전 조회에서 `acct_type=01` 실계좌 1개와 `03` 모의계좌 3개를 확인했다. 전체 계좌번호·토큰·Secret은 출력하거나 저장하지 않았다.

검증:

```text
uv run pytest tests/services/brokers/nhplug/test_auth.py tests/services/brokers/nhplug/test_client.py tests/scripts/test_nhplug_mock_smoke.py tests/services/brokers/nhplug/test_static_guard.py -q
→ 77 passed

uv run ruff check <변경 Python 파일>
→ All checks passed!

uv run ty check app/services/brokers/nhplug/auth.py app/services/brokers/nhplug/client.py scripts/nhplug_mock_smoke.py
→ All checks passed!

uv run ruff check . && uv run ty check
→ All checks passed! / All checks passed!

uv run pytest -q
→ 기존 Windows 비호환 `fcntl`, `SIGHUP`과 frozen source SHA 등 23 collection errors로 중단

NH OAuth와 `/n2/acctinfo` 안전 discovery
→ 성공, acct_type 01: 1개 / acct_type 03: 3개
→ 03 세 계좌의 끝 네 자리가 모두 2097이라 자동 선택 중단
```

최초 테스트 실행은 test dependency group이 설치되지 않아 `ModuleNotFoundError: pytest_asyncio`로 중단됐고, `uv sync --group test --group dev` 후 같은 집중 테스트가 통과했다. 이는 코드 실패가 아니라 새 clone의 의존성 준비 문제였다.

## 다음 세션이 바로 할 일

1. 사용할 전체 `acct_type=03` 모의계좌번호를 로컬
   `C:/Users/hanse/.secrets/kasset-nhplug.env`의 `NHPLUG_MOCK_ACCOUNT_NO`에 추가한다.
2. 정확한 세 값으로 서버 root 전용 env와 token cache를 저장하고 account/quote smoke를 실행한다.
3. PR #3과 기존 추천 PR #2를 검토·merge한다. 실제 배포는 사용자가 명시적으로 요청한 경우에만 merge된 `main`으로 수행한다.

## 세션 이력

- 2026-08-27: NH 토큰 캐시 PR #3 생성, OAuth·계좌목록 성공 후 동일 끝자리의 모의계좌 3개 중 사용자 선택 대기.
- 2026-08-27: Android 추천 검토 API PR #2 구현·로컬 PostgreSQL 검증 완료.
