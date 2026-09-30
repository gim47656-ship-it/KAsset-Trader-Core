# 2026-09-30 — 실시간 관문 중·소형주 완화와 3~5단계 보유 상한 해제

RECORD:
DATE: 2026-09-30
SCOPE: NH 실시간 관문, RealtimeTapeConfig, 동시 보유 상한, max_concurrent_holdings
PATHS: app/extensions/kasset/automation/realtime_tape.py, app/extensions/kasset/automation/policy.py, app/schemas/ai_recommendations.py
STATUS: partial

## 관측 (첫 정규장, 읽기 전용)

- 운영 이미지 `82e2de88`(PR #102·#103), `nh-stream` 연결 `slot=0`. 005930은 60초 창에 체결 약 800건, 리셋 0회였다.
- 09:04~09:06 KST 표본에서 010955는 리셋 27회, 003555는 17회, 090435는 11회(`book_gap`·`trade_gap`)였다. 이 종목들은 60초 warmup을 채우지 못했고, owner 7의 090435 BUY는 `warmup_incomplete`·`trade_stale`·`book_stale`·`trades_sparse`·`spread_wide`로 막혔다.
- owner 4(5단계)는 09:00:00 사이클 때 보유 6/6에 잔여 예산이 15,700원이어서 `presizing_zero_quantity:BELOW_MARKET_LOT`가 났다. 전날 22:30 청산 SELL 2건이 09:00:01·09:00:37에 체결된 뒤, 09:10 사이클에서 BUY 2건과 SELL 2건이 생성됐다.

## 판단

- NH는 바뀐 체결·호가만 보낸다. 종목별 5초·15초 공백은 끊김이 아니라 거래가 뜸하다는 뜻이다. 연결 끊김과 세션 60초 무프레임은 `runner._drop_connection`이 그 세션의 창을 이미 버리므로, 종목별 공백 리셋은 중복된 보호이면서 중·소형주를 구조적으로 배제했다. 그래서 공백 리셋을 제거했다.
- 데이터 품질 기준만 완화했다. 체결 10건은 3건으로, 호가 3초·체결 15초 신선도는 60초 관찰창으로, 스프레드 30bp는 50bp로 바꿨다. 400원 이상 종목에서 KRX 1틱은 최대 25bp이므로 50bp는 2틱이다. 체결강도 ≥100, 60초 가격 비하락, 현재가 ≥ VWAP 같은 방향 조건과 흐름 청산 네 조건은 바꾸지 않았다.
- 보유 상한은 사용자 지시("보유한도를 막지말아봐")로 3·4·5단계만 무제한으로 했다. 전날 횟수 제한을 풀 때 초보(1·2단계)만 남긴 기준과 같다. 예산, 종목 비중, kill switch, 손절은 그대로다. API `maxConcurrentHoldings`는 `null=한도 없음`이다. 앱(`V:/HANSE/KAsset-Trader`)은 이미 `Int?`로 파싱하고 게이지에 "한도 없음"을 표시한다.
- 옛 저장 설정의 단계 추정(`_nearest_risk_level`)은 해제 전 값 5·5·6을 `_LEGACY_PRESET_LIMITS`로 유지한다.

## 남은 경계

- 보유 종목과 실시간 BUY 후보의 합이 NH 구독 한도 30종목을 넘으면, 넘친 종목은 구독되지 않는다.
- 이 PR의 CI와 사용자 배포 승인이 남았다. 배포 뒤 첫 정규장에서 중·소형주 BUY가 `warmup_incomplete` 없이 방향 조건으로만 판정되는지, owner 4·7의 보유 수가 5·6을 넘을 때 POSITION이 통과하는지 읽기 전용으로 관찰한다.
