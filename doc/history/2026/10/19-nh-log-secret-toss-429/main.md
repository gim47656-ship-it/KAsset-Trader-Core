# 2026-10-19 — nh-stream 로그의 NH PLUG secret 노출과 Toss 429 재시도 보완

RECORD: main-nh-log-secret-toss-429
DATE: 2026-10-19
SCOPE: nh-stream httpx 로그 NH PLUG appkey·appsecretkey 노출 차단, Toss GET 429 재시도 2회·limiter 재획득, 장중 점검(BEAR 국면 신규 BUY 정지)
PATHS: app/extensions/kasset/nhplug/runner.py, app/services/brokers/toss/client.py, tests/extensions/kasset/automation/test_realtime_runtime.py, tests/services/brokers/toss/test_client.py
STATUS: partial (PR #132 미머지·미배포, Toss 테스트 수정 전 실패 확인 대기)

## 요청
사용자: "장중감시하면서 잘돌아가나 지켜보고싶다" → 읽기 전용 점검에서 나온 문제 2건에 "2건보완해줘".

## 장중 관측(운영 읽기 전용, 2026-10-19 09:25~09:45 KST)
- 컨테이너 6종 `498920bc`(10-12 배포) 정상, worker ERROR 0, 10분 사이클·매분 보호청산 정상.
- 두 계정(4·7) 보유 0·예산 사용 0. 마지막 BUY는 10-12 13:34. 사이클 skip 사유는 `no_affordable_actionable_candidate`지만 후보 제외 사유는 `presizing_zero_quantity:REGIME_BLOCKED`(BEAR, `bear_risk_multiplier=0`)였다. 거래대금 상위 100종목 근사치로 20일선 위 13%·20일 수익률 중앙값 −10.5%(10-16 종가 기준)라 BEAR 판정과 맞는다. 하락장 매수 차단은 설계대로이며 바꾸지 않았다.
- NH 스트림은 10-14 이후 `no_demand`로 닫혀 있다(보유·실시간 BUY 대상 없음).

## 문제와 원인
1. **NH PLUG secret 평문 로그**: `nh-stream` 로그에 `POST https://api.nhplug.com:8443/oauth2/token?appkey=…&appsecretkey=…` 줄이 httpx INFO로 남았다(보존 로그 2줄, 10-13·10-14 토큰 발급). 다른 진입점(api·worker·mcp·CLI)은 `configure_dependency_log_levels()`로 httpx를 WARNING으로 올리지만 `app/extensions/kasset/nhplug/runner.py` `main()`은 `basicConfig(INFO)`만 했다. 토큰 발급 오류 메시지는 이미 secret을 가린다.
2. **Toss 429**: 10-13~10-19 worker 로그에서 분봉 수집 429가 39건, 장중 5분봉 `kasset intraday OHLCV unavailable … UpstreamUnavailableError`가 139건이었다. 5분봉 실패는 10분 사이클 시작 0~15초에 몰렸고 대형주(000660·005380 등)도 포함돼 종목 문제가 아니다. 이 실패는 장중 trigger를 `unavailable`로 만들어 그 사이클의 BUY 판정을 막는다. 분봉 수집 실패 중 `404 stock-not-found` 136건은 기존 backoff 대상이라 손대지 않았다.
   - 공식 한도는 `MARKET_DATA_CHART` 초당 20회(토큰 버킷)이고 로컬 limiter는 5회다. 운영 worker에서 캔들 1건 probe가 `limit=20, remaining=14`인 429를 받고 재시도에서 200을 받았다. 문서 밖 제한이 있다는 뜻이며 원인은 Toss 쪽이라 확인할 수 없다.
   - 클라이언트는 429에 한 번만 재시도했고, 그 재시도는 limiter를 거치지 않았다. Toss 문서는 `Retry-After` 대기와 지수 백오프+jitter를 권장한다.
   - `api` 컨테이너의 "429" 1,159줄은 `/healthz` 접속 포트 번호였다(Toss 429 아님).

## 변경(브랜치 `fix/nh-log-secret-toss-429`, PR #132)
- `nhplug/runner.py`: `main()`이 `configure_dependency_log_levels()`를 부른다.
- `brokers/toss/client.py` `_request`: GET은 429에 최대 2회 재시도(`retry_delay_seconds(attempt=0,1)`, `Retry-After` 우선), 매 재시도 전 그룹 limiter를 다시 거친다. GET 외(주문 POST 등)는 기존처럼 1회이고 `pre_send_hook`은 매 전송 전 그대로 돈다. `retry_on_429=False`는 0회.
- 테스트: `test_realtime_runtime.py`에 runner 로그 정책 회귀 1건, `test_client.py`에 GET 429 회복·포기·주문 POST 1회 유지 3건.

## 검수·검증
- 교차 리뷰(OpenAI `gpt-6-astra`) 1회차 지적 2건(메서드 대소문자 정규화, 테스트의 signal 핸들러 전역 상태)을 고쳤고 2회차 지적 0건으로 resolve했다(`xr-20261019004400-4ae3e2`).
- 로컬 실행 금지(AGENTS.md 14)라 GitHub Actions로 검증한다.
- 수정 전 실패: 테스트만 담은 `a1cea71`의 [CI 39181950018](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/39181950018) shard 4에서 `test_runner_main_hides_httpx_request_lines_that_carry_nh_secrets`가 `assert not httpx_logger.isEnabledFor(logging.INFO)` → `assert not True`로 실패했다(나머지 4,198 통과, lint 통과). 이후 리뷰 반영으로 멈추는 지점만 signal 설치 전으로 옮겼고 단언은 같다.
- Toss 새 테스트가 있는 shard 1은 shard 4 실패로 fail-fast 취소됐고, job 단독 재실행도 실행 없이 취소됐다. 서버 격리 pytest는 장중(08:50~16:20) 금지라 16:20 뒤 `a1cea71`에서 확인한다.

## 남은 판단
- 노출된 NH PLUG app key·secret 교체 여부는 사용자 결정이다. 로그는 운영 서버 json-file(20MB×3)에만 있고, 배포로 `nh-stream` 컨테이너가 다시 만들어지면 이전 로그는 사라진다.
