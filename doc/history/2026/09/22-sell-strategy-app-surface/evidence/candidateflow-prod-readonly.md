# CandidateFlow 운영 read-only 관측 (2026-09-22 KST)

모두 운영 DB(`kasset-trader-db-1`, DB `kasset`) 대상 **SELECT 전용**이다. 쓰기·DDL 없음.
운영 checkout `/opt/kasset-trader-core`, `.env.kasset`, 운영 볼륨은 사용하지 않았다.

## 1. 하루치 추천 24건 — 전부 BUY, 6종목

```sql
select symbol, coalesce(name,'<NULL>'), action,
       to_char(created_at at time zone 'Asia/Seoul','MM-DD HH24:MI')
from review.ai_recommendations
where source='kasset-automation'
  and created_at >= '2026-09-21 15:00+00' and created_at < '2026-09-22 15:00+00'
order by created_at, symbol;
```

```
000155|<NULL>|BUY|09-22 09:00
000157|<NULL>|BUY|09-22 09:00
005930|삼성전자|BUY|09-22 09:00
005935|<NULL>|BUY|09-22 09:00
138040|<NULL>|BUY|09-22 09:00
000155|<NULL>|BUY|09-22 10:10
000157|<NULL>|BUY|09-22 10:10
005930|삼성전자|BUY|09-22 10:10
005935|<NULL>|BUY|09-22 10:10
02826K|<NULL>|BUY|09-22 10:10
000157|<NULL>|BUY|09-22 11:20
005930|삼성전자|BUY|09-22 11:20
005935|<NULL>|BUY|09-22 11:20
005940|<NULL>|BUY|09-22 11:20
02826K|<NULL>|BUY|09-22 11:20
000157|<NULL>|BUY|09-22 12:30
005935|<NULL>|BUY|09-22 12:30
005940|<NULL>|BUY|09-22 12:30
02826K|<NULL>|BUY|09-22 12:30
000157|<NULL>|BUY|09-22 13:40
005935|<NULL>|BUY|09-22 13:40
005940|<NULL>|BUY|09-22 13:40
02826K|<NULL>|BUY|09-22 13:40
005940|<NULL>|BUY|09-22 14:50
```

이름이 채워진 것은 `005930` 하나뿐이다. 배치는 09:00, 10:10, 11:20, 12:30, 13:40, 14:50 — 정확히 70분 간격.

## 2. 후보 출처 — snapshot 경로는 0건, 전량 live 스크리너

```sql
select candidate_count, ranked_count, candidate_sources::text
from review.kasset_automation_cycle_events
where observed_at >= '2026-09-21 15:00+00' and observed_at < '2026-09-22 15:00+00'
  and recommendation_count > 0
order by observed_at limit 3;
```

```
100|97|{"watchlist": 4, "tvscreener_kr": 100}
100|93|{"watchlist": 4, "paper_holding": 4, "tvscreener_kr": 100}
100|96|{"watchlist": 4, "paper_holding": 4, "tvscreener_kr": 100}
```

`invest_screener_snapshots`는 한 건도 없다. 즉 종목 마스터로 이름을 채우던 snapshot 경로는 이날 한 번도 실행되지 않았고,
이름을 채우지 않는 `tvscreener_kr` 경로가 후보 100건 전부를 만들었다. `005930`만 이름이 있는 이유는 watchlist 4건에 들어 있어서다.

## 3. 추천별 집행 결과 — 유효기간 60분 vs 배치 간격 70분

```sql
select symbol,
       to_char(created_at  at time zone 'Asia/Seoul','HH24:MI'),
       to_char(valid_until at time zone 'Asia/Seoul','HH24:MI'),
       coalesce(paper_execution_status,'PENDING'), decision
from review.ai_recommendations
where source='kasset-automation'
  and created_at >= '2026-09-21 15:00+00' and created_at < '2026-09-22 15:00+00'
order by symbol, created_at;
```

```
000155|09:00|10:00|SUCCEEDED|APPROVED
000155|10:10|11:10|SUCCEEDED|APPROVED
000157|09:00|10:00|SUCCEEDED|APPROVED
000157|10:10|11:10|PENDING|PENDING
000157|11:20|12:20|PENDING|PENDING
000157|12:30|13:30|SUCCEEDED|APPROVED
000157|13:40|14:40|PENDING|PENDING
005930|09:00|10:00|SUCCEEDED|APPROVED
005930|10:10|11:10|PENDING|PENDING
005930|11:20|12:20|SUCCEEDED|APPROVED
005935|09:00|10:00|FAILED|APPROVED
005935|10:10|11:10|FAILED|APPROVED
005935|11:20|12:20|FAILED|APPROVED
005935|12:30|13:30|FAILED|APPROVED
005935|13:40|14:40|SUCCEEDED|APPROVED
005940|11:20|12:20|FAILED|APPROVED
005940|12:30|13:30|PENDING|PENDING
005940|13:40|14:40|PENDING|PENDING
005940|14:50|15:50|SUCCEEDED|APPROVED
02826K|10:10|11:10|PENDING|APPROVED
02826K|11:20|12:20|PENDING|APPROVED
02826K|12:30|13:30|PENDING|APPROVED
02826K|13:40|14:40|PENDING|APPROVED
138040|09:00|10:00|SUCCEEDED|APPROVED
```

집계: SUCCEEDED 9, FAILED 5(전부 `risk_preview_rejected:BUDGET`), 미집행 PENDING 10.
`valid_until = created_at + 60분`이고 다음 배치는 +70분이므로 **모든 추천은 다음 배치가 시작되기 10분 전에 이미 만료**된다.

## 4. 이미 hard risk가 막기로 판정한 추천이 그대로 저장돼 있었다

```sql
select symbol, to_char(created_at at time zone 'Asia/Seoul','HH24:MI'),
       (select string_agg(x->>'rule',',')
          from jsonb_array_elements(
                 jsonb_path_query_array(evidence::jsonb,'$.**.checks[*] ? (@.passed == false)')) x),
       coalesce(paper_execution_status,'PENDING')
from review.ai_recommendations
where source='kasset-automation'
  and created_at >= '2026-09-21 15:00+00' and created_at < '2026-09-22 15:00+00'
order by created_at, symbol;
```

24건 중 hard risk가 실패로 판정한 것은 한 건이다.

```
000157|13:40|ORDER_COUNT|PENDING
```

그 행의 상세:

```
{"rule": "ORDER_COUNT",
 "detail": "ordersToday=7/20; buysToday=7/10; sellsToday=0/10; hardMaxBuys=24; hardMaxSells=30; hardMaxOrders=54; sameSymbolBuys=2/2",
 "passed": false}
```

`sameSymbolBuys=2/2` — 주문 단계가 절대 통과시키지 않을 것을 알면서 추천 행을 만들었다.

## 5. 실제 체결 9건

```sql
select owner_user_id, symbol, side, status,
       to_char(created_at at time zone 'Asia/Seoul','MM-DD HH24:MI')
from public.kasset_android_paper_orders
where created_at >= '2026-09-21 15:00+00' and created_at < '2026-09-22 15:00+00'
order by created_at;
```

```
4|000155|BUY|FILLED|09-22 09:10
4|000157|BUY|FILLED|09-22 09:15
4|138040|BUY|FILLED|09-22 09:20
4|005930|BUY|FILLED|09-22 09:25
4|000155|BUY|FILLED|09-22 10:15
4|005930|BUY|FILLED|09-22 11:25
4|000157|BUY|FILLED|09-22 12:35
4|005935|BUY|FILLED|09-22 13:45
4|005940|BUY|FILLED|09-22 14:55
```

## 6. owner 4의 설정 — 재진입 한도는 2

```sql
select key, jsonb_pretty(value::jsonb) from user_settings
where user_id=4 and key like '%trading%';
```

```json
{
  "mode": "AUTO_PAPER",
  "settings": {
    "currency": "KRW",
    "risk_level": 5,
    "kill_switch": false,
    "operating_budget_krw": "20000000",
    "operating_budget_usd": "10000",
    "daily_target_rate_pct": "5",
    "custom_max_buys_per_day": 10,
    "max_daily_loss_rate_pct": "5",
    "custom_max_sells_per_day": 10
  }
}
```

`risk_level: 5` → `_RISK_PRESETS[5].same_symbol_reentry_limit == 2`, `max_concurrent_holdings == 6`.
`000155`·`000157`·`005930`이 각각 하루 2회 체결된 것과 정확히 일치한다.

## 7. 종목 마스터에는 이름이 다 있다

```sql
select market, count(*) from review.ai_recommendations
where source='kasset-automation' group by market;
```

```
US|4
KRX|39
```

추천 `market` 값은 `KRX`/`US` 두 가지뿐이다(재진입 카운트 쿼리의 `market` 필터 근거).
`symbol_master`의 KRX 이름 누락 0건은 Main이 별도로 확인했다.
