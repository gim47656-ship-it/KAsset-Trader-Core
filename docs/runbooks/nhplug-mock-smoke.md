# NHPLUG 모의 read-only smoke

이 문서는 NHPLUG 모의투자 통합의 **read-only 1단계** 런북이다. 계좌목록과 국내주식 잔고만 조회한다. 시세·주문·정정·취소·MCP 주문 도구·레저·reconcile·스케줄러는 이 단계에 존재하지 않는다.

## 안전 경계

- 데이터 클라이언트는 `https://moapi.nhplug.com:8443`만 허용한다. 다른 scheme·호스트·포트·경로는 거부한다.
- 요청을 만든 뒤 `send` 직전에 `request.url.scheme`, `request.url.host`, `request.url.port`, path를 다시 확인한다. OAuth와 데이터 양쪽에서 `follow_redirects=False`를 명시한다. 3xx는 따라가지 않고 실패한다. 이는 호스트 경계만이 아니라 APP KEY/SECRET 경계다. httpx는 cross-origin redirect에서 custom credential header를 자동 제거하지 않을 수 있으므로 redirect를 따르면 secret이 외부 host로 전달될 수 있다.
- 데이터 allowlist는 `/n2/acctinfo`와 국내 잔고 두 path뿐이며, allowlist 검사는 토큰 해석과 소켓 생성 전에 실행된다.
- 계좌목록에서 `acct_type=03`인 값만 allowlist에 넣는다. `01`·`02`는 거부 타입 상수이며, 동일 계좌번호가 상충하는 type으로 중복되면 전체 응답을 거부한다. `NHPLUG_MOCK_ACCOUNT_NO`도 반드시 broker 응답의 `03` allowlist에 있어야 한다. 검증된 allowlist는 dispatcher에 bind되며 balance dispatch는 이를 필수로 사용한다.
- 잔고 요청은 시작 시 configured `act_no` allowlist를 확인하고, 최초 전송과 token refresh 재전송 각각의 `send` 직전에 다시 확인한다.
- 접근토큰은 벤더 제약상 운영 OAuth 호스트에서만 발급된다. 운영 호스트를 아는 코드는 `app/services/brokers/nhplug/auth.py` 하나이며, `POST /oauth2/token`, `POST /oauth2/revoke`만 allowlist한다. OAuth dispatch도 데이터 dispatch와 같은 `NHPLUG_MOCK_ENABLED` master gate 뒤에 있다. 데이터 클라이언트는 운영 호스트 상수나 import를 갖지 않는다.
- 벤더 Python SDK는 의존하거나 import하지 않는다. 호스트는 env가 아닌 코드 상수이며, `NHPLUG_BASE_URL`과 `NHPLUG_AUTH_URL`은 읽지 않는다.

## Access Token cache와 재발급 계약

공식 참고 구현은 [Access Token 전역 cache 안내](https://github.com/PLUG-OpenAPI/nhplug-sdk/tree/main/snippets/auth/token_cache)와 [`nh_token.py`](https://github.com/PLUG-OpenAPI/nhplug-sdk/blob/main/snippets/auth/token_cache/nh_token.py)다. SDK 전체를 의존성으로 추가하지 않고, 이 read-only client에 필요한 cache·재발급 계약만 적용한다. 공식 구현이 지적하듯 Access Token을 메모리에만 두면 프로세스를 시작할 때마다 재발급되고 매번 알림톡이 발생한다.

- 기본 cache는 owner별 `~/.nhplug/token_cache.<owner_fingerprint>.json`이다. 조회 순서는 **프로세스 메모리 → owner 파일 → 유효한 legacy 단일 파일 → 신규 발급**이다. 기존 `~/.nhplug/token_cache.json`이 현재 owner에 유효하면 OAuth를 다시 발급하지 않고 owner 파일로 원자 이동한다.
- JSON에는 `base`, `owner_fingerprint`, `token`, `exp`만 저장한다. `owner_fingerprint`는 raw App Key와 고정 auth base 조합의 SHA-256이며 App Key 원문과 App Secret은 저장하지 않는다. `base`와 fingerprint가 모두 현재 client와 일치해야 재사용한다. 서로 다른 공용·사용자 App Key는 각자의 owner 파일을 사용한다.
- `exp`가 현재 시각보다 60초 넘게 남아 있어야 유효하다. JSON 손상, 필드·타입 오류, 다른 owner, 다른 auth base, 만료 또는 60초 이내 만료는 cache miss이며 신규 발급으로 진행한다.
- cache parent는 mode `700`이고, 최종 owner cache와 같은 디렉터리에 만드는 매번 다른 임시 파일은 모두 mode `600`이다. 임시 파일을 flush·`fsync`한 뒤 `os.replace`로 owner 파일을 원자 교체한다. cache 파일에는 실제 Bearer token이 있으므로 공유·커밋·화면 출력·로그 첨부를 금지한다.
- `get_access_token`은 owner cache별 process lock을 잡은 뒤 파일을 다시 확인한다. 동일 owner의 blue/green 프로세스가 동시에 cold miss해도 먼저 발급한 token을 뒤 프로세스가 재사용한다. `force_refresh=True, failed_token=...`도 다른 요청 또는 프로세스가 이미 저장한 다른 유효 token이 있으면 그것을 재사용한다.
- `revoke_access_token`이 성공하면 revoke한 토큰과 일치하는 메모리·owner·legacy cache만 무효화한다. 경쟁 중 더 최신 토큰으로 바뀐 cache는 지우지 않는다.
- data client는 HTTP `401` 또는 response body의 `IGW40043`만 토큰 무효로 분류한다. 첫 응답에서만 force refresh하고 동일한 path와 `Input_0`를 한 번 더 보낸다. 두 번째 `401`/`IGW40043`은 그대로 실패하며 세 번째 전송은 없다.
- HTTP `429` 또는 `IGW42902`는 호출 한도 오류이지 토큰 오류가 아니다. 토큰을 폐기·재발급하지 말고 호출 간격을 조정한다. host·path·`acct_type=03` account 검사는 최초 전송과 토큰 refresh 후 재전송 각각의 `send` 직전에 다시 실행한다.

cache mode 교정이 실패해도 파일을 읽을 수 있으면 검증된 token을 재사용한다. cache 본문 읽기·쓰기 자체가 실패하면 자격증명 값을 오류나 로그에 넣지 않고 cache miss로 처리하며, 이 경우 신규 발급과 알림톡이 다시 발생할 수 있다. 반복 알림이 보이면 owner 파일의 소유권·권한과 `~/.nhplug` 쓰기 가능 여부를 먼저 확인하되 cache 내용을 출력하지 않는다.

## 보장 강도와 제거 불가 위험

보장 강도는 **"우발 방지 + 정적 검출"**이다. 구조적 불가능이라는 주장이 아니다.

- 운영 계좌는 같은 고객번호 아래 실재하고 같은 APP KEY로 접근 가능하다. `acct_type=03` allowlist는 벤더 격벽이 아니라 이 코드가 거는 검증이다.
- Kiwoom live read-only의 3중 방어 ③(계좌번호를 프로세스 환경에 두지 않음)에 대응물은 없다. NH는 `/n2/acctinfo` 응답에 운영 계좌도 항상 함께 내려주므로, 프로세스가 운영 계좌를 전혀 알지 못하게 만드는 방법이 벤더 설계상 없다.
- 벤더 기본값은 운영이다. 이 때문에 벤더 SDK를 사용하지 않고, mock data host와 auth host를 물리적으로 분리한 자체 클라이언트를 사용한다.
- OAuth 토큰은 운영에서 발급되고 양쪽 환경에 통용된다. auth path allowlist는 그 예외를 두 path로 좁힐 뿐, 운영 토큰 발급 자체를 제거하지 못한다.

## 자격증명 파일과 게이트

운영자가 직접 전용 파일을 만든다. 구현자는 파일을 만들거나 키 값을 이동하지 않는다.

```dotenv
NHPLUG_APP_KEY=...
NHPLUG_APP_SECRET=...
NHPLUG_MOCK_ACCOUNT_NO=...
```

권장 파일명은 `.env.nhplug-mock.native`다. 이 파일에는 정확히 위 세 키만 있어야 하며 `DATABASE_URL`을 포함하면 안 된다. `NHPLUG_MOCK_ENABLED=true`은 파일이 아니라 실행 환경에서 별도로 명시한다. CLI는 파일명 또는 `ENV_FILE`에 `prod`가 있으면 거부하고, 누락·추가 키는 **이름만** 보고한다.

`NHPLUG_MOCK_ENABLED`은 default-disabled다. 미설정 또는 truthy가 아닌 값에서는 모든 OAuth 및 데이터 dispatch가 fail-closed 된다.

## 실행

다음 명령은 계좌번호·토큰·키 값·원문 broker body를 출력하지 않는다.

```bash
# 네트워크 0회: gate, env-file shape, static read allowlist만 확인
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke \
  --env-file .env.nhplug-mock.native --mode preflight

# `/n2/acctinfo`로 acct_type=03을 검증한 뒤 국내 잔고를 조회
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke \
  --env-file .env.nhplug-mock.native --mode account --confirm-read

```

모드는 정확히 `preflight`, `account` 둘뿐이다.

- `preflight`: 네트워크 0회. 전용 파일·gate·read-only allowlist를 확인한다.
- `account`: 계좌목록을 받아 `acct_type=03`으로 `NHPLUG_MOCK_ACCOUNT_NO`를 검증하고, 그 검증된 계좌의 잔고만 조회한다.

`account`는 `--confirm-read`도 요구한다. 이는 조회의 추가 운영자 의도 확인이며, 주문용 gate가 아니다.

## dry-run / confirm 계약

`app.services.brokers.nhplug.contracts.DryRunConfirmContract`는 미래 action을 위한 타입 계약이다. 기본은 `dry_run=True`, `confirm=False`이며 non-dry action은 `confirm=True` 없이는 허용되지 않는다.

그러나 이 단계에는 그 타입을 소비하는 dispatch 메서드가 없다. 주문 관련 메서드·endpoint·TR을 만드는 것은 범위 밖이며, AST guard가 알려진 주문 endpoint/TR, vendor SDK import, 운영 host 문자열의 잘못된 위치, host-override env 참조를 빌드에서 차단한다.

## 현재 정지점

2026-09-01 운영 read-only smoke에서 `acct_type=03` 모의계좌 검증과 잔고 응답 `rsp_cd=00000`을 확인했다. NH 현재가 endpoint는 HTTP 400을 반환해 runtime surface에서 제거했으며, 한국 주식 시세는 Toss 공용 경로가 담당한다.
