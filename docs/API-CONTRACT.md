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

## 주식 시세 (단일·배치)

`GET /api/v1/market/quote?broker=PAPER&market=KRX&symbol=005930`

`GET /api/v1/market/quotes?market=KRX&symbols=005930,000660`

`GET /api/v1/market/quote?broker=PAPER&market=US&symbol=TQQQ`

- 인증된 KAsset 모바일 세션이 필요하다. 미인증은 `401 UNAUTHORIZED`다.
- 배치 응답은 `{"quotes": [Quote, ...]}`이며 `Quote`는 단일 조회와 완전히 같은 camelCase 계약(`broker`, `market`, `symbol`, `name`, `currency`, `price`, `previousClose`, `changeAmount`, `changeRate`, `session`, `regularClose`, `sessionChangeAmount`, `sessionChangeRate`, `asOf`, `source`)이다. 모든 가격·등락 값은 JSON number가 아닌 십진수 문자열이다.
- `symbols`는 콤마 구분 1~50개다. `KRX`(`KR`)는 6자리 종목코드, `US`(`NASDAQ`·`NYSE`·`AMEX`)는 미국 티커만 허용한다. 중복은 서버가 제거하고 응답에서도 한 번만 나온다. 개수·형식·시장 위반은 `422 VALIDATION_ERROR`다.
- 조회에 실패한 종목은 값을 만들어내지 않고 응답 배열에서 제외한다. 요청 순서는 성공한 종목 사이에서 유지된다.
- KRX 시세 우선순위는 **토스 인증 REST 배치 → 서버 공용 NH PLUG 채널 → 저장 일봉(PAPER)**, US는 **토스 인증 REST 배치 → 저장 일봉(PAPER)** 이다. 앞 단계가 실패하면 조용히 다음 단계로 강등하며, 어떤 단계도 서버 현재 시각을 시세 시각으로 위조하지 않는다. `source`가 실제 채널을 구분하며 자격증명이나 공급자 원문 예외는 응답·로그 어디에도 담지 않는다.
- `TOSS_API_ENABLED`가 꺼져 있으면 토스 가격 단계를 시도하지 않고 기존 폴백 경로를 쓴다.
- 토스 가격 단계는 종목당 2초 캐시 + 종목별 단일비행으로 보호한다. 같은 종목을 동시에 조회하는 여러 요청은 한 번의 배치 호출로 합쳐지고, 배치 호출 실패 직후 2초는 재호출하지 않는다. `TossReadClient`는 프로세스에서 한 번 만들어 재사용하고 앱 lifespan 종료 때 닫는다.
- 배치에서 NH 공용 채널은 전 종목 공용 호출 간격(0.45초)에 묶여 있어 총 2.5초 예산까지만 시도하고, 예산을 넘긴 종목은 저장 일봉으로 강등한다. 응답 안에서 종목별 `source`가 서로 다를 수 있다.
- `asOf`는 항상 UTC `Z` 표기다. 토스가 `+09:00` 오프셋으로 준 시각은 UTC로 정규화하며, 공급자가 시각을 주지 않거나 오프셋 없는 시각을 주면 그 종목은 다음 폴백으로 내려간다. 저장 일봉 시세의 `asOf`는 그 일봉 거래일의 `T00:00:00Z`다.
- `session`은 Toss market calendar 원문 구간을 `DAY_MARKET`, `PRE_MARKET`, `REGULAR`, `AFTER_MARKET`, `CLOSED`로 옮긴 값이다. US만 `DAY_MARKET`을 가질 수 있다. KR calendar는 KRX+NXT 통합 구간만 주므로 별도 `NXT` 상태를 만들지 않는다. `singlePriceAuctionStart/End`는 상위 구간 안의 동시호가 경계라 별도 상태로 쪼개지 않으며, 공급자 명세가 제외한 시간외종가·시간외단일가는 이 계약에도 포함하지 않는다. KRX의 `PRE_MARKET`·`AFTER_MARKET`은 NXT 전용이므로 종목 universe의 최신 `nxt_eligible`·`nxt_trading_suspended`가 참여 가능함을 증명할 때만 그 상태를 내리고, 미지원·정지 종목은 `CLOSED`, 근거가 없거나 오래되었으면 `null`이다. calendar 조회 자체가 실패해도 휴장으로 만들지 않고 `null`이다.
- `price`는 현재 세션 최신가이고 `previousClose`는 전일 정규장 종가다. `changeAmount`·`changeRate`는 정규장 진행 중에는 현재가, 정규장 종료 뒤에는 `regularClose`를 `previousClose`와 비교한다. `regularClose`는 완료된 정규장의 마지막 1분봉 종가이며 정규장 진행 중에는 `null`이다. `sessionChangeAmount`·`sessionChangeRate`는 현재가를 `regularClose`와 비교한다. 정규장 종가를 증명할 수 없으면 세 필드는 `null`이고, 기존 `changeAmount`·`changeRate`는 이전 호환 동작인 현재가-전일종가로 강등한다. 등락률 단위는 퍼센트 포인트이고 소수 둘째 자리로 반올림한다.
- 정규장 종료 뒤 처음 조회하는 종목은 Toss 1분봉 요청 1회를 추가로 사용한다. 결과는 `(symbol, 정규장 종료 시각)`으로 캐시하므로 같은 정규장 기준의 후속 폴링·스트림은 재조회하지 않으며, 실패만 60초 동안 음수 캐시한다.
- 클라이언트는 홈 화면과 관심종목 시세를 15초 주기로 폴링한다. 서버 개요 캐시도 15초라 최악 지연이 폴링 주기 수준으로 유지된다.

배치 응답 예:

```json
{
  "quotes": [
    {
      "broker": "PAPER",
      "market": "KRX",
      "symbol": "005930",
      "name": "삼성전자",
      "currency": "KRW",
      "price": "256500",
      "previousClose": "250000",
      "changeAmount": "6500",
      "changeRate": "2.60",
      "session": "REGULAR",
      "regularClose": null,
      "sessionChangeAmount": null,
      "sessionChangeRate": null,
      "asOf": "2026-08-28T09:44:26Z",
      "source": "TOSS_API_PRICES"
    }
  ]
}
```

## 실시간 시세 스트림

`WS /api/v1/market/stream`

- `status.pollingTopics`는 앱이 기존 REST 시세 경로로 폴링해야 하는 토픽이다. 후속 `status`에서 목록이 비면 폴링을 중단한다.
- `status.reason`은 다음 값 중 하나다.
  - `UPSTREAM_UNAVAILABLE`: 토스 상향 연결을 사용할 수 없다.
  - `UPSTREAM_BLOCKED`: 토스가 상향 연결을 차단했다. 서버 허용 IP 설정을 확인해야 한다.
  - `UPSTREAM_SYNCING`: 상향 연결은 살아 있지만 declare ack가 아직 확정되지 않았다. 앱은 `pollingTopics`를 임시로 폴링하고 다음 `status`를 기다린다.
  - `TOPIC_BUDGET_EXCEEDED`: 전체 수요가 토스 연결의 100토픽 상한을 넘어 allocator가 해당 토픽을 제외했다.
  - `TOPIC_REJECTED_BY_PROVIDER`: 토스가 해당 토픽을 거부했다.
- `TOPIC_BUDGET_EXCEEDED`는 실제 allocator 제외 토픽에만 사용한다. ack 대기 중인 토픽에는 `UPSTREAM_SYNCING`을 사용한다.

## 홈 시장 개요

`GET /api/v1/market/overview`

- 인증된 KAsset 모바일 세션이 필요하다.
- 응답 필드는 camelCase이며 모든 가격·등락 값은 JSON number가 아닌 십진수 문자열이다.
- `indices`는 `KOSPI`, `KOSDAQ`, `SPX`, `NASDAQ`, `fx`는 `USDKRW`, `JPYKRW`, `EURKRW` 순서로 항상 7개 항목을 유지한다. 개별 조회가 실패해도 항목을 제거하지 않고 숫자 필드를 `null`, `status`를 `unavailable`로 반환한다.
- `changeRate` 단위는 퍼센트 포인트다. 예를 들어 `"0.70"`은 `0.70%`이며 클라이언트가 다시 100을 곱하지 않는다.
- `USDKRW`는 Toss Open API가 설정되어 정상 응답하면 그 값을 우선하고, 실패하거나 미설정이면 open.er-api 값으로 강등한다. `JPYKRW`는 관행적인 100엔 단위가 아니라 **1 JPY당 KRW**, `EURKRW`는 **1 EUR당 KRW**다. 두 교차 환율은 USD-base open.er-api 스냅샷 한 건의 양수 유한 환율만 검증해 계산한다.
- `status`는 전체 7개 항목이 `available`이면 `fresh`, 하나라도 `stale` 또는 `unavailable`이면 `partial`, 7개 모두 `unavailable`이면 `unavailable`이다. 항목 `status`가 시세 신선도의 권한자이며 클라이언트는 자체 시간 차 계산으로 덮어쓰지 않는다.
- KRX는 기존 지수 응답의 `data_state`를 그대로 신선도 권한으로 사용한다. US는 정규장 기본 batch 또는 직접 시세만 `available`, 프리마켓·애프터마켓·휴장 또는 단일 심볼 current 실패의 일봉 fallback은 `stale`이다. FX는 검증된 스냅샷이면 `available`이며 서버가 항목 시각으로 별도 나이 계산을 하지 않는다.
- `sessionState`와 `sessions[].state`는 `DAY_MARKET`, `PRE_MARKET`, `REGULAR`, `AFTER_MARKET`, `CLOSED` 중 하나이며 calendar를 읽지 못하면 `null`이다. `DAY_MARKET`은 US에만 나타난다. 지수 항목에만 `sessionState`가 있고 FX 항목은 `null`이다. `sessions` 순서는 `KRX`, `US`다.
- `errors`는 실패 항목마다 `{scope, symbol, code}`를 제공한다. `scope`는 `indices` 또는 `fx`, `code`는 `UNAVAILABLE` 또는 `TIMEOUT`이다. 공급자 이름이나 원본 예외 문자열은 모바일 계약에 노출하지 않는다.
- `asOf`는 공급자가 제공한 시각만 사용한다. KRX 지수는 실제 quote timestamp, Toss USD/KRW는 `validFrom`, open.er-api는 실제 `time_last_update_unix`가 있을 때만 채운다. US 지수나 공급자 응답에 시각이 없으면 `null`이며 서버 현재 시각을 시세 시각으로 만들지 않는다. 최상위 `asOf`는 파싱 가능한 항목 시각 중 가장 최신 값이고, 아무 값도 없으면 `null`이다.
- 응답 스냅샷은 프로세스 내에서 15초 동안 단일비행 캐시한다. 앱 홈 폴링 주기(15초)와 같은 값이라 서버 캐시와 앱 폴링이 겹쳐 지연이 누적되지 않는다. 지수와 FX 소스는 동시에 조회하며 각각 6초로 제한한다. 한 소스 또는 항목이 실패해도 HTTP `200`으로 `partial`/`unavailable` 계약을 반환한다.
- 기본 지수 조회에서 KRX 두 종목과 US 조회를 병렬로 실행하고, US의 `SPX`·`NASDAQ`은 한 번의 일봉 batch 조회에서 마지막 두 유효 거래일을 사용해 현재가·전일 대비·시가·고가·저가·거래량을 계산한다. 한 US 심볼의 행이 비어 있어도 다른 심볼은 유지한다. 단일 심볼 지수 조회는 기존 current+history 경로를 그대로 사용한다.

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

## 홈 지수 상세

`GET /api/v1/market/indices/{symbol}?range=1W|1M|3M|6M`

- 인증된 KAsset 모바일 세션이 필요하다. `{symbol}`은 `KOSPI`, `KOSDAQ`, `SPX`, `NASDAQ` 중 하나이며 대소문자는 구분하지 않는다. 지원하지 않는 심볼은 `404 UNKNOWN_INDEX`, 허용하지 않는 `range`는 `422`다.
- 응답은 `summary`와 `candles`로 구성한다. `summary`의 가격·등락 값과 모든 candle 값은 JSON number가 아닌 십진수 문자열이다. 공급자 이름, 내부 상태 필드, 원본 예외 문자열은 노출하지 않는다.
- 범위는 `1W = day/5`, `1M = day/20`, `3M = day/60`, `6M = week/26`으로 기존 실제 지수 history 조회에 전달한다.
- `candles`는 `{time, open, high, low, close, volume}` 형태다. 거래일만 제공되는 history의 `time`은 실제 체결시각으로 만들지 않고 해당 거래일의 `T00:00:00Z` bucket으로 표현한다.
- `volume`은 `null`일 수 있다. 국내 지수 시세 원천은 지수 봉의 거래량을 제공하지 않으며, 이때 `0`을 만들어 넣지 않고 `null`로 둔다. 클라이언트는 가격만 표시한다.
- candle은 날짜 오름차순이고 같은 bucket은 마지막 실제 행 하나만 유지한다. 날짜 또는 OHLC 중 하나라도 유효한 실제 값이 없으면 그 행을 제외하며 값을 합성하지 않는다.
- `summary.status`, `summary.sessionState`, `summary.asOf`는 홈 시장 개요와 같은 서버 규칙을 사용한다. 현재 지수 값이 없거나 조회가 실패하면 숫자와 `asOf`를 `null`, `status`를 `unavailable`로 반환하되 HTTP 상태는 `200`이다.
- 응답은 정규화된 `symbol + range`별로 프로세스 내 15초 단일비행 캐시한다. 다른 심볼 또는 다른 범위는 별도 키다.

```json
{
  "summary": {
    "symbol": "SPX",
    "name": "S&P 500",
    "market": "US",
    "currency": "USD",
    "price": "6500.5",
    "changeAmount": "20.15",
    "changeRate": "0.31",
    "asOf": null,
    "status": "available",
    "sessionState": "OPEN",
    "range": "1M"
  },
  "candles": [
    {
      "time": "2026-08-28T00:00:00Z",
      "open": "6480",
      "high": "6510",
      "low": "6475",
      "close": "6500.5",
      "volume": null
    }
  ]
}
```

## 종목 뉴스·공시 목록

`GET /api/v1/market/news?market=KRX&symbol=005930&kind=all&limit=20`

- 인증된 KAsset 모바일 세션이 필요하다. 없는 종목도 오류가 아니라 `200`과 `{"items": [], "nextCursor": null}`을 반환한다.
- `market`과 `symbol`은 선택이다. `market`은 다른 KAsset 시장 조회와 같은 표기인 `KRX`(`KR`) 또는 `US`(`NASDAQ`·`NYSE`·`AMEX`)를 대소문자 구분 없이 받는다. `symbol`은 앞뒤 공백을 제거하고 대문자로 정규화한다.
- `kind`는 `all`, `news`, `disclosure` 중 하나이며 기본값은 `all`이다. 저장값 `feed_source == "dart"`만 `disclosure`, 나머지 모든 공급자는 `news`다. `feed_source`와 공급자 식별자는 응답에 노출하지 않는다.
- `limit`은 `1..50`, 기본값은 `20`이다. `cursor`는 서버가 발급한 opaque keyset 토큰만 허용하며 잘못되거나 변조된 값은 `422 VALIDATION_ERROR`다. 토큰은 `(article_published_at, id)` 경계를 함께 보존하므로 같은 시각의 여러 항목도 다음 페이지에서 누락하거나 중복하지 않는다.
- 정렬은 `article_published_at DESC NULLS LAST, id DESC`다. 게시시각이 없는 실제 행도 값을 만들지 않고 `publishedAt=null`로 목록 마지막에 둔 뒤 `id DESC`로 결정적으로 정렬한다.
- 저장 `article_published_at`은 KST 벽시각을 담은 timezone 없는 열이다. 서버는 이를 먼저 KST로 해석한 뒤 UTC `Z`로 변환한다. 예를 들어 저장값 `2026-08-29 00:00:00`은 `2026-08-28T15:00:00Z`다.
- `is_analyzed=false`인 항목도 그대로 조회한다. AI 분석 완료 여부는 이 목록의 전제 조건이 아니다.

```json
{
  "items": [
    {
      "kind": "disclosure",
      "title": "주요사항보고서(자기주식취득결정)",
      "summary": null,
      "source": "DART",
      "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260829000123",
      "publishedAt": "2026-08-28T15:00:00Z",
      "symbol": "005930",
      "stockName": "삼성전자"
    }
  ],
  "nextCursor": null
}
```

## 관심종목

`GET /api/v1/watchlist`

- 응답은 camelCase `{items, maxItems}`이며 `maxItems`는 항상 `20`이다.
- 활성 관심종목은 사용자당 최대 20개다. 서비스가 사용자 행을 transaction lock한 상태에서 중복 여부와 활성 개수를 확인하므로 같은 사용자의 동시 추가도 한도를 넘지 않는다.
- 이미 활성인 같은 종목 추가는 20개 한도와 무관하게 기존처럼 멱등 성공(`200`)한다. 비활성 종목 재활성화와 신규 추가는 새 활성 슬롯을 사용한다.
- 20개가 찬 상태의 21번째 신규 추가는 `409`와 아래 오류를 반환한다.

```json
{
  "error": {
    "code": "WATCHLIST_LIMIT_REACHED",
    "message": "관심종목은 최대 20개까지 등록할 수 있습니다."
  }
}
```

## AI PAPER 운용 설정

`GET|PUT /api/v1/ai/trading/state`

- `settings`의 사용자 입력은 `riskLevel(1..5)`, `operatingBudget`,
  `dailyTargetRatePct`, `maxDailyLossRatePct`, `killSwitch`, `currency(KRW|USD)`와
  nullable 양의 정수 `customMaxBuysPerDay`, `customMaxSellsPerDay`다.
- `derivedLimits`는 서버 계산값이다. `maxBuysPerDay`, `maxSellsPerDay`,
  `maxOrdersPerDay`, `maxCustomBuysPerDay`, `maxCustomSellsPerDay`,
  `maxCustomOrdersPerDay`와 종목 비중·동시보유·재진입·AI 확신도 한도를 포함한다.
- 기본 `하루 매수/매도/전체 주문`은 `1단계 1/1/2`, `2단계 2/1/3`,
  `3단계 3/2/5`, `4단계 5/3/8`, `5단계 8/4/12`다. 사용자 횟수가 `null`이면
  이 기본값을 쓰고, 값이 있으면 각 side의 `maxCustom*` 상한을 넘을 수 없다.
- `usage`는 `buysToday`, `sellsToday`, `ordersToday`, `concurrentHoldings`,
  `budgetUsed`, 당일 실현 손익을 반환한다. 목표수익은 참고값이고 최대손실은 신규 매수를
  차단한다. 위험을 줄이는 매도는 최대손실 도달만을 이유로 차단하지 않는다.

## AI 추천 시장 범위와 일일 루틴

`GET|PUT /api/v1/ai/daily-routine`

- 응답은 `date`, `inheritedFrom`, `recommendationMarketScope`, `enabledRoutines`,
  `availableRoutines`, `alerts`, `updatedAt`을 반환한다.
- `recommendationMarketScope`는 `KR_ONLY`, `US_ONLY`, `KR_US` 중 하나다. 추천 후보
  범위만 정하며 PAPER 주문 가능 여부는 설정 통화·예산·보유량·Hard Risk에서 다시 검사한다.
- `PUT`은 `enabledRoutines`와 선택 필드 `recommendationMarketScope`를 받는다.
  범위를 생략하면 기존 값을 유지하고, 모르는 값은 `422 VALIDATION_ERROR`다.
