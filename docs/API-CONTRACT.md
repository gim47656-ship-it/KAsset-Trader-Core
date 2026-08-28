# KAsset Trader API Contract

## 국내주식 10단 호가

`GET /api/v1/market/orderbook?market=KRX&symbol=005930`

- 인증: `/api/v1/market/quote`와 동일한 모바일 세션 인증이 필요하다.
- `market`은 `KRX`, `symbol`은 숫자 6자리 국내 종목코드만 허용한다. 그 외 입력은 `422 VALIDATION_ERROR`이다.
- 조회가 들어온 종목은 NH PLUG 모의투자 WebSocket 실시간호가 KRX 채널에 구독된다. 마지막 조회 후 60초가 지나면 구독과 해당 스냅샷을 제거한다.
- `asks`는 최우선 매도호가부터 가격 오름차순, `bids`는 최우선 매수호가부터 가격 내림차순이며 각각 최대 10단이다.
- 가격, 잔량, 총잔량은 JSON number가 아닌 십진수 문자열이다.
- 아직 첫 실시간 프레임을 받지 못했거나 장 마감으로 데이터가 없으면 오류 대신 `ready=false`를 반환한다. 클라이언트는 1초 간격으로 계속 폴링한다.
- `NHPLUG_MOCK_ENABLED=false`, 서버 공용 자격 미설정, 또는 WebSocket 인증 실패 시 `409 BROKER_NOT_CONNECTED`를 반환한다.

준비된 응답:

```json
{
  "symbol": "005930",
  "market": "KRX",
  "ready": true,
  "asOf": "2026-08-28T05:30:00+00:00",
  "source": "NH_PLUG_WS",
  "asks": [
    {"price": "260500", "volume": "1234"}
  ],
  "bids": [
    {"price": "260000", "volume": "5678"}
  ],
  "totalAskVolume": "43210",
  "totalBidVolume": "98765"
}
```

아직 준비되지 않은 응답:

```json
{
  "symbol": "005930",
  "market": "KRX",
  "ready": false,
  "asOf": null,
  "source": "NH_PLUG_WS",
  "asks": [],
  "bids": [],
  "totalAskVolume": "0",
  "totalBidVolume": "0"
}
```

NH PLUG 정본은 `https://www.nhplug.com/llms-full.txt` 및 그 문서가 지목한 국내주식 `openapi.json`의 `x-realtime-channels`이다. 모의투자 접속 주소는 `wss://moapi.nhplug.com:17070/websocket`, 채널은 `tr_cd=ob`, 구독 키는 6자리 `code`다. access token은 WebSocket HTTP 헤더가 아니라 구독 메시지의 `header.token`으로 전달한다.

## 홈 시장 개요

`GET /api/v1/market/overview`

- 인증된 KAsset 모바일 세션이 필요하다.
- 응답 필드는 camelCase이며 모든 가격·등락 값은 JSON number가 아닌 십진수 문자열이다.
- `indices`는 `KOSPI`, `KOSDAQ`, `SPX`, `NASDAQ`, `fx`는 `USDKRW`, `JPYKRW`, `EURKRW` 순서로 항상 7개 항목을 유지한다. 개별 조회가 실패해도 항목을 제거하지 않고 숫자 필드를 `null`, `status`를 `unavailable`로 반환한다.
- `changeRate` 단위는 퍼센트 포인트다. 예를 들어 `"0.70"`은 `0.70%`이며 클라이언트가 다시 100을 곱하지 않는다.
- `JPYKRW`는 관행적인 100엔 단위가 아니라 **1 JPY당 KRW**, `EURKRW`는 **1 EUR당 KRW**다. USD-base open.er-api 스냅샷 한 건의 양수 유한 환율만 검증해 교차 환율을 계산하므로 단위 배율을 임의로 바꾸지 않는다.
- `status`는 전체 7개 항목이 `available`이면 `fresh`, 하나라도 `stale` 또는 `unavailable`이면 `partial`, 7개 모두 `unavailable`이면 `unavailable`이다. 항목 `status`가 시세 신선도의 권한자이며 클라이언트는 자체 시간 차 계산으로 덮어쓰지 않는다.
- KRX는 기존 지수 응답의 `data_state`를 그대로 신선도 권한으로 사용한다. US는 정규장 직접 시세만 `available`, 프리마켓·애프터마켓·휴장 또는 일봉 fallback은 `stale`이다. FX는 검증된 스냅샷이면 `available`이며 서버가 항목 시각으로 별도 나이 계산을 하지 않는다.
- `sessionState`와 `sessions[].state`는 `OPEN`, `PREOPEN`, `AFTER_HOURS`, `CLOSED` 중 하나다. 지수 항목에만 `sessionState`가 있고 FX 항목은 `null`이다. `sessions` 순서는 `KRX`, `US`다.
- `errors`는 실패 항목마다 `{scope, symbol, code}`를 제공한다. `scope`는 `indices` 또는 `fx`, `code`는 `UNAVAILABLE` 또는 `TIMEOUT`이다. 공급자 이름이나 원본 예외 문자열은 모바일 계약에 노출하지 않는다.
- `asOf`는 공급자가 제공한 시각만 사용한다. KRX 지수는 실제 quote timestamp, open.er-api는 실제 `time_last_update_unix`가 있을 때만 채운다. US 지수나 open.er-api 응답에 공급자 시각이 없으면 `null`이며 서버 현재 시각을 시세 시각으로 만들지 않는다. 최상위 `asOf`는 파싱 가능한 항목 시각 중 가장 최신 값이고, 아무 값도 없으면 `null`이다.
- 응답 스냅샷은 프로세스 내에서 60초 동안 단일비행 캐시한다. 지수와 FX 소스 그룹은 동시에 조회하며 각각 6초로 제한한다. 한 그룹 또는 항목이 실패해도 HTTP `200`으로 `partial`/`unavailable` 계약을 반환한다.

정상 응답 예:

```json
{
  "asOf": "2026-08-28T06:00:00Z",
  "status": "fresh",
  "indices": [
    {
      "symbol": "KOSPI",
      "name": "KOSPI",
      "market": "KRX",
      "currency": "KRW",
      "price": "2700.10",
      "changeAmount": "18.90",
      "changeRate": "0.70",
      "asOf": "2026-08-28T14:00:00+09:00",
      "status": "available",
      "sessionState": "OPEN"
    },
    {
      "symbol": "KOSDAQ",
      "name": "KOSDAQ",
      "market": "KRX",
      "currency": "KRW",
      "price": "900.20",
      "changeAmount": "-2.10",
      "changeRate": "-0.23",
      "asOf": "2026-08-28T14:01:00+09:00",
      "status": "available",
      "sessionState": "OPEN"
    },
    {
      "symbol": "SPX",
      "name": "S&P 500",
      "market": "US",
      "currency": "USD",
      "price": "6500.50",
      "changeAmount": "20.15",
      "changeRate": "0.31",
      "asOf": null,
      "status": "available",
      "sessionState": "OPEN"
    },
    {
      "symbol": "NASDAQ",
      "name": "NASDAQ",
      "market": "US",
      "currency": "USD",
      "price": "21000.25",
      "changeAmount": "84.5",
      "changeRate": "0.40",
      "asOf": null,
      "status": "available",
      "sessionState": "OPEN"
    }
  ],
  "fx": [
    {
      "symbol": "USDKRW",
      "name": "USD/KRW",
      "market": "FX",
      "currency": "KRW",
      "price": "1500.00",
      "changeAmount": null,
      "changeRate": null,
      "asOf": "2026-08-28T06:00:00Z",
      "status": "available",
      "sessionState": null
    },
    {
      "symbol": "JPYKRW",
      "name": "JPY/KRW",
      "market": "FX",
      "currency": "KRW",
      "price": "10.00",
      "changeAmount": null,
      "changeRate": null,
      "asOf": "2026-08-28T06:00:00Z",
      "status": "available",
      "sessionState": null
    },
    {
      "symbol": "EURKRW",
      "name": "EUR/KRW",
      "market": "FX",
      "currency": "KRW",
      "price": "2000",
      "changeAmount": null,
      "changeRate": null,
      "asOf": "2026-08-28T06:00:00Z",
      "status": "available",
      "sessionState": null
    }
  ],
  "sessions": [
    {"market": "KRX", "state": "OPEN"},
    {"market": "US", "state": "OPEN"}
  ],
  "errors": []
}
```

개별 실패 항목 예:

```json
{
  "symbol": "KOSDAQ",
  "name": "KOSDAQ",
  "market": "KRX",
  "currency": "KRW",
  "price": null,
  "changeAmount": null,
  "changeRate": null,
  "asOf": null,
  "status": "unavailable",
  "sessionState": "OPEN"
}
```
