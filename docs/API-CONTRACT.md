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
