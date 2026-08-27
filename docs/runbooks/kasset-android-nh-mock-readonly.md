# KAsset Android + NH PLUG Mock Read-Only 런북

이 런북은 `main` 브랜치의 Android API를 로컬에서 실행하고 KAsset Trader
Android 앱을 공개 계정 인증, `PAPER`, NH PLUG Mock Read-Only 경로에 연결하는
절차다. NH 실전 주문과 NH 운영 데이터 host는 이 범위에 없다.

## 1. 런타임 설정

`env.example`을 `.env`로 복사한 뒤 최소 다음 값을 설정한다.

| 목적 | 이 저장소의 변수 | 비고 |
|---|---|---|
| 실행 환경 | `ENVIRONMENT` | 외부 배포 문서의 `APP_ENV`에 해당 |
| 공개 주소 | 없음 | `PUBLIC_BASE_URL` 대신 TLS reverse proxy와 Android 계정 화면에 주소를 설정 |
| PostgreSQL | `DATABASE_URL` | Alembic과 서버가 같은 DB를 사용 |
| JWT 서명 | `SECRET_KEY` | 계정/device access·refresh token 서명 키 |
| Credential Vault | `CREDENTIAL_MASTER_KEY` | base64로 인코딩한 정확히 32바이트 키 |
| PAPER 주문 gate | `TRADING_ENABLED` | 기본 `false` |
| LIVE 주문 gate | `LIVE_TRADING_ENABLED` | Phase 1에서는 `false` 유지 |
| NH Mock 조회 gate | `NHPLUG_MOCK_ENABLED` | 기본 `false`; NH 조회 시에만 `true` |

`CREDENTIAL_MASTER_KEY` 생성 예:

```sh
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

키를 바꾸면 기존 Vault ciphertext를 복호화할 수 없다. DB 백업과 함께 별도 secret
store에 보관한다. `.env`, 사용자 비밀번호, NH App Key, NH App Secret, 전체 계좌번호는
Git에 넣지 않는다.

## 2. DB와 서버 시작

```sh
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

확인:

```sh
curl -sS http://127.0.0.1:8000/health
```

정상 응답은 `{"status":"ok"}`다. 운영에서는 Android가 입력할 공개 주소 앞에 TLS
reverse proxy를 둔다. 계정 등록·로그인, credential, token 요청을 평문 인터넷에 노출하지 않는다.

## 3. Android 연결

Android 프로젝트는 HANSE 저장소의 `KAsset-Trader/android`다.

```sh
cd KAsset-Trader/android
gradlew.bat :app:assembleDebug
```

계정 화면의 서버 주소:

- Android Emulator: `http://10.0.2.2:8000`
- `adb reverse tcp:8000 tcp:8000`을 쓴 실기기: `http://127.0.0.1:8000`
- 운영/원격 기기: TLS가 적용된 `https://...` 주소

`연결 확인` 후 `계정 만들기` 또는 `로그인`을 사용한다. 앱은 비밀번호를 저장하지
않고 access/refresh token만 Android Keystore에 저장한다. 로그아웃해도 서버 주소와
설치별 device ID는 유지된다.

## 4. PAPER 확인

1. 앱 설정에서 `PAPER`가 연결 상태인지 확인한다.
2. 홈에서 잔고, 보유 화면에서 포지션, 주문 화면에서 현재가를 확인한다.
3. 주문을 허용할 때만 `TRADING_ENABLED=true`로 서버를 다시 시작한다.
4. 시장가 또는 지정가를 preview한 뒤 제출한다.
5. 같은 `clientOrderId` 재시도는 기존 주문을 반환하고 새 주문을 만들지 않는다.
6. Kill Switch를 켠 뒤 신규 주문이 거절되는지 확인한다.

`PAPER`와 NH는 같은 Android `Balance`, `Position`, `Quote` DTO를 사용하지만 주문
capability는 다르다.

## 5. NH Credential 등록과 조회

서버에서 `NHPLUG_MOCK_ENABLED=true`를 설정한 뒤 앱 설정의 NH 항목에서 다음 값을
입력한다.

- App Key
- App Secret
- Account Number

등록 요청은 값을 서버로 한 번 전송한다. Android는 성공 즉시 입력 필드를 지우고 영구
저장하지 않는다. 서버는 AES-256-GCM으로 각각 암호화해 Credential Vault에 저장한다.
API 응답에는 masked App Key와 masked 계좌번호만 있고 App Secret은 없다.

`연결 확인`은 다음 순서로 동작한다.

1. Vault 복호화
2. NH OAuth token 발급(메모리 cache만 사용)
3. `POST /n2/acctinfo`
4. 입력 계좌가 응답 계좌 목록에 있는지 확인하고 allowlist에 결합
5. `lastVerifiedAt` 기록

검증 성공 후 지원하는 조회:

- `GET /api/v1/account/balance?broker=NH`
- `GET /api/v1/positions?broker=NH`
- `GET /api/v1/market/quote?broker=NH&market=KRX&symbol=005930`

NH data 요청은 `https://moapi.nhplug.com:8443`와 아래 path만 허용한다.

- `/n2/acctinfo`
- `/krstock/inquiry/v1/balance`
- `/krstock/quote/v1/currentPrice`

OAuth는 `https://api.nhplug.com:8443`만 사용한다. redirect, 다른 host/port/path,
미검증 계좌는 fail closed다.

## 6. NH 주문 차단

NH는 조회 전용이다. Android 주문 화면은 capability(`marketOrder=false`,
`limitOrder=false`)로 주문 버튼을 비활성화한다. 서버도 다음 요청을
`409 BROKER_READ_ONLY`와 `NH PLUG는 현재 모의 Read-Only 단계입니다.`로 거절한다.

- `POST /api/v1/orders` (`broker=NH`)
- `POST /api/v1/orders/{id}/cancel?broker=NH`
- `POST /api/v1/orders/{id}/amend?broker=NH`

UI 차단만으로 안전을 판단하지 않는다.

## 7. 검증

로컬 credential 없이 실행 가능한 계약·보안·PAPER 회귀:

```sh
uv run pytest -q tests/extensions/kasset/api
uv run pytest -q tests/services/brokers/nhplug tests/scripts/test_nhplug_mock_smoke.py
uv run ruff check app/extensions/kasset/api tests/extensions/kasset/api
uv run ty check app/extensions/kasset/api tests/extensions/kasset/api
```

Android:

```sh
gradlew.bat :app:testDebugUnitTest
gradlew.bat :app:lintDebug
gradlew.bat :app:assembleDebug
```

## 8. 별도 NH 외부 read-only smoke

이 smoke는 Core Credential Vault를 쓰는 Android 런타임 경로와 별도다. 저장소 밖의
`.env.nhplug-mock.native`에 정확히 세 값만 둔다.

```text
NHPLUG_APP_KEY=...
NHPLUG_APP_SECRET=...
NHPLUG_MOCK_ACCOUNT_NO=...
```

먼저 네트워크 호출 없는 preflight를 실행한다.

```sh
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke
```

실제 Mock 조회는 운영자가 명시적으로 `--confirm-read`를 붙일 때만 실행한다.

```sh
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke --mode account --confirm-read
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke --mode quote --symbol 005930 --confirm-read
```

출력은 credential, token, 계좌번호, broker response body를 표시하지 않는다. 실제
operator credential이 없으면 이 외부 smoke는 실행할 수 없으며 성공으로 간주하지 않는다.
