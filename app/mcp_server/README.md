# auto_trader MCP server

MCP tools (market data, portfolio, order execution) exposed via `fastmcp`.

## Current operational boundary

- KR/US live account, order, portfolio, quote, and candle paths use Toss.
  `account_mode` defaults to `toss_live`; ambiguous `real`/`live` values and
  `kis_live`/`kis_mock` operational dispatch fail closed.
- KIS registrars, tasks, WebSockets, provider transports, and provider-backed
  tools are removed. Only the KIS ledger models and their historical
  `account_mode` values remain so historical rows can be read without
  reinterpretation.
- Toss fill confirmation is polling-based. Production live equity operation
  requires `toss_live.poll_fills_periodic` at least every two minutes
  (`TOSS_FILL_POLL_ENABLED=true`, `TOSS_FILL_POLL_CRON=*/2 * * * *`) or an
  immediate targeted non-dry reconcile after every accepted order.

## Observability (Sentry MCP)
- MCP tracing uses `sentry_sdk.integrations.mcp.MCPIntegration` when enabled.
- Recommended trace filter:
  - `service:auto-trader-mcp op:mcp.server`
- yfinance outbound HTTP is custom-instrumented via `SentryTracingCurlSession` and injected with `session=` at yfinance entrypoints.
- yfinance child span format:
  - `op:http.client`
  - span name/description: `METHOD /path` (query string excluded)
- Example trace filters for yfinance spans:
  - `service:auto-trader-mcp op:http.client transaction:"tools/call screen_stocks"`
  - `service:auto-trader-api op:http.client span.description:"GET /v1/finance/screener"`
- `profile` flamegraph and `trace` spans are different datasets, so some frames may appear only in profiling.
- It is normal to see only high-level spans when a tool does not execute DB/HTTP operations.
- MCP tool call arguments are attached as structured Sentry context (`mcp_tool_call`) via `McpToolCallSentryMiddleware`:
  - Context fields: `tool_name` (string), `arguments` (dict, sanitized and truncated)
  - Tag: `mcp.tool.name` for issue-level filtering
  - Sensitive values (`token`, `secret`, `password`, `authorization`, `cookie`) are masked to `[Filtered]`
  - Large arguments are truncated (strings: 1024 chars, lists/dicts: 25 items) with a visible `[truncated]` marker
  - The middleware never calls `capture_exception` directly; exception capture is handled by Sentry's `MCPIntegration`
- Semantic/operator observations are attached to the active `mcp.server` span and the `mcp_tool_observability` context:
  - `mcp.semantic_success=false` and span status `failed_precondition` are used for controlled failure envelopes (`success=false`), invalid supplied CP0 fields, and supplied CP0 observations whose `data_state` is not `fresh`. Tools outside the CP0 scope keep the existing success/error fallback when all CP0 fields are absent. Raised exceptions retain an exception-specific non-success span status.
  - `mcp.error_code` prefers the stable envelope `error_code`, a machine-readable `error`, or a provider-provenance error code; free-form exception/error text is never promoted to a tag.
  - Caller and lineage tags are `mcp.consumer`, `mcp.operator_session`, `mcp.analysis_run_id`, `mcp.correlation_id`, `mcp.lane`, `mcp.verdict`, `mcp.report_uuid`, `mcp.artifact_uuid`, and `mcp.proposal_uuid` when present. Current UUID-valued `artifact_id` and `proposal_id` surfaces are consumed as their observability `*_uuid` compatibility values.
  - Operator session resolution prefers an explicit `operator_session`, then the transport MCP session id. `session_label` remains ROB-1048 artifact-renewal metadata and is not reused as operator identity. `mcp.operator_session.source` records which source won.
  - ROB-1232 lane tags (always present, never null): `mcp.profile` from process env `MCP_PROFILE` and `mcp.session_label` from the tool argument when supplied. Missing sources are recorded as the literal `untagged` so Sentry span queries can partition by profile/session after deploy. Tag resolution failures must not drop the tools/call span. **Measurement window starts at deploy time** (tags only appear on post-deploy traffic).
- Freshness/provenance fields are read-only observations of the ROB-1048 CP0 contract. ROB-1048 owns `data_state`, `derived_as_of`, `fetched_at`, `data_age_seconds`, `cache_hit`, `fallback_source`, and `provider_provenance`; this middleware does not define or mutate them.
  - Complete, type-valid fields are copied to matching `mcp.*` span data. Query tags include `mcp.data_state`, `mcp.cache_hit`, `mcp.fallback_source`, `mcp.freshness.contract`, and the bounded `mcp.data_age.bucket`.
  - An absent/invalid `data_state` is observed as `mcp.data_state=unknown`, never `fresh`; absent `cache_hit` is `unknown`, never `false`. The `unknown` value is an observability sentinel, not an extension of the ROB-1048 envelope enum. A wholly absent CP0 contract does not itself change semantic success for unrelated tools, while supplied invalid or non-fresh CP0 fields cannot produce semantic success.
- Raw symbols are never Sentry span tags. Sentry SDK request/result span attributes have symbol-bearing values replaced with a fixed marker; queryable attributes are limited to `mcp.symbol.mode`, exact `mcp.symbol.count`, and bounded `mcp.symbol.count_bucket`. The sanitized, size-bounded `mcp_tool_call` debug context retains the original argument value.
- Funnel spans use the bounded `mcp.funnel.stage` tag:
  - `bootstrap`: `get_operating_briefing`
  - `lane`: `route_request`, `get_trading_policy`
  - `evidence`: the registered quote/news/screen/analyze/bundle evidence tools
  - `verdict`: investment-report create/decide/status tools
  - `artifact`: analysis/stage artifact persistence
  - `proposal`: `order_proposal_create`
  - `fill`: reconcile/fill-evidence tools
  - `retrospective`: `save_trade_retrospective`
  - Other calls use `other`; join stages with caller/session/run/correlation and report/artifact/proposal identifiers rather than raw symbols.

## Tools

### News Tools (Pre-Market Briefing Pipeline)

- `get_news(symbol, market=None, limit=10)`
  - Fetch symbol-level recent news for decision diagnostics (`kr`: Naver Finance, `us`/`crypto`: Finnhub)
  - KR/US/crypto: fetched articles are persisted (`news_articles` + `symbol_news_relevance`) and the response is served from DB state. Each item carries a `relevance` block (`status`: `pending`/`confirmed`, judged fields, non-authoritative `hints`). `excluded` articles (judged unrelated/low by the external judgment job) are omitted; `excluded_count` reports how many. No deterministic blacklist — auto_trader never excludes on its own.
  - When `NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED=true`, visible `pending` rows in the canonical DB response enqueue `news_relevance.judge_pending`, including rows created during an earlier worker/webhook outage. Duplicate enqueue is acceptable because the worker re-queries pending rows.
  - `degraded: true` + `fetch_error` appear when provider fetch failed and the response was served from DB cache only.
  - ROB-1048 freshness/provenance fields are always present:
    `data_state` (`fresh|stale|degraded|missing`), `derived_as_of`,
    `fetched_at`, `data_age_seconds`, `cache_hit`, `fallback_source`, and
    `provider_provenance`. Nullable timestamps/age/fallback remain explicit
    `null`; error responses use `data_state="missing"` and never synthesize a
    timestamp.
  - `fetched_at` is the original upstream acquisition time. DB-only fallback
    rows preserve `news_articles.scraped_at`; a failed retry is never stamped
    as newly fetched. Aggregate timestamp/age uses the oldest returned evidence
    and the existing 180-minute news-readiness budget.
  - DB fallback sets `cache_hit=true`,
    `fallback_source="news_articles"`, and provenance keeps the failed primary
    provider while using `served_by="news_articles"` / `mode="fallback"`.
    Expired fallback reports `stale` rather than `degraded`.
  - `pending` means "not yet judged" — treat as unverified recall, not confirmed evidence.
  - Returns: `symbol`, `market`, `source`, `count`, `excluded_count`, `news`

- `get_market_news(market=None, hours=24, feed_source=None, source=None, keyword=None, limit=20, briefing_filter=False)` [LEGACY — briefing only, not decision evidence]
  - Fetch recent market news for agent pre-market briefing
  - `market`: Optional market scope (`kr`, `us`, `crypto`) for market-separated briefing inputs
  - `feed_source`: Collection path key (e.g., `browser_naver_mainnews`, `browser_naver_research`, `rss_cointelegraph`, `rss_cnbc_earnings`, `rss_cnbc_finance`)
  - `source`: Publisher label (e.g., `연합뉴스`, `매일경제`, `CNBC`, `Cointelegraph`)
  - `briefing_filter`: Format market-specific briefing sections for `kr`/`us`, and rank crypto-relevant articles while separating broad-tech/AI noise into `excluded_news`; raw storage is not affected
  - US briefing sections include `macro_fed`, `finance_credit_rates`, `big_tech`, `earnings`, `market_sentiment`, and `watchlist_analyst`; `rss_cnbc_earnings` is source-hinted into `earnings`, `rss_cnbc_finance` into `finance_credit_rates`, while `http_finviz_news` and `rss_investing_stock_market_news` remain experimental and are only boosted when already market-relevant
  - Returns: `surface`, `advisory`, `count`, `total`, `news` (list), `sources` (unique publishers), `feed_sources` (unique collection paths), `briefing_filter`, `briefing_summary`, `briefing_sections`, `excluded_news`
  - Each article includes `stock_symbol` and `stock_name` for holdings impact analysis; formatted articles include `briefing_relevance`; crypto articles also include `crypto_relevance` metadata


### Market Data Tools

- `search_symbol(query, limit=20)`
- `get_quote(symbol, market=None)`
  - KR/US equity live quotes use Toss. `analyze_stock_batch(..., quick=True)` is
    DB-only and never fetches a live price; always call `get_quote` for that.
  - NXT/unified venue data that depended on KIS is not synthesized; unsupported
    venue requests return `provider_unsupported`.
- `get_fx_rate(pair="USDKRW")`
  - Read-only spot FX quote for exchange-timing and US-market cash conversion decisions.
  - P1 supports USD/KRW only. Accepted spellings: `USDKRW`, `USD/KRW`, `USD_KRW`, `USD-KRW`.
  - Source is `app.services.exchange_rate_service.get_usd_krw_rate_details()`, which uses Toss when enabled and open.er-api as fallback.
  - Response fields: `pair`, `base_currency`, `quote_currency`, `rate`, `mid_rate`, `default_rate`, `source`, `valid_from`, `valid_until`, `basis_point`, `rate_change_type`.
  - `default_rate` mirrors the scalar exchange-rate behavior used by existing portfolio and cash consumers.
  - Unsupported pairs raise a tool argument error. FX pairs are not market indices; `get_market_index("USDKRW")` remains unsupported.
  - Trends, bank-specific quotes, preferential effective rates, exchange execution, and US-order total-cost routing are outside ROB-567 P1.
- `get_orderbook(symbol, market="crypto")`
  - Orderbook support is limited to the KRW crypto market. KR/US equity
    orderbooks have no provider and are rejected.
- KR quote responses expose `price_as_of`, `price_freshness`
  (`fresh|stale|unavailable`), `price_usable`, and a stable
  `price_unavailable_reason` when unusable. Missing timestamps and epoch-zero
  values are unavailable; prior-date timestamps are stale. NXT tradability
  similarly returns `nxt_tradable=null` plus the observed value and reason when
  its as-of is missing or stale.
- US equity quotes use Toss. Provider failures are propagated as tool-level
  errors rather than silently falling back to KIS.
- `get_holdings(account=None, market=None, include_current_price=True, minimum_value=None, account_mode=None)`
  - Crypto positions may include optional `strategy_signal` field when Phase 2 exit logic triggers (4.5% stop-loss or RSI > 46 mean-reversion on profitable positions)
- `get_position(symbol, market=None, account_mode=None)`
- `get_financials(symbol, statement="income", freq="annual", market=None)`
  - Provider payloads with no numeric financial values return
    `status="unavailable"`, `scoreable=false`, the stable reason
    `financial_metrics_unavailable`, and source/statement/frequency/period-count
    evidence. The tool preserves the empty provider payload and does not invent
    metrics.
- `get_ohlcv(symbol, count=100, period="day", end_date=None, market=None, include_indicators=False)`
  - period: `day`, `week`, `month`, `1m`, `5m`, `15m`, `30m`, `4h`, `1h`
  - `include_indicators=True` adds `indicators_included` at the payload top level and appends `rsi_14`, `ema_20`, `bb_upper`, `bb_mid`, `bb_lower`, `vwap` to each row
  - `vwap` is populated for intraday periods and `null` for `day/week/month`
  - `1m` / `5m` / `15m` / `30m`: KR/US equity + crypto
  - `4h`: crypto only
  - `1h`: KR/US equity + crypto
  - Crypto `1m` / `5m` / `15m` / `30m` rows expose `timestamp`, `date`, `time`, `open`, `high`, `low`, `close`, `volume`, `value`, `trade_amount` and do not expose raw `datetime`
- US OHLCV behavior:
  - US intraday (`1m`/`5m`/`15m`/`30m`/`1h`) uses Toss and returns ET-naive
    timestamps at the shared Candle boundary. An active symbol with a normal
    empty provider response returns `[]`; provider failures raise.
  - Daily/weekly/monthly reads use the registered non-KIS data providers.
- KR OHLCV behavior:
  - KR daily and intraday provider reads use Toss. Wider intraday intervals are
    aggregated from Toss `1m`; persisted DB rows remain the read-through source
    where the service contract allows it.
  - Missing or inactive universe symbols fail closed before provider access.
    No KIS minute overlay or fallback runs.
  - KR intraday response rows include `session` and `venues` fields.
- `get_indicators(symbol, indicators, market=None)`
- `get_market_index(symbol=None, period="day", count=20)`
  - KR indices (`KOSPI`/`KOSDAQ`) are tagged with `data_state` from the KRX
    session clock. If the clock is live but the Naver payload is self-inconsistent
    (`change == 0`, `change_pct == 0`, and `open != current`), the original
    numeric fields are preserved and `data_state` is downgraded to `"stale"`
    with `data_state_reason: "kr_index_fresh_clock_payload_lagging"` and `as_of`.
- `get_investment_opinions(symbol, limit=10, market=None)`
- `get_analyst_consensus(symbol)`
  - Get analyst consensus (recommendation mean and price target mean) for a Korean stock from Naver mobile integration API. Distinct from `get_investment_opinions` (report-level). Korean stocks only.
- `get_short_interest(symbol, days=20)`
  - KIS-only short-interest data is unavailable. The tool returns
    `provider_unsupported` and does not synthesize a replacement.
- `get_intraday_investor_flow` and `get_execution_strength` are not registered because their provider-only evidence has no Toss equivalent
- `get_toss_buy_balance(symbol)`
  - Toss orderbook balance rate (buyBalanceRate/sellBalanceRate) and foreigner holding ratio — NOT user buy ratio. Live per-call, operator-gated. Disabled by default (returns `status='disabled'` unless `TOSS_CONSUMER_SIGNALS_ENABLED=true` is set). Korean stocks only.
- `get_toss_ai_signal(symbol)`
  - Toss AI signal (direction + reasoning). Live per-call, operator-gated. Disabled by default (returns `status='disabled'` unless `TOSS_CONSUMER_SIGNALS_ENABLED=true` is set). Korean stocks only.
- `get_volume_profile(symbol, market=None, period=60, bins=20)`
- `get_order_history(symbol=None, status="all", order_id=None, limit=50, account_mode=None)`
  - `status="pending"` 만 symbol 없이 호출 가능
  - `status in {"all", "filled", "cancelled"}` 는 symbol 필요
  - filled/cancelled 조회는 시장별 historical endpoint 제약 때문에 symbol fan-out을 자동 수행하지 않음
- `save_trade_journal(symbol, thesis, ..., paperclip_issue_id=None)` - Save the thesis, strategy, account context, and optional external issue key (legacy Paperclip name; current Linear ROB key) for a trade.
- `get_trade_journal(symbol=None, status=None, ..., paperclip_issue_id=None)` - Query active journal entries by symbol/account or reverse-lookup an external issue key (legacy Paperclip name; current Linear ROB key).
- `update_trade_journal(journal_id=None, symbol=None, ...)` - Activate, close, stop, or adjust the latest matching journal entry.
- `get_latest_market_brief(symbols=None, market=None, limit=10)` - Return concise latest AI analysis context for recent or selected symbols.
- `get_market_reports(symbol, days=7, limit=10)` - Return detailed AI analysis report history and decision trend for one symbol.
- `place_order(symbol, side, order_type="limit", quantity=None, price=None, amount=None, dry_run=True, reason="", exit_reason=None, thesis=None, strategy=None, target_price=None, stop_loss=None, min_hold_days=None, notes=None, indicators_snapshot=None, defensive_trim=False, approval_issue_id=None, account_mode=None)`
  - `side="buy"` 이고 `dry_run=False` 인 경우 `thesis` 와 `strategy` 가 필수
  - 실매수 성공 시 trade journal draft를 자동 생성하고 fill 저장 후 active로 연결 시도
  - 실매도 성공 시 동일 symbol의 active journal을 FIFO 기준으로 auto-close 시도
  - 부분 매도는 quantity를 수정하지 않고, fully-consumed journal만 close한다
  - journal close 실패는 주문 성공을 되돌리지 않고 `journal_warning` 으로 응답한다
  - 직접 `defensive_trim=True` 및 `exit_intent="loss_cut"` 경로는 fail-close하며 `order_proposal_create`를 안내한다. proposal loss-cut만 Telegram 2단계 확인 후 평균단가 1% floor를 우회할 수 있다
- `modify_order(order_id, symbol, market=None, new_price=None, new_quantity=None, dry_run=True, account_mode=None)`
- `cancel_order(order_id, symbol=None, market=None, account_mode=None)`
  - US equities: resolves exchange from symbol DB, open orders, and recent history before cancel
  - When symbol is omitted, KR/US auto-lookup is best effort and may fail if the order cannot be reconstructed
  - Discord button flows: `cancel_order(order_id="...", market="...")` — symbol auto-lookup enabled
- `modify_order` Discord button flow example:
  - `modify_order(order_id="...", symbol="...", market="...", new_price=123.45, dry_run=false)`
- `screen_stocks(...)` - Screen stocks across different markets (KR/US/Crypto) with various filters. **Generic candidate-discovery entrypoint.**
  - If the response has `meta.reason="krx_session_expired"`, KRX re-authentication was attempted once and did not recover the session. Use `screen_stocks_snapshot(market="kr")` for persisted-preset discovery, or `get_momentum_candidates(market="kr")` for intraday momentum candidates.
- `discover_buy_candidates_fanout()` - KR-only, read-only bounded discovery across RSI ordering (without a `max_rsi` prefilter), pullback, turnover, snapshot support/flow, and snapshot value/catalyst source families. Each source is capped at 10 rows, snapshot groups at 5 presets, and only the top 10 deduped DB-standard symbols receive full-analysis revalidation with top-level `data_state` freshness proof. Missing freshness is recorded as undetermined observation only, never eligibility. It never creates a proposal/order or reads broker/account state, so budget is deferred. Do not use it for PnL scoring or immediate threshold tuning.
- `evaluate_buy_gate_ab_shadow(candidates, evaluation_as_of, created_by)` — ROB-1301 shadow-only A/B buy-gate evaluator. Variant A is live (strong support); variant B is moderate+ support with every other gate identical and the same `evaluation_as_of`. Observation-only: no proposal, order, watch, or DB write. B-only rows return `forecast_save` kwargs tagged `shadow_buy` / `promote=false` / `calibration_exclude`. Do not use the output to retune the live gate or change policy before the pre-registered 4-week collection completes.
- `get_krx_session_health()`
  - Read-only authenticated KRX-session probe. It uses the normal KRX login/re-authentication path and reports `status`, `reason`, `retryable`, and `authenticated`; no market, broker, or order state is changed.
- `screen_stocks_snapshot(preset=None, presets=None, market="kr", filters=None, exclude_watched=false, exclude_held=false, exclude_symbols=None, min_analyst_count=None, min_analyst_buy_count=None, min_market_cap=None, min_market_cap_eok=None, max_market_cap_eok=None, sort=None, limit=40, offset=0)`
  - **DB-only (ROB-1309): makes zero external HTTP calls** — no sector lazy-fill, no analyst-consensus fetch, no live broker holdings lookup, no live price fetch. Snapshot-backed discovery workflow. Pass either `preset="consecutive_gainers"` or `presets=["consecutive_gainers", "double_buy"]`; `preset` also accepts a comma-separated list for compatibility.
  - Returns symbols that matched the preset(s) from the persisted daily snapshots.
  - Supports multi-preset sweeps with symbol deduplication and `matchedPresets` tagging.
  - `exclude_held` is NOT supported here — it would require the live holdings call this DB-only tool never makes; passing `exclude_held=True` returns a fail-closed error pointing at `screen_stocks_enrich` (same redirect pattern as `min_analyst_count`/`min_analyst_buy_count`). `isHeld` is always `false` on every returned row.
  - `exclude_watched` (bool): accepted for compatibility, but currently unsupported in MCP because no user watchlist context is wired; requests emit an explicit warning.
  - `exclude_symbols`: explicit symbols to remove after dedupe.
  - `min_analyst_count` / `min_analyst_buy_count`: NOT applied here — this tool never returns consensus data; passing either returns the same fail-closed redirect error as `exclude_held`. Use `screen_stocks_enrich` for analyst-count filtering.
  - `min_market_cap` (float): size filter using raw numeric `marketCapValue` (`KRW` for KR, `USD` for US/crypto).
  - `min/max_market_cap_eok` (float): KR compatibility size filter — unit is 1억원.
  - `sort="matched_presets_desc"`: ranks intersections (stocks in multiple presets) first.
  - `filters` list: tune preset thresholds (threaded for `consecutive_gainers` and `crypto`).
  - `preset="support_proximity"` is KR-only and reads persisted price/support/distance plus Naver-normalized KRW market cap. It performs no query-time OHLCV fetch or support recalculation; revalidate only the returned top symbols with `get_support_resistance`/`get_quote` when acting.
  - Results are capped (default 40) and paginated. Check `pagination` in payload.
  - Preset sweeps are capped at 5 presets.
  - Minimum market-cap filters exclude rows with missing `marketCapValue` and report the excluded count in `warnings`.
  - `priceLabel`/`changePctLabel`/`metricValueLabel` are values at the snapshot time and may be stale by up to one session; revalidate a top candidate with `get_quote`/`get_support_resistance`/`analyze_stock_batch` before acting.
  - Crypto snapshot examples:
    - `screen_stocks_snapshot(preset="crypto_high_volume", market="crypto", limit=40)`
    - `screen_stocks_snapshot(preset="crypto_momentum", market="crypto", filters=[{"field":"trade_amount_24h","operator":"gte","value":10000000000}], limit=40)`
  - Use `get_crypto_top_movers` for live Upbit top movers; use `screen_stocks_snapshot(..., market="crypto")` for persisted snapshot-backed filtering.
- `screen_stocks_enrich(preset=None, presets=None, market="kr", filters=None, exclude_watched=false, exclude_held=false, exclude_symbols=None, min_analyst_count=None, min_analyst_buy_count=None, min_market_cap=None, min_market_cap_eok=None, max_market_cap_eok=None, sort=None, limit=40, offset=0)`
  - **ROB-1309 opt-in live-enrichment counterpart to `screen_stocks_snapshot`.** Runs the identical preset/filter/discovery/pagination pipeline (same params), then makes external calls: KR/US analyst consensus (buy/hold/sell counts + target prices, Redis cache-aside with a call-time-fresh target-upside recompute), sector-label lazy-fill, RSI14 from persisted snapshot closes, and one live Toss holdings call for `exclude_held`/`isHeld`.
  - `min_analyst_count`/`min_analyst_buy_count` filter on resolved consensus counts before pagination (capped at 200 merged rows before enrichment); only the returned page is fully enriched with `analysisContext`.
  - Only call this after `screen_stocks_snapshot` when analyst consensus / sector labels / analyst-count filtering / `exclude_held` are actually needed — every call fans out one HTTP round-trip per uncached symbol on the page and can take tens of seconds for a full page.
  - A symbol whose sector/consensus fetch just failed is not retried within a bounded window (`meta.enrichment_excluded`); a symbol with >=3 consecutive failures is dropped from `results` on that call only, reported under `meta.chronic_failure_candidates` (self-healing on the next success). Read-only wrt broker/order/watch state.
- `get_top_stocks(market="kr", ...)` - KIS volume-rank based KR rankings return `provider_unsupported`; crypto rankings remain available from Upbit.
- `get_crypto_top_movers(ranking_type="relative_strength", limit=20)` - Crypto-only Upbit KRW discovery wrapper. Default ranking sorts non-BTC coins by 24h outperformance vs KRW-BTC.
- `get_upbit_altseason(include_constituents=false, constituents_limit=50)` - Upbit altseason ratio and 24h breadth. With constituents enabled, `breadth.constituents` lists KRW alts beating BTC with 24h change, vs-BTC relative strength, volume, and traded value.
- ~~`recommend_stocks(...)`~~ — **DEPRECATED / registry-hidden (ROB-359).** No longer registered on the MCP tool surface. Use `screen_stocks` for candidate discovery. The implementation is retained in `analysis_tool_handlers.recommend_stocks_impl` for a possible future narrow `build_buy_plan` tool; do not call it from active report/operator prompts.

- `analyze_stock_batch(symbols, market=None, include_peers=False, quick=True, decision_history_account_mode=None)`
  - Batch analysis for up to 10 symbols. `quick=True` is the default DB-only fast projection;
    `quick=False` is the explicit full/deep analysis path.
  - `screen_stocks_snapshot` is DB-only (ROB-1309) and never returns consensus/RSI
    inline. For analyst consensus/sector labels, call `screen_stocks_enrich` with
    the same preset/filter/pagination inputs (it has no `symbols` parameter — it
    reruns the identical discovery query, then enriches the returned page); for
    RSI/support/resistance or full `quick=False` analysis on individual symbols,
    call `analyze_stock_batch`.
  - Quick returns only the allowlisted projection: symbol, market_type, source,
    current_price, latest OHLCV, rsi_14, supports (top 3), resistances (top 3),
    and the freshness envelope. It also preserves compact `decision_history`
    and `earnings` meaning through set-based DB read models. It makes zero HTTP
    requests and reads all requested data in at most 12 DB executions per batch.
    The quick path does not run news, profile, provider earnings, consensus,
    recommendation, or holdings work.
  - **`current_price` is NOT live.** It is the close of the most recent
    CLOSED daily candle (`data_state="stale"`,
    `data_state_reason="db_only_projection"`). Call `get_quote` for a live
    price or live session/NXT-tradability state.
  - `include_position` is accepted for compatibility but has no effect for
    any `quick` value — `analyze_stock_batch` never attaches a `position`
    field, quick or full. Use `get_holdings` for per-account positions.
  - **Removed from the quick contract (PR #1915, deep/`quick=False`-only now):**
    `nxt_tradable`/`nxt_tradable_source`/`nxt_tradable_asof`/`nxt_tradable_stale`
    (ROB-668), `price_source`/`session`/`session_state`/`krx_prev_close`/
    `change_pct` (ROB-725/ROB-888), `venue`/`quote_asof`/`delayed` (quote
    provenance), `price_data_state` (ROB-1048), `fresh_artifact_exists`
    (ROB-648), and `consensus`/`recommendation` (holdings). Use
    `get_quote` for the live-price/session-provenance fields.
  - Use `quick=False` when consensus, recommendation, news, profile, provider
    earnings,
    peers, or the complete full-analysis payload is explicitly required.
    The full output contract is unchanged.
  - Every full and compact result carries the ROB-1048 freshness/provenance
    envelope: `data_state` (`fresh|stale|degraded|missing`),
    `derived_as_of`, `fetched_at`, `data_age_seconds`, `cache_hit`,
    `fallback_source`, and `provider_provenance`. Aggregate timestamps use the
    oldest included provider evidence. A swallowed provider exception produces
    `degraded` (or `missing` when no usable evidence remains) with null
    timestamps; response-time `now` is never substituted.
  - The full path may use its provider fetch cache and its `refresh` behavior;
    these provider-cache semantics do not apply to quick. Compact quick rows
    use the DB-only freshness envelope described above.

### Snapshot-backed report generation

Snapshot-backed generator/Hermes MCP tools are registered only when
`SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true`. With the default `false`
setting they are physically absent from the MCP surface instead of returning
disabled no-op payloads:

- `investment_report_generate_from_bundle`
- `investment_report_prepare_bundle`
- `investment_report_get_hermes_context`
- `investment_report_create_from_hermes_composition`
- `investment_stage_artifacts_ingest_from_hermes`
- `investment_report_prepare_intraday_context`

### Frozen analysis snapshot bundles (ROB-838)

The `analysis_bundle_create` and `analysis_bundle_get` tools are gated by
`ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED`, whose default is `false`. When the gate
is false, both tools are physically absent from the default MCP surface.

`analysis_bundle_create(market, account_scope, symbols, user_id=None,
market_session=None)` captures a fixed, append-only input document. `market` is
`kr`, `us`, or `crypto`; `account_scope` is optional and, when present, is
`kis_live`, `kis_mock`, `alpaca_paper`, or `upbit_live`; `symbols` contains 1–10
symbols; and `user_id` and `market_session` are optional capture context. A
successful call returns `success`, `bundle_id`, the canonical SHA-256
`content_hash`, `captured_at`, and completeness fields: `status` (`complete` or
`partial`), `unavailable_sections`, and `partial_sections`. Capture has no order
or proposal mutation side effect.

`analysis_bundle_get(bundle_id, sections=None)` accepts the bundle UUID and an
optional list drawn from `portfolio`, `quotes_orderbooks`,
`indicators_support_resistance`, `market_gate_inputs`, `investor_flow`, and
`decision_history`. Omitting `sections` returns the complete stored document.
Supplying it only projects the named stored sections; it does not recapture,
recompute, refresh, backfill, or otherwise change any stored value. The result
includes `success`, `bundle_id`, `content_hash`, `integrity_verified`, creation,
capture, and read timestamps, bundle `age_seconds`, `status`, `completeness`,
`stale_warning`, per-section `section_freshness`, and the stored `document`.

Bundles are write-once evidence. To correct or update input, create a new bundle
and hand off its new `bundle_id`; never patch an old bundle. Every get recomputes
the canonical SHA-256 hash and compares it with the stored hash before returning
the document. A mismatch, malformed frozen document, wrong bundle purpose/kind,
or invalid item cardinality returns `error="analysis_bundle_integrity_error"`
instead of unverified evidence.

Freshness is read-time metadata only. `age_seconds`, `stale_warning`, and each
section's `as_of`, age, and freshness status describe the frozen evidence
without refreshing it. Likewise, a provider failure captured as an unavailable
section retains its original error and remains unavailable; get never calls a
provider to fill it.

### Session Context Tools

`session_context_append(entries)` persists append-only operator context for
cross-session handoff. It is for "where did we leave off?" state: plans,
decisions, deferred items, rejected candidates, constraints, open questions,
next actions, and handoff notes. It is not an investment report, research
session, trade journal, watch alert, or order ledger.

Each entry accepts:

- `kst_date` optional `YYYY-MM-DD`; defaults to current KST date.
- `market` required: `kr`, `us`, or `crypto`.
- `account_scope` optional: `kis_live`, `kis_mock`, `alpaca_paper`, `upbit_live`.
- `entry_type` required: `plan`, `decision`, `deferred`,
  `rejected_candidate`, `constraint`, `open_question`, `next_action`,
  `handoff_note`.
- `title` required short title.
- `body` required markdown body.
- `refs` optional object: `report_uuid`, `item_uuid`, `alert_uuid`, `order_id`,
  `journal_id`, `symbols`.
- `created_by` optional: `claude`, `operator`, `system`, `codex`; defaults to
  `claude`.
- `session_label` optional grouping label.

`session_context_get_recent(market?, account_scope?, kst_date_from?, entry_type?, limit)`
returns recent entries newest first. `limit` is clamped to 1..100 and defaults
to 20. New trading sessions should call this before comparing yesterday's plan
with today's candidate tournament.

### Analysis Artifact Tools

`analysis_artifact_save(market, kind, title, symbols?, payload?, as_of?, valid_until?, created_by?, session_label?, correlation_id?, account_scope?, readiness_label?)`
persists a structured analysis artifact for cross-session reuse. It is for the
durable outputs of analysis runs — screening rankings, profit-taking verdicts,
support/resistance maps, flow assessments, candidate pools, session summaries,
and briefings — so a later session can reuse them instead of recomputing. Save is
explicit only; `analyze_stock_batch` and other analysis runs do not auto-persist.
This is the cross-session artifact store; it is complementary to (not a duplicate
of) the ROB-638 fetch-layer Redis cache, which dedupes slowly-changing provider
fetches across calls within a run.

Each artifact accepts:

- `market` required: `kr`, `us`, or `crypto`.
- `kind` required: `screening_ranking`, `profit_taking_verdicts`,
  `support_resistance_map`, `flow_assessment`, `candidate_pool`,
  `session_summary`, `briefing`.
- `title` required short title.
- `symbols` optional string list; defaults to empty on create and is normalized
  by sorting and deduplication.
- `payload` optional JSON object; defaults to empty on create.
- `as_of` optional ISO datetime; defaults to now (UTC) for a new row. On a
  `correlation_id` retry, omission reuses the stored `as_of`, so an exact retry
  cannot renew freshness to request-time now. Supply an explicit new `as_of`
  to renew the artifact.
- `valid_until` optional ISO datetime; when in the past the artifact is stale
  and excluded from `analysis_artifact_list` unless `include_stale=true`. **When
  omitted on create or an evidence-time renewal, the server assigns a per-kind
  default TTL** (price/screen kinds expire at the end of the `as_of` KST day;
  `session_summary`/`briefing` at the end of the next KST day), so every new
  artifact has a concrete expiry. An exact/partial retry preserves the stored
  expiry. Explicit `valid_until=null` means unknown expiry and is stale, not
  permanently fresh. An omitted legacy null is healed on the next save.
- `created_by` optional: `claude`, `operator`, `system`; defaults to `claude`.
- `session_label` optional grouping label.
- `correlation_id` optional idempotency key. Re-saving the same `correlation_id`
  updates the row in place (omit to append a new row).
- `account_scope` optional grouping/filter label.
- `readiness_label` optional advisory (caller-declared, not a gate):
  `screen_grade`, `not_decision_ready`, `ready_for_order_review`, `blocked`.

On a `correlation_id` retry, omitted optional fields preserve their stored
values. This includes `symbols`, `payload`, `valid_until`, `created_by`,
`session_label`, `account_scope`, and `readiness_label`; omission is not a
metadata change. Explicit null clears nullable fields and resets `symbols` /
`payload` to their empty representations. (`as_of` is required evidence time:
omission preserves it on retry, while explicit null is invalid.)

The response includes `action` and the saved artifact. `action` is `created`
(new row), `updated` (correlation_id re-save whose payload or persisted metadata
changed — `version` is bumped in place), or `unchanged` (an exact retry whose
canonical payload and normalized persisted metadata are identical — no write,
`version` preserved). Renewal-sensitive metadata includes `as_of`, resolved
`valid_until`, and `readiness_label`; changing any of them updates the row even
when `content_hash` remains the same. Each artifact carries a server-computed
`content_hash` (over the canonical payload JSON) and an integer `version`.

`analysis_artifact_list(market?, kind?, symbol?, since?, include_stale?, limit, correlation_id?, account_scope?)`
returns matching artifacts newest `as_of` first. `symbol` does a containment
match on the `symbols` array. `limit` is clamped to 1..100 and defaults to 20.
Stale rows (`valid_until` in the past or null) are excluded unless
`include_stale=true`.
`correlation_id` and `account_scope` are optional exact-match filters (the same
labels set on `analysis_artifact_save`).

`analysis_artifact_get(artifact_id)` returns a single artifact including the
full payload, by numeric `id` or `artifact_uuid` string. Missing ids return
`success=false` with `error="not_found"`.

### Investment Report Tools

- `investment_report_add_items(report_uuid, items, actor=None)` - Append new proposal items to an existing draft investment report. The item payload contract matches `investment_report_create`. Duplicate `client_item_key` rows are returned as existing items and are not rewritten. Non-draft reports return `error="not_draft"`. No broker, order, or watch mutation is performed.
- `investment_report_update(report_uuid, title=None, summary=None, risk_summary=None, thesis_text=None, no_action_note=None, market_snapshot=None, portfolio_snapshot=None, metadata=None, valid_until=None, actor=None, reason=None)` - Update draft report header fields without changing report identity, lifecycle status, predecessor chain, account scope, generator version, or items. Each successful update appends an audit entry to `report.metadata.draft_updates`. Non-draft reports return `error="not_draft"`.

### Execution Ledger Fill Event Tools (ROB-755)

ROB-755 exposes the same fill-event triage surface that powers the operator-host
alert poller (`scripts/list_recent_fill_events.py`) over MCP. Read-only; no broker
mutation; no order mutation; no watch mutation. Registered in the "Always" block
of `register_all_tools`, so every profile registers it except
`analysis_readonly`, which uses its own curated allowlist.

- `execution_ledger_fill_events_list_recent(after_id=None, market=None, side=None, source="websocket", broker=None, account_mode=None, limit=50)`
  - Read-only. Returns recent fills from `execution_ledger` rows newer than
    `after_id`, optionally filtered by `market` (`kr`|`us`|`crypto`),
    `side` (`buy`|`sell`), `source` (`websocket`|`reconciler`|`manual_import`),
    `broker`, and `account_mode` (`live`|`mock`).
  - `source` defaults to `websocket` so triagers don't accidentally ingest
    reconciler/manual_import backfills; pass `source=None` to read every source.
  - `limit` is clamped to `1..500` by the repo.
  - Response: `{"success": True, "count": int, "fills": [sanitized_fill, ...]}`
    on success; `{"success": False, "error": str}` on failure.
  - **No broker mutation.** No order mutation. No DB write.

**Sanitized fill shape (21 keys, identical to the CLI output):**
`ledger_id`, `event_key`, `broker`, `account_mode`, `venue`, `instrument_type`,
`market`, `symbol`, `raw_symbol`, `side`, `filled_qty`, `filled_price`,
`filled_notional`, `currency`, `broker_order_id`, `fill_seq`, `correlation_id`,
`source`, `filled_at`, `trade_day_kst`, `created_at`. **`raw_payload_json` is never emitted**
(security constraint — same as the CLI).

**Validation errors:**
- `source="invalid"` (or any string outside `websocket`/`reconciler`/`manual_import`/`None`)
  → `{"success": False, "error": "invalid_source"}` immediately, no DB call.

### Order proposal approval tools (ROB-816)

When `ORDER_PROPOSALS_ENABLED=true`, the default and
`tradingcodex_execution` profiles expose the proposal-ledger tools below. A
proposal describes a possible order; creating or voiding one is not a broker
order mutation.

- `order_proposal_create(...)`
  - `market` uses canonical `equity_kr`, `equity_us`, or `crypto`; the tool
    accepts `kr` and `us` aliases and normalizes them before validation,
    payload hashing, and persistence.
  - Supported place combinations are `toss_live` with
    `equity_kr`/`equity_us`. Crypto proposal execution is not operational.
  - `action="place"` is the default and `target_broker_order_id=None` is the
    default.
  - `place`: `target_broker_order_id` must be absent; one or more proposal
    rungs are allowed.
  - `replace`: `target_broker_order_id` is required; exactly one rung is the
    proposed new order. Creation reads and snapshots the open target order but
    does not mutate the broker. Telegram approval cancels the target, confirms
    cancellation from fresh broker evidence, and only then submits the new
    order.
  - `cancel`: `target_broker_order_id` is required; exactly one rung must be
    an exact snapshot of the target order (side, remaining quantity, and limit
    price). It performs no new-order submit.
  - Target actions are supported only for `toss_live/equity_kr` and
    `toss_live/equity_us`.
  - When `valid_until` is omitted, it defaults to the next `00:00 KST`.
  - Before an approval card/nonce is published, dispatch enforces
    timezone-aware `valid_until` and the proposal's broker/market submission
    session. Missing, naive, malformed, or expired validity fails closed.
    US DAY proposals are regular-session only and use the XNYS or Toss broker
    calendar; KR keeps KRX regular plus positively confirmed, fresh NXT
    eligibility. Unknown calendar or capability evidence also fails closed. A
    proposal with a non-null
    `exit_intent` is a protective exit and bypasses only session/calendar
    resolution and approval-window policy-stamp binding, so calendar
    uncertainty cannot preempt its confirmation flow. Its `valid_until`
    remains mandatory and fail-closed; `exit_reason` alone grants no
    exemption.
  - A dispatch block does not fail proposal persistence. The create response
    adds `approval_dispatch={status:"blocked", code, ...}` with typed
    `EXPIRED`, `INVALID_VALID_UNTIL`, `DEFER_SESSION_CLOSED`,
    `CALENDAR_UNKNOWN`, or `NO_EXECUTABLE_WINDOW` evidence. No Telegram card
    or approval nonce is published for that dispatch attempt. The blocked
    attempt is nevertheless finalized durably with
    `approval_dispatch_state="failed"` and a `CODE/detail` failure code.
    Every create-time non-sent dispatch also sends a Discord-first operational
    alert through `discord_webhook_alerts`; callback-generated reconfirm and
    loss-cut confirmation dispatches use the same alert boundary. The alert
    includes proposal ID, symbol, side, failure code, and the safe next action.
    Alert delivery failure is logged at error level and retained in
    `source_asof.approval_dispatch_alert` instead of being reported as success.
  - When `supersedes_proposal_id` is present, the old group's
    `pending_approval`/`needs_reconfirm` rungs become `superseded`, its
    approval nonce is consumed, and its recorded Telegram message is edited
    best-effort to remove the old buttons. Submitted/resting/terminal rungs
    are left unchanged because broker cancellation/replacement owns them.
  - It accepts nullable `exit_intent`, `exit_reason`, `retrospective_id`, and
    `approval_issue_id` fields for ordinary proposals. For
    `exit_intent="loss_cut"`, `exit_reason`, `retrospective_id`, and a
    non-empty `approval_issue_id` are all required.
  - The referenced retrospective is checked at create time for existence,
    matching symbol, an eligible loss-cut trigger type (`stop_loss` or
    `thesis_change`), and freshness within 72 hours.
    This create-time check does not replace approval-time checks.
  - No external issue tracker is queried. A loss-cut first approval click
    submits nothing: it edits the message with order/current-price/loss/slip
    and retrospective evidence and issues `⚠️ 손절 확인`. That second button
    has a 90-second single-use nonce bound to proposal/rung/revision. Only the
    second click reruns full preview, <=72h retrospective, slip-band, price
    diff, and approval-hash checks and may submit.
  - ROB-871 adds a separate default-off
    `ORDER_PROPOSALS_AUTO_APPROVE` gate. When enabled, only `limit` + `place`
    proposals with no `exit_intent` may auto-submit. The fresh preview price,
    configured minimum resting distance, per-order cap, and account/KST-day
    cumulative cap are checked immediately before the existing submit path.
    Market orders, loss cuts, replace/cancel actions, guard failures, 861
    buying-power reconfirmations, and policy misses fall back to the normal
    Telegram approval message without being discarded. Account/market pairs
    without the existing cancel adapter and multi-rung ladders also remain
    human-gated so veto and all-or-human fallback are enforceable.
  - The response always includes `approval_dispatch`. Proposal persistence can
    still succeed while Telegram fails, but those outcomes are distinct:
    `approval_dispatch.ok`, `state`, `message_id`, HTTP `status_code`, Telegram
    numeric `error_code`, allowlisted `error_classification`, `failure_code`,
    and conservative `payload_chars` are returned to the caller. Telegram's
    remote `description` is discarded at the HTTP boundary and is never
    returned, logged, or persisted; a present description that is not an exact
    allowlisted constant is reduced to `unknown_telegram_error`.
  - Every dispatch attempt is committed as `pending` before Telegram I/O and
    finalized into the closed
    `sent_current`/`sent_superseded`/`failed`/`partial_failed` state set only
    after the current-owner fence. Caller `ok=true` is derived only from
    `sent_current`; a physically sent stale attempt returns
    `state="superseded"` and `failure_code="approval_dispatch_superseded"`.
    Proposal summary fields
    (`approval_dispatch_state`, `approval_dispatch_attempted_at`,
    `approval_dispatch_failure_code`, `approval_dispatch_payload_chars`) and
    the per-attempt ledger row remain durable. A failed attempt invalidates
    its nonce; a retry mints a new nonce.
  - Telegram `sendMessage` payloads are checked against a conservative 4,096
    UTF-16-unit ceiling. When the full rendered card is too long, the complete
    thesis/strategy is split losslessly into plain-text context messages. The
    short inline-button card is sent only after every context message succeeds;
    a context failure publishes no approval button.
  - Callback data binds the current attempt ID, card kind, membership revision,
    membership digest, and nonce. Manual approval/deny, batch approval,
    auto-veto, and loss-cut confirmation all cross the same fail-closed gate;
    only `sent_current` may consume a nonce or reach broker submit/cancel.
  - Batch cards are immutable published snapshots. The second eligible member
    freezes a staged batch before publication; later proposals create a new
    batch instead of editing the published card. Batch callbacks recompute and
    verify the exact ordered membership digest, so a row not shown on the card
    cannot be approved.
- `support_reserve_net_consume(request)`
  - Explicitly invokes the deterministic reserve-net consumer with a complete
    caller-supplied evidence packet. It performs no broker/account reads and
    never infers, trims, or aliases `broker_account_id`.
  - All selected proposal rows use the watcher-scope seam and commit in one DB
    transaction. Existing proposal dispatch/classification runs only after the
    commit; no new approval or submit rule is introduced.
  - It is a conditional buy helper, not a scheduler or a generic route step.
    Missing/stale evidence, a freeze, an unavailable seam, or an active legacy
    or concrete scope creates zero proposals.
- `order_proposal_redispatch(proposal_id, dry_run=true)`
  - This is a single-proposal manual lever; there is no automatic redispatch
    sweep. `dry_run=true` is read-only and must be reviewed before execution.
  - Only active `limit` + `place` proposals whose prior dispatch is failed (or
    absent for proposals blocked before the dispatch ledger existed) are
    eligible. A pending/current dispatch, published card, active or consumed
    nonce, terminal/approved/cancelled rung, expired/closed/unknown approval
    window, unsupported action, or market order fails closed.
  - Every rung receives a fresh broker-safe dry-run preview. The normalized
    executable price and quantity must exactly match the stored proposal, and a
    limit that crossed the current market is rejected as a price departure.
  - `dry_run=false` repeats the checks and sends only a fresh Telegram approval
    card. It never approves or submits an order. A DB-row-locked nonce guard
    rejects concurrent redispatch after the first attempt becomes pending or
    current, so two live approval cards cannot be minted for one proposal.
- `order_proposal_void(proposal_id, reason)`
  - Requires a non-blank operator reason.
  - Pre-submit rungs retain the existing local `voided` path. An `unverified`
    rung is eligible only after a five-minute broker settlement grace and a
    fresh account-scoped lookup prove the order absent; it then becomes
    `voided_local_stale`, with the lookup scope and result appended to
    `void_reason`.
  - A found broker order, incomplete lookup, timeout, provider error, or local
    accepted-only Toss ledger row rejects the whole request without partial
    mutation. Timeout-to-`unverified` is never auto-voided.
  - For a Toss rung without a broker order ID, absence requires both zero
    accepted-only `toss_live_order_ledger` rows and a complete OPEN+CLOSED scan
    with no order matching the normalized symbol, side, quantity, and price.
    Quantity and price use finite Decimal comparison rather than string equality.
    The per-rung attempt window is inclusive from `created_at - 24h` through
    `max(valid_until, updated_at) + 24h`; the broker scan covers the union of
    those windows' KST dates. Provider errors, timeouts, malformed potential
    matches, invalid pagination, and CLOSED page-cap exhaustion make the scan
    incomplete and fail closed instead of proving absence.
  - After a successful void, the recorded Telegram approval message is edited
    without an inline keyboard so stale approval buttons no longer remain live.

Telegram approval rechecks the persisted dispatch policy stamp, proposal
validity, and authoritative market session before consuming a nonce, then
repeats the same window policy inside revalidation and through a pre-send hook
at the final Toss HTTP boundary for submit and cancel. The
session evidence includes the allowed interval end, so a close crossed while
an awaited check is running is rejected at the transport boundary. A missing
or mismatched policy stamp and unknown/stale evidence fail closed. An expired
proposal converges through the existing proposal/rung expiry transitions
without rolling back a sibling that already has broker evidence. A still-valid
closed-session proposal returns
`DEFER_SESSION_CLOSED` with the next confirmed session; it is never scheduled
or automatically resubmitted. An initial late pre-send block proves provider
HTTP=0; a retry-time block may follow one explicit 429/auth rejection but never
an accepted or ambiguous mutation. With that proof and no sibling mutation,
the callback restores the rung, lease, and durable proposal nonce instead of
stranding a consumed approval.
Batch approval locks and verifies the exact ordered proposal/nonce-snapshot
membership before consuming its own nonce; one missing, changed, expired, or
closed member blocks the whole displayed batch. Toss then reruns its applicable
ROB-800 checks through the broker-specific submit boundary.
Successful automatic submissions retain `approved_by_telegram_user_id=NULL`
and write policy/version/eligibility evidence under
`source_asof.auto_approved`. Their Telegram summary carries a single-use
`취소` veto button; the callback uses the existing broker cancel adapter and a
fresh status lookup to converge the rung to `cancelled` or report `체결됨`.
Failure to deliver that post-submit summary triggers the same immediate
cancel-and-confirm compensation and is audited in `source_asof`.
`ORDER_PROPOSALS_SUBMIT_AGENT_ID` has no default identity (`""`) and is used
only while the Telegram callback revalidates and submits the approved proposal.
Operators must set it explicitly and add the exact same trimmed identity to
`LOSS_CUT_ALLOWED_AGENT_IDS`; missing or whitespace-only values bind no caller
identity, so `loss_cut` validation fails closed. Do not use a hardcoded UUID as
a fallback for this setting.

Loss-cut proposal binding supports only `toss_live` equities
(`equity_kr`/`equity_us`). Historical KIS and Upbit proposal rows retain their
original provenance but are never resubmitted or rerouted to Toss.
`toss_preview_order` owns the wire price/quantity used for revalidation, including KR tick normalization, and
provides its read-only warning, price/cost, NXT-context, and advisory sector
concentration checks. For `loss_cut`, preview and submit both reuse the shared
ROB-800 validator (caller allowlist and matching fresh retrospective), exempt
only the validated request from the average-cost floor, and enforce the
configured current-price slip band. No external approval backend is queried.
`toss_place_order` runs the confirmation/activation, high-value, warnings,
opposite-pending-order, sell-loss, and configured NXT guards immediately before
POST. A Toss loss-cut live send requires the exact supplied preview approval
token even when `TOSS_APPROVAL_HASH_MODE=off`; sell-loss and required mutation
gates fail closed, while sector
concentration remains advisory. Proposal `Decimal` values cross the Toss
boundary as exact `str | int` values, never floats. Proposal revalidation binds
a private client ID derived only from `proposal_id + rung` around both preview
and submit, verifies the preview returned that exact ID, and privately carries
the rung correlation into the Toss ledger. Neither value is operator-controlled
through the MCP tool schema. The rung ledger stores the actual
`approval_hash_digest` returned by `toss_place_order`, not the raw token.

An accepted send is still not a fill. `toss_reconcile_orders(dry_run=False)`
books confirmed cumulative evidence and projects partial/fill/broker-confirmed
cancel onto `order_proposal_rungs`. Cancellation preserves an already projected
partial quantity. A non-dry reconcile also sweeps terminal Toss ledger rows
still joined to non-terminal proposal rungs, so a transient projection failure
does not become permanent drift. Dry runs never write either ledger.
Any post-send timeout or ledger ambiguity remains `unverified`. Telegram result
messages surface a bounded rejection or guard reason: at most 240 characters,
followed by an ellipsis when truncated.

### Account Routing

MCP account-facing tools use an explicit `account_mode`:

- `account_mode="toss_live"` or omitted: Toss Securities KR/US live account.
  Reads require Toss configuration; mutation POSTs additionally require
  `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true`. Live sells retain the fresh
  broker-authoritative sellable-quantity preflight.
- `account_mode="upbit"`: historical provenance selector only; operational
  account and order dispatch reject it because Upbit is not operational.
- `account_mode="db_simulated"`: DB-backed paper trading only. Existing
  `paper`/`simulated` aliases remain simulation-only and never become live.
- `account_mode="kis_live"` and `account_mode="kis_mock"`: historical selectors
  may still be parsed for ledger reads, but operational account/order dispatch
  rejects them with `provider kis is not operational`.
- `account_mode="real"` and `account_mode="live"` are ambiguous and rejected;
  callers must name `toss_live`.


### Toss Live Order MCP Tools

The `default` profile registers eight typed `toss_live` MCP tools:
- `toss_preview_order`
- `toss_place_order`
- `toss_modify_order`
- `toss_cancel_order`
- `toss_get_order_history`
- `toss_get_positions`
- `toss_get_orderable_cash`
- `toss_reconcile_orders`

Operator activation and the one-share live smoke are documented in
[`docs/runbooks/toss-live-smoke.md`](../../docs/runbooks/toss-live-smoke.md).

#### Toss Safety Rules and Gates

- **API Enablement**: Toss live tools are default-disabled. They fail closed unless `TOSS_API_ENABLED=true` and `validate_toss_api_config()` returns no missing keys.
- **Account Mode Routing**: All Toss tools require `account_mode="toss_live"` (or `account_type="toss_live"`) and reject any mismatched account parameters.
- **Mutation Safety (Dry-Run, Confirm, and Activation Gate)**: All mutation tools (`toss_place_order`, `toss_modify_order`, `toss_cancel_order`) default to `dry_run=True`. They perform actual HTTP requests (POSTs) to Toss Securities only when `dry_run=False`, `confirm=True`, and `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true` are explicitly set. Keep `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=false` until the accepted-order ledger and operator live-smoke hold are cleared.
- **Accepted-only ledger and reconcile**: Real `toss_place_order` writes only an accepted/rejected row to `review.toss_live_order_ledger`. It does not create fills, journals, or realized PnL at send time. `toss_reconcile_orders(dry_run=True)` previews broker evidence from `GET /orders/{orderId}`; `dry_run=False` books only confirmed execution deltas. GET order-detail `403 non-json-response` failures are retried once after token reissue; unresolved failures are persisted as `requires_manual_review=true`. Mutation POSTs are not implicitly retried on that error.
- **Loss-cut authorization**: Direct `toss_preview_order`/`toss_place_order` loss-cut calls fail closed and point callers to `order_proposal_create`. A loss-cut proposal requires a live limit sell, eligible `exit_reason`, matching <=72h retrospective, non-empty `approval_issue_id`, allowed submit identity, slip-band compliance, and a valid approval hash. Telegram's first approval click only renders evidence and issues a proposal/rung/revision-bound 90-second `⚠️ 손절 확인` nonce; the second click reruns every guard and may submit. The issue ID is retained for audit and never externally queried. Toss ledger rows retain `exit_intent`, `retrospective_id`, and `approval_issue_id` for audit.
- **Loss-cut polling SLA**: Toss has no fill push in this path and both automatic polling paths are default-off. Before enabling Toss loss-cut, either enable and operationally verify `TOSS_FILL_POLL_ENABLED` with an approved `TOSS_FILL_POLL_CRON`, or require a targeted `toss_reconcile_orders(order_id=<broker-order-id>, dry_run=False)` immediately after execution. Non-dry reconcile projects broker evidence to proposal rungs and idempotently repairs terminal-ledger projection misses.
- **Proposal identity handoff**: Order-proposal flows privately bind a stable client ID derived from `proposal_id + rung` around both `toss_preview_order` and `toss_place_order`, then require preview to return that exact ID. The proposal correlation and rung are carried through the same internal binding into the Toss ledger. These values are not exposed as operator-controlled MCP parameters. Accepted responses expose `approval_hash_digest`, the canonical ledger digest; the raw approval token is used only as the `approval_hash` submit input.
- **US FX PnL split**: Toss order detail does not provide fill-time FX. For US rows only, `toss_reconcile_orders(dry_run=False)` captures USD/KRW through `exchange_rate_service` at reconcile time. Buy rows persist `buy_fx_rate`; sell rows persist `sell_fx_rate`, FIFO-attributed `fx_pnl_krw`, `security_pnl_usd`, `security_pnl_krw`, and `total_pnl_krw`. Automatic values are labelled `fx_rate_source="reconcile_spot"` and `fx_pnl_accuracy="approximate"`. Legacy lots with no buy FX keep FX PnL fields null until an operator backfills exact values through `modify_journal_entry`.
- **Fill Notifications (ROB-576)**: `toss_reconcile_orders(dry_run=False)` sends a Discord/Telegram fill notification only when `TOSS_FILL_NOTIFY_ENABLED=true`, the reconcile pass books a new fill delta, and the shared `TradeNotifier` has a KR/US webhook or Telegram fallback configured. Notifications reuse the existing fill card format and route by `market='kr'|'us'`; Toss fill enrichment is intentionally disabled (`enrichment=None`) until Toss account PnL/position enrichment exists. The optional paused TaskIQ task `toss_live.reconcile_periodic` calls `toss_reconcile_orders_impl(dry_run=False)` only when both `TOSS_LIVE_AUTO_RECONCILE_ENABLED=true` and `TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED=true`. It has no in-repo schedule; operator automation must register/unpause the cadence externally.
- **Toss fill poller (ROB-757)**: `toss_live.poll_fills_periodic` is default-off behind `TOSS_FILL_POLL_ENABLED`. It scans Toss `GET /orders` read-only, records app-direct orders missing from `review.toss_live_order_ledger`, and reuses `toss_reconcile_orders` to book confirmed deltas. New Toss fill deltas are also upserted into `review.execution_ledger` with `broker='toss'`, `account_mode='live'`, and `source='reconciler'`; ROB-755 triage should read them with `source='reconciler', broker='toss'`. For Toss loss-cut proposals, this poller/cadence or the targeted reconcile above is mandatory support infrastructure.
- **High-Value Orders**: KR orders with a computable notional value >= 100,000,000 KRW fail locally unless `confirm_high_value_order=True` is supplied.
- **KR Stock Warnings**: KR order previews include active Toss warning rows. Confirmed non-dry-run KR orders call the same warnings guard before mutation and block active `LIQUIDATION_TRADING`; Toss warning lookup failures are fail-open and reported as `warnings_check_message`.
- **Preview Market And Cost Context**: `toss_preview_order` is read-only but enriches the payload preview with Toss quote and cost context. It returns `current_price`, `current_price_currency`, `fill_distance` for off-market limit prices, `order_warnings` for marketability/fill-risk strings, `estimated_value`, `fee`, `fee_currency`, `fx_cost_full_conversion`, `fx_cost_full_conversion_currency`, and `estimated_costs`. The existing `warnings` field remains reserved for Toss stock-warning rows; string order warnings are not mixed into it. US `fx_cost_full_conversion` assumes the full order notional is converted KRW->USD and is labelled `fx_assumption="full_notional_krw_conversion"`; use `suggest_order_account` for cash-aware routing cost comparison.
- **Sell Loss-Sell Guard**: For ordinary sell orders and all sell reprices, holdings cost basis is validated. They block locally if the execution price (limit) or current market proxy price (market) is below `average_purchase_price * 1.01`. Only a fully validated `loss_cut` limit sell bypasses that floor, and it remains bounded by `current_price * (1 - loss_cut_max_slip)`. If holdings, cost basis, or the loss-cut current price cannot be resolved, the sell fails closed.
- **Opposite Pending Orders**: Before placing a non-dry-run order, the tool queries all paginated `OPEN` order pages for the symbol and blocks the order if an opposite-side pending order already exists. Pagination anomalies fail closed.
- **Modify Semantics**:
  - KR modify requires both `new_price` and `new_quantity`.
  - US modify requires `new_price` and rejects `new_quantity`.
  - Cancel/modify responses surface `replacement_order_id` and semantic notes indicating that Toss issues a new replacement `orderId` instead of modifying/canceling the original one in-place.

> [!IMPORTANT]
> **Implementation Hold Status**: Toss live order MCP tools implemented under ROB-531 are under `hold_for_final_review`. Do not merge, deploy, or execute live Toss orders until a stronger review clears the safety boundaries and confirmation gates.

### `get_orderbook` spec
Parameters:
- `symbol`: Upbit market code such as `KRW-BTC` (required)
- `market`: must be a crypto alias (`"crypto"`, `"upbit"`); every other market,
  including the KR/US equity aliases, is rejected because no equity orderbook
  provider remains
- `venue`: rejected when non-blank

Behavior:
- These unsigned public-data calls require no Upbit access or secret key.
- Crypto orderbook requests require explicit `market="crypto"` (or `"upbit"`) and a raw `KRW-*` symbol such as `KRW-BTC`; plain coins (`BTC`) and non-KRW crypto pairs (`USDT-BTC`) raise an argument error
- Providing a non-blank `venue` raises an argument error
- Successful responses use `source: "upbit"`, `instrument_type: "crypto"`, and may return non-empty wall arrays
- Successful responses always include MCP-only derived fields: `pressure`, `pressure_desc`, `spread`, `spread_pct`, `bid_walls`, and `ask_walls`
- Invalid input raises; upstream failures for otherwise valid requests return an in-band error payload via the shared MCP error contract. When the underlying exception is a `DomainServiceError`, the payload may also include `error_type`

Response format:
```json
{
  "symbol": "KRW-BTC",
  "instrument_type": "crypto",
  "source": "upbit",
  "asks": [{"price": 70100.0, "quantity": 123.0}],
  "bids": [{"price": 70000.0, "quantity": 321.0}],
  "total_ask_qty": 1000.0,
  "total_bid_qty": 1500.0,
  "bid_ask_ratio": 1.5,
  "pressure": "buy",
  "pressure_desc": "매수잔량이 매도잔량의 1.5배 - 매수 압력",
  "spread": 100.0,
  "spread_pct": 0.143,
  "bid_walls": [],
  "ask_walls": []
}
```

Derived fields:
- `pressure` is derived from `bid_ask_ratio` using fixed inclusive boundaries:
  - `ratio > 2.0` -> `strong_buy`
  - `ratio > 1.3` (i.e. 1.3 excluded) -> `buy`
  - `ratio >= 0.7` (i.e. 0.7 included, up to 1.3 inclusive) -> `neutral`
  - `ratio >= 0.5` (i.e. 0.5 included, below 0.7) -> `sell`
  - `ratio < 0.5` -> `strong_sell`
- `pressure_desc` is a Korean interpretation string. `strong_buy`/`buy` use `total_bid_qty / total_ask_qty`, `strong_sell`/`sell` use `total_ask_qty / total_bid_qty`, and `neutral` is always `"매수/매도 잔량이 균형권 - 중립"`
- If `bid_ask_ratio` is `null`, both `pressure` and `pressure_desc` are `null`
- `spread` is `asks[0].price - bids[0].price` when both best levels exist; otherwise it is `null` (integer-valued for KR, fractional-capable for crypto)
- `spread_pct` is `(spread / bids[0].price) * 100`, rounded to 3 decimal places, and becomes `null` when the best bid is missing or `<= 0`
- `bid_walls` / `ask_walls` are MCP-only convenience fields. They are calculated by taking each side's `value_krw = round(price * quantity)`, using the side median as the baseline, selecting levels where `value_krw >= baseline * 2`, sorting by `value_krw` descending, and returning up to 3 entries shaped as `{price, size, value_krw}`


### US symbol/exchange resolution
- US symbol search and order routing resolve from DB table `us_symbol_universe` only.
- Runtime does not use in-memory/file-cache fallback for US symbol/exchange lookups.
- If symbol/name is missing, inactive, or ambiguous in `us_symbol_universe`, tools return explicit lookup errors with sync hint.
- US prerequisite: run `make sync-us-symbol-universe` (or `uv run python scripts/sync_us_symbol_universe.py`) right after migrations.

### KR symbol resolution
- KR symbol search resolves from DB table `kr_symbol_universe` only.
- Runtime does not use in-memory/file-cache fallback for KR name/symbol lookups.
- If symbol/name is missing, inactive, or ambiguous in `kr_symbol_universe`, tools return explicit lookup errors with sync hint.
- KR prerequisite: run `make sync-kr-symbol-universe` (or `uv run python scripts/sync_kr_symbol_universe.py`) right after migrations.

### Upbit symbol resolution
- Upbit crypto symbol/market resolution uses DB table `upbit_symbol_universe` only.
- Runtime does not call Upbit `/v1/market/all`; that endpoint is sync-path only.
- If `upbit_symbol_universe` is empty/unavailable, tools fail fast with explicit sync hint.
- If a coin/market lookup is missing or inactive in `upbit_symbol_universe`, MCP tools generally propagate explicit lookup errors (no silent fallback/default ticker).
- `search_symbol` (crypto) uses DB-backed `search_upbit_symbols` only; in-memory map-based search is removed.
- Upbit prerequisite: run `make sync-upbit-symbol-universe` (or `uv run python scripts/sync_upbit_symbol_universe.py`) right after migrations.
- Scheduled sync task `symbols.upbit.universe.sync` runs daily at `06:15` KST (`cron: 15 6 * * *`, `cron_offset: Asia/Seoul`).

### `get_indicators` spec
Parameters:
- `symbol`: Asset symbol/ticker
- `indicators`: Indicator list (e.g. `rsi`, `sma`, `obv`)
- `market`: Optional explicit market (`crypto`, `kr`, `us`)

Symbol/market contract:
- `market` is required when `symbol` is a plain alphabetic token (for example `AAPL`, `ETC`).
- If omitted for plain alphabetic symbols, `get_indicators` raises:
  - `"market is required for plain alphabetic symbols. Use market='us' for US equities, or provide KRW-/USDT- prefixed symbol for crypto."`
- Crypto symbols continue to support prefix-based routing (`KRW-` / `USDT-`) and can omit `market`.
- This requirement is specific to `get_indicators`; other tools keep their existing routing behavior.

Examples:
- Allowed: `symbol="AAPL", market="us"`
- Allowed: `symbol="KRW-ETC", market="crypto"`
- Allowed: `symbol="KRW-ETC"` (market omitted)
- Rejected: `symbol="ETC"` (market omitted)

### `analyze_stock` spec

Parameters:
- `symbol`: Asset symbol/ticker/code (required; string or int accepted)
- `market`: Market - `"kr"`, `"us"`, `"crypto"` (optional, inferred from symbol if omitted)
- `include_peers`: Whether to include sector peer analysis for KR/US equities (default: false; ignored for crypto)

Response notes:
- Equity responses (KR/US markets) include `recommendation.rsi14` when RSI(14) is available from the indicator payload
- This field provides a convenient summary; callers should continue to use `get_indicators` when they need the full indicator set rather than the summarized recommendation field
- Responses include the same ROB-1048 seven-field freshness/provenance envelope
  documented under `analyze_stock_batch`. `derived_as_of`/`fetched_at` are null
  when provider evidence failed or has no trustworthy acquisition timestamp;
  response construction time is never substituted.

### `get_correlation` spec
Parameters:
- `symbols`: List of asset ticker/code inputs (required, 2-10 entries)
- `period`: Lookback window in days (default: 60, minimum effective value: 30, maximum: 365)

Symbol contract:
- `get_correlation` has no `market` parameter and therefore accepts ticker/code inputs only.
- Mixed-market ticker/code inputs continue to work, including KR codes such as `005930`, US tickers such as `AAPL`, and crypto symbols such as `KRW-BTC`.
- Company-name inputs such as `삼성전자` or `Apple Inc.` are rejected with:
  - `"get_correlation does not support company-name inputs because it has no market parameter. Use ticker/code inputs directly."`
- When at least 2 ticker/code inputs resolve and fetch successfully, the tool still returns a correlation matrix and includes failed symbols in `errors`.

### `get_earnings_calendar` spec

Parameters:
- `symbol`: Optional equity ticker/code. US examples: `AAPL`, `MSFT`; KR examples: `005930`, `A005930`.
- `from_date`: Optional ISO start date, inclusive. Defaults to server `today`.
- `to_date`: Optional ISO end date, inclusive. Defaults to `from_date + 30 days`.
- `market`: Optional explicit market (`us`, `kr`). If omitted, 6-digit or A-prefixed KR codes route to KR; other non-crypto symbols route to US.

Behavior:
- US requests keep the existing Finnhub path and response shape: `symbol`, `instrument_type`, `source`, `from_date`, `to_date`, `count`, `earnings`.
- KR requests read existing `market_events` rows where `category="earnings"` and `market="kr"`.
- KR rows are read-only and may come from `source="wisefn"` scheduled earnings or `source="dart"` filings classified as earnings.
- KR response top-level includes `source="market_events"`, `sources`, `market="kr"`, `warning`, and the existing `earnings` list.
- KR `earnings` items include `symbol`, `company_name`, `date`, `hour`, `time_hint`, `quarter`, `year`, `status`, `source`, `source_event_id`, `source_url`, and `title`.
- KR `eps_*` and `revenue_*` fields are present for shape compatibility but usually `null` until realized-value joins are implemented.

Limitations:
- KR shareholder meetings, ex-dividend dates, IR, and conferences are not collected by this tool yet.
- Empty KR results mean no matching `market_events` rows are currently stored for the requested window; they do not prove there is no real-world event.
- Production WiseFn ingestion enablement and scheduler activation are operational follow-ups, not part of this MCP read-path contract.

Errors:
- Crypto symbols return an explicit error because earnings calendars apply to equities only.
- `from_date > to_date` is rejected.
- Explicit `market="us"` with a Korean equity code is rejected with guidance to use `market="kr"`.

### `get_disclosures` spec
Parameters:
- `symbol`: Korean corporation lookup input (required)
- `days`: Lookback window in days (default: 30)
- `limit`: Maximum filings to return (default: 20)
- `report_type`: Optional Korean disclosure group (`정기`, `주요사항`, `발행`, `지분`, `기타`)

Symbol contract:
- Direct 6-digit KR stock codes such as `005930` are passed through to OpenDartReader as-is.
- Korean company names such as `삼성전자` are supported on a best-effort basis through OpenDartReader's exact-name corp lookup.
- Blank or whitespace-only `symbol` inputs are rejected with an explicit in-band error payload (`success: false`, `error: "symbol is required"`, `filings: []`, `symbol: ""`).
- Company-name inputs that OpenDartReader cannot resolve return an explicit in-band error payload with `success: false`; they do not silently degrade to an empty `filings` list.

Behavior:
- `report_type` maps internally to DART disclosure kinds: `정기 -> A`, `주요사항 -> B`, `발행 -> C`, `지분 -> D`, `기타 -> E`.
- Unsupported `report_type` inputs return `success: false` instead of silently broadening the query.
- Successful responses return the existing `filings` list shape with `date`, `report_nm`, `rcp_no`, and `corp_name`.
- An empty DataFrame from OpenDartReader is treated as a successful lookup with `filings: []`.
- The first process-local client initialization still downloads the OpenDART corp-code cache, so cold-start latency can be higher than warm calls.

Error payload:
- Failure responses include `success`, `error`, `filings`, and `symbol`.

### `get_investment_opinions` spec
Parameters:
- `symbol`: Asset ticker/code input (required)
- `limit`: Maximum detailed opinion rows to return (default: 10)
- `market`: Optional explicit market (`kr`, `us`)

Behavior:
- KR requests keep the existing Naver Finance path and return recent analyst opinions plus consensus statistics.
- US requests use yfinance and keep the public top-level shape: `symbol`, `count`, `opinions`, `consensus`, plus optional `warning`.
- US `opinions` remains the recent Yahoo `upgrades_downgrades` event list (firm/rating/date plus row-level `target_price` when Yahoo provides one).
- US top-level `count` remains `len(opinions)`; it does **not** represent aggregate analyst coverage.
- US `consensus.total_count` is the aggregate analyst coverage count from the current Yahoo `recommendationTrend` / `ticker.recommendations` row (`period="0m"` preferred).
- US aggregate count mapping is:
  - `buy_count = strongBuy + buy`
  - `hold_count = hold`
  - `sell_count = sell + strongSell`
  - `strong_buy_count = strongBuy`
  - `total_count = strongBuy + buy + hold + sell + strongSell`
- US target statistics (`avg_target_price`, `median_target_price`, `min_target_price`, `max_target_price`, `current_price`, `upside_pct`) come from Yahoo `analyst_price_targets` after numeric normalization.
- US target normalization accepts Yahoo raw dicts such as `{raw, fmt}`, plain numbers, and pandas/numpy scalars; `0`, negative, empty, and non-numeric placeholders are treated as unavailable.
- When Yahoo analyst counts or target statistics are unavailable, the corresponding US `consensus` fields are returned as `null` instead of fabricated zeroes.
- When Yahoo provides neither usable aggregate counts nor usable analyst target data, the US response includes a top-level `warning`.

### `investment_report_create` item contract

`investment_report_create` persists one advisory report bundle and does not submit broker orders. The report idempotency key is `(report_type, market, market_session, account_scope, execution_mode, kst_date, generator_version)`. To create a new row for an updated draft, bump `generator_version` or another keyed field.

Each `items[]` object requires:
- `client_item_key`: caller-stable item key within the report.
- `item_kind`: `action`, `watch`, or `risk`.
- `intent`: `buy_review`, `sell_review`, `risk_review`, `trend_recovery_review`, or `rebalance_review`.
- `rationale`: human-readable thesis.

Optional typed item fields:
- `evidence`: `[{source, metric, value, as_of, freshness}]`; `source` is required.
- `freshness`: `fresh`, `soft_stale`, `stale`, or `unknown`.
- `entry_plan`: `[{label, price, quantity, notional, currency, condition, rationale}]`.
- `stop_loss`: `{price, quantity, notional, currency, condition, rationale}`.
- `target_price`: `{price, quantity, notional, currency, condition, rationale}`.
- `linked_order_ids`: `[{broker, account_scope, order_no, odno, ledger_id, report_item_uuid, raw}]`.

The lite quality basis `item_evidence_lite` reads `evidence[]` and item-level `freshness`; arbitrary `evidence_snapshot` keys are not counted as typed evidence. Typed trade-plan fields round-trip under reserved keys in `items[].evidence_snapshot`.

Unknown top-level item keys are rejected with `error: "invalid_items"`. Put caller-specific extension data under `metadata` or raw `evidence_snapshot` explicitly.

Order linkage note: `linked_order_ids` is report-side reference metadata. For new live orders, pass the report item's `item_uuid` as `report_item_uuid` to the order tool so ROB-473 ledger audit linkage is populated.

Watch execution context fields:
- `trigger_checklist`: `string[]`; copied into watch alert notifications so the operator can re-check the trigger.
- `max_action`: structured watch execution-plan JSON. `account_mode` is required when `max_action` is present; it also requires `side` and exactly one of `quantity` or `notional`. Optional keys include `amount_krw`, `limit_price`, `limit_price_hint`, and `ladder_level`.
- Do not send `planned_action` in item input. `planned_action` is derived from `max_action` when Hermes watch payloads are built.

### `manage_watch_alerts` — removed (ROB-265)

The legacy Redis-backed `manage_watch_alerts` MCP tool was removed by
ROB-265 along with the `watch_alerts` / `watch_scanner` Redis surface.
`review.watch_order_intent_ledger` is retained for historical rows only: its
writer service was removed with the mock-only auto-execution path.
Report-scoped watches now flow through
`investment_report_activate_watch` (which copies an approved watch
item into `investment_watch_alerts` as an immutable activation
snapshot) and the `investment_watch_scanner` job (which evaluates
those alerts, writes `investment_watch_events` with the full trigger
identity snapshot, and emits Hermes review-trigger notifications).
Watches are review triggers by default. The explicit `auto_execute_mock` mode
is restricted to the owner-scoped `db_simulated` paper account and dispatches
through the Android `PaperOrderFacade`; `kis_mock` and every live broker are
rejected. Its event starts with `outcome='pending'` and becomes `executed` only
after positive executor evidence. Delivery state is auditable per event row
(`delivery_status` / `delivery_reason` / `delivered_at` / `delivery_attempts`).

### `list_active_watches`

Read-only active watch discovery for `review.investment_watch_alerts`.

Parameters:
- `market`: optional `kr`, `us`, or `crypto`.
- `symbol`: optional exact symbol filter.
- `include_expired_status_rows`: default `false`. When `false`, only returns `status='active'` rows whose `valid_until` is still in the future. When `true`, includes rows that remain `status='active'` even if `valid_until` has passed, for scanner-lag diagnostics.
- `limit`: default `100`, clamped to `1..250`.

Response includes `active_watches[]` with `symbol`, `operator`, `threshold`, `valid_until`, `rationale`, `source_report_uuid`, and `source_item_uuid`.

### `investment_watch_create`

Creates an active row in `review.investment_watch_alerts` without creating an
investment report or report item.

Because a direct watch has no report/item source, its `source_report_uuid` and
`source_item_uuid` are `null`. Do not synthesize or look up a report for these
rows; use the alert's immutable rationale, checklist, max-action, and condition
snapshot. Report-activated watches continue to carry real source UUIDs.

Parameters:
- `created_by`: required provenance label. Use `tradingcodex` from the
  `tradingcodex_execution` profile.
- `market`: `kr`, `us`, or `crypto`.
- `symbol`: exact symbol. US and crypto symbols are normalized uppercase.
- `intent`: one of the investment report item intents, for scanner/event context.
- `rationale`: operator-readable reason for the watch.
- `watch_condition`: same normalized watch condition payload used by report items.
  Flat `{metric, operator, threshold}` and v2 `conditions[]` are accepted.
- `valid_until`: future timezone-aware ISO8601 timestamp.
- `trigger_checklist`: optional list of operator checks.
- `max_action`: optional planned-action envelope for downstream approval context.
- `metadata`: optional audit metadata merged into alert `metadata`.
- `idempotency_key`: optional caller key. Omit to use the deterministic direct-watch key.

Response:
- `success`
- `idempotent`
- `alert`, the created or existing `InvestmentWatchAlertResponse`.

This tool never creates reports/items and never submits, previews, modifies,
cancels, or reconciles broker orders.

### `investment_watch_void`, `investment_watch_expire`, and `sweep_expired_watches`

ROB-971 lifecycle controls for existing watch-alert rows. `investment_watch_void`
cancels one active invalid, orphaned, duplicate, or zombie watch and requires an
operator reason. `investment_watch_expire` explicitly expires one active stale
watch. `sweep_expired_watches(dry_run=true)` lists all active watches whose
`valid_until` has passed; set `dry_run=false` to expire that set. The sweep is
also available as a scheduleless, environment-gated TaskIQ task; production
recurrence remains an operator decision after manual reps. These tools never
touch a broker or order path. See the tool descriptions for the single-watch vs
bulk-cleanup choice.

### `get_operating_briefing`

Read-only one-call bootstrap for a new operating session.

Parameters:
- `market`: required `kr`, `us`, or `crypto`.
- `account_scope`: optional. Defaults are `kr/us -> kis_live`, `crypto -> upbit_live`.
- `session_context_limit`: default `10`, clamped by the session context service.
- `include_current_price`: default `true`.
- `cohort`: optional, default `live_gated`. Realized trade-journal cohort to load (e.g., `live_gated`, `mock_counterfactual`).
- `include_counterfactual_delta`: default `false`. When `true`, returns aggregates delta scoreboard comparing `live_gated` and `mock_counterfactual` cohorts.

Response sections:
- `holdings`: summary and top movers derived from `get_holdings`.
- `pending_orders`: pending-order snapshot with `expected_expiry` when factually derivable.
- `active_watches`: same active watch rows as `list_active_watches`.
- `latest_report`: latest report summary and item status counts, or `null`.
- `session_context`: recent ROB-516 handoff entries.
- `staleness`: per-section `as_of`, freshness, and unavailable reason where available. If an optional DB-backed section (`active_watches`, `latest_report`, or `session_context`) raises, the tool still returns `success=true`; that section is returned as an empty or null fallback and `staleness.<section>.freshness_status` is `unavailable` with `unavailable_reason`.
- `trading_scoreboards`: trading scoreboard or counterfactual delta metrics, depending on `include_counterfactual_delta` parameter.

The tool never submits, modifies, cancels, reconciles, activates, expires, or mutates orders/watches/session context.


### `get_trading_scoreboard`

Query setup-tagged trade-journal aggregates over closed round-trips reconstructed from fills.

Parameters:
- `market`: optional `kr`, `us`, or `crypto`.
- `account_mode`: optional.
- `date_from`: optional date (YYYY-MM-DD).
- `date_to`: optional date (YYYY-MM-DD).
- `setup_tag`: optional tag filter.
- `min_sample`: default `1`.
- `cohort`: default `live_gated`. Realized trade-journal cohort to load (e.g., `live_gated`, `mock_counterfactual`).
- `include_counterfactual_delta`: default `false`. When `true`, returns aggregates delta scoreboard comparing `live_gated` and `mock_counterfactual` paired by shared `report_item_uuid` where available. `correlation_id` is still considered for legacy rows, but live place-time IDs are account-scoped and should not be expected to equal `mirror:{item_uuid}`.
- `min_pair_threshold`: default `20`. Only affects `pairing_health`; it does not filter rows.
- When `include_counterfactual_delta=True`, `market`, `account_mode`, `date_from`, `date_to`, `setup_tag`, `min_sample`, and `min_pair_threshold` are passed into the delta builder and echoed under `filters`.

Returns Win-rate, expectancy (% and R-multiple), profit factor, average/worst MAE and MFE.

When `include_counterfactual_delta=true`, the response additionally carries:
- `pairing_diagnostics`: closed-trade and key-coverage counts used to explain why pairs did or did not form.
- `pairing_health`: `ok`, `warming_up`, or `needs_design_review` based on `paired_count`, closed sample availability, and `min_pair_threshold`.

**Order linkage note**: For report-originated live orders, passing `report_item_uuid` is required for counterfactual pairing; without it, `paired_count` can remain zero even when both live and mock cohorts have closed trades.


### `screen_stocks` spec
Parameters:
- `market`: Market to screen - "kr", "kospi", "kosdaq", "konex", "all", "us", "crypto" (default: "kr")
- `asset_type`: Asset type - "stock", "etf", "etn" (only applicable to KR, default: None)
- `category`: Category filter - ETF categories for KR, sector for US (default: None)
- `sector`: Sector filter for KR/US stocks (default: None). Not supported for crypto or KR ETF/ETN requests
- `exclude_sectors`: Sector exclusion list for KR/US stocks (default: None). Values are de-duplicated case-insensitively for ASCII labels
- `instrument_types`: Instrument taxonomy filter list - "common", "preferred", "etf", "reit", "spac", "unknown" (default: None)
- `adv_krw_min`: Minimum 30-day average daily value in KRW. Use 1,000,000,000 for a conservative liquidity floor or 5,000,000,000 for an aggressive liquidity floor
- `market_cap_min_krw`: Minimum market capitalization in KRW (default: None)
- `market_cap_max_krw`: Maximum market capitalization in KRW (default: None)
- `sort_by`: Sort criteria - "volume", "trade_amount", "market_cap", "change_rate", "dividend_yield", "rsi" (default: crypto="rsi", KR/US="volume")
- `sort_order`: Sort order - "asc" or "desc" (default: "desc")
- `min_market_cap`: Minimum market cap (억원 for KR, USD for US; not supported for crypto)
- `max_per`: Maximum P/E ratio filter (not applicable to crypto)
- `min_dividend_yield`: Minimum dividend yield filter (accepts both decimal, e.g., 0.03, and percentage, e.g., 3.0; values > 1 are treated as percentages) (not applicable to crypto)
- `min_dividend`: Alias for `min_dividend_yield`. Accepts same format. If both specified, they must be equal
- `min_analyst_buy`: Minimum analyst buy count filter (default: None). Only supported for KR/US stocks (not ETF/ETN)
- `max_rsi`: Maximum RSI filter 0-100 (not applicable to sorting by dividend_yield in crypto)
- `limit`: Maximum results 1-100 (default: 50)

Market-specific behavior:
- **KR market**:
  - `market="konex"` screens KONEX only; `market="all"` screens KOSPI, KOSDAQ, and KONEX
  - Default `asset_type in {None, "stock"}` + `category=None` requests use tvscreener only when verified KR stock-query capabilities cover the request; otherwise they fall back to the legacy KRX/Naver path before entering tvscreener
  - Successful stock responses expose `meta.source = "tvscreener"` and include `adx`, `instrument_type`, and 30-day ADV fields when TradingView provides them
  - `adv_krw_min` uses TradingView 30-day average volume multiplied by price; responses set `meta.adv_window_days = 30` when this filter is requested
  - Legacy KRX fallback cannot compute `adv_krw_min`; it returns a warning and skips only that filter
  - `sort_by="rsi"` is supported via tvscreener RSI data; legacy path falls back to OHLCV-based RSI enrichment
  - ETF/category requests stay on the legacy KRX/Naver path
  - KRX data cached with 300s TTL (Redis) + in-memory fallback
  - Trading date auto-fallback (up to 10 days back)
  - Category filter auto-limits to ETFs if `asset_type=None`
  - ETN (`asset_type="etn"`) not supported - returns error

- **US market**:
  - Default `asset_type in {None, "stock"}` requests use tvscreener only when verified US stock-query capabilities cover the request
  - US `category`/`sector` alias requests stay on the tvscreener path only when the TradingView sector filter capability is verified; otherwise they fall back to legacy before running the tv query
  - `sort_by="rsi"` is supported via tvscreener RSI data; legacy yfinance path falls back to OHLCV-based RSI enrichment
  - Successful stock responses expose `meta.source = "tvscreener"`, include `adx`, `instrument_type`, 30-day ADV fields, and preserve public enrichment fields (`sector`, `analyst_buy`, `analyst_hold`, `analyst_sell`, `avg_target`, `upside_pct`) from tvscreener when available
  - `adv_krw_min` uses TradingView 30-day average volume multiplied by price; responses set `meta.adv_window_days = 30` when this filter is requested
  - Legacy yfinance fallback cannot compute `adv_krw_min`; it returns a warning and skips only that filter
  - Post-screen enrichment skips per-row Finnhub/yfinance fan-out when those public fields are already populated; missing fields fall back to lightweight yfinance/Finnhub enrichment
  - Unsupported or unverified tvscreener request-critical capabilities fall back to the legacy yfinance path
  - Legacy yfinance maps: `min_market_cap` → `intradaymarketcap`, `max_per` → `peratio.lasttwelvemonths`, `min_dividend_yield` → `forward_dividend_yield`
  - Legacy yfinance sort maps: `volume` → `dayvolume`, `market_cap` → `intradaymarketcap`, `change_rate` → `percentchange`
  - Legacy yfinance screen enrichment reuses a request-scoped session for repeated analyst-target lookups
  - Yahoo OHLCV (`day/week/month`) requests use Redis closed-candle cache at the service boundary
  - Closed-bucket cutoff uses NYSE session close via `exchange_calendars` (`XNYS`), including DST/holidays/early close

- **Crypto market**:
  - Default success path uses tvscreener `CryptoScreener` filtered by `EXCHANGE == "UPBIT"`
  - Default sort remains `sort_by="rsi"`, `sort_order="asc"`; a requested crypto `sort_by="rsi", sort_order="desc"` is coerced to ascending and reported in `warnings` plus `filters_applied.sort_order`
  - `trade_amount_24h` maps to TradingView `CryptoField.VALUE_TRADED` and keeps the public KRW traded-value contract
  - `volume_24h` keeps the legacy Upbit 24h volume meaning (`acc_trade_volume_24h`); `VOLUME_24H_IN_USD` is never used as a public replacement for either `trade_amount_24h` or `volume_24h`
  - Result symbols are normalized back to Upbit format such as `KRW-BTC`
  - Successful tvscreener responses still restore legacy public crypto fields including `rsi_bucket`, `market_cap_rank`, `market_warning`, `volume_ratio`, `candle_type`, `plus_di`, and `minus_di`
  - Warning/crash metadata (`filtered_by_warning`, `filtered_by_crash`) and CoinGecko cache metadata are preserved on the tvscreener success path
  - Stop-loss cooldown filter: symbols in an 8-day stop-loss cooldown window (after a stop-loss sell) are excluded from results; count available in `meta.filtered_by_stop_loss_cooldown`
  - `sort_by="volume"` is not supported for crypto and returns an error
  - Crypto response payload does not include `volume`; use `trade_amount_24h`
  - `market_cap` sorting is supported; public `market_cap` prefers CoinGecko cache values and falls back to TradingView `MARKET_CAP`, and final ordering uses that public value without silently falling back to `trade_amount_24h`
  - `max_per`, `min_dividend_yield`, `sort_by="dividend_yield"` not supported - returns error
  - `min_market_cap` filter is not supported; crypto responses return a warning that it was ignored
  - `sector`, `exclude_sectors`, `instrument_types`, `adv_krw_min`, `market_cap_min_krw`, `market_cap_max_krw`, and `min_analyst_buy` filters are not supported for crypto - returns error

Filter compatibility and error semantics:
- `sector` filter: Supported for KR/US stocks only. Returns error for crypto or KR ETF/ETN requests
- `exclude_sectors`: Supported for KR/US stocks only. Cannot overlap with `sector`
- `instrument_types`: Supported for KR/US only. `asset_type="etf"` conflicts with `instrument_types=["common"]`
- `adv_krw_min`, `market_cap_min_krw`, `market_cap_max_krw`: Non-negative integers only. `market_cap_min_krw` must be less than or equal to `market_cap_max_krw`
- `min_analyst_buy` filter: Supported for KR/US stocks only (not ETF/ETN). Returns error for crypto or non-stock asset types
- `min_dividend` / `min_dividend_yield`: These are aliases. Accepts decimal (0.03) or percentage (3.0) formats. If both are specified with different values, returns error. Not supported for crypto
- `category` and `sector`: These are aliases for US market. If both are specified with different values, returns error
  - `min_market_cap` filter is not supported; crypto responses return a warning that it was ignored

#### Crypto Composite Score Formula (`recommend_stocks`)

Crypto market uses a dedicated composite score formula instead of strategy-weighted scoring:

```
Total Score = (100 - RSI) * 0.4 + (Vol_Score * Candle_Coef) * 0.3 + Trend_Score * 0.3
```

**Components:**
- **RSI Score** (40%): `100 - RSI` - Lower RSI (oversold) gives higher score
- **Volume Score** (30%): `min(vol_ratio * 33.3, 100)` where `vol_ratio = today_volume / avg_volume_20d`
- **Trend Score** (30%): Based on ADX/DI indicators
  - `plus_di > minus_di` → 90 (uptrend)
  - `adx < 35` → 60 (weak trend)
  - `35 <= adx <= 50` → 30 (moderate trend)
  - `adx > 50` → 10 (strong trend, possibly exhausted)

**Candle Coefficient** (applied to volume score):
- Uses completed candle (index -2, fallback to -1)
- `total_range == 0` → coef=0.5, type=flat
- Bullish (close > open) → coef=1.0, type=bullish
- Lower shadow > body*2 → coef=0.8, type=hammer
- Body > range*0.7 and bearish → coef=0.0, type=bearish_strong
- Other bearish → coef=0.5, type=bearish_normal

**Default values for missing data:**
- RSI missing → rsi_score = 50
- ADX/DI missing → trend_score = 30 (conservative)
- Volume missing → vol_score = 0
- Final score is clamped to 0-100

**Crypto recommend_stocks behavior:**
- Top 30 candidates pre-filtered by 24h traded value (`trade_amount`)
- Enriched with composite metrics (RSI, ADX/DI, volume ratio, candle type)
- Sorted by composite score (descending)
- Equal-weight budget allocation
- `score` field is always numeric (0-100)
- Timeout/429 errors return partial results with warnings instead of failing

Advanced filters subset behavior (KR/US):
- **Note**: `min_market_cap` is NOT an advanced filter for KR/US - it uses already available KRX/yfinance fields and does not trigger extra fetches.
- Advanced filters (PER, dividend yield, RSI) require external enrichment in KR market.
- KR/US RSI enrichment subset limit: `min(len(candidates), limit*3, 150)`.
- Parallel fetch with `asyncio.Semaphore(10)`.
- Timeout: 30 seconds.
- Individual failures don't stop overall operation.

Crypto enrichment behavior:
- Uses a dedicated crypto composite enrichment subset: `min(max(limit*3, 30), 60)`.
- `min_market_cap` is not applied as a filter in crypto; it is returned as a warning only.

Response format:
```json
{
  "results": [
    {
      "code": "005930",
      "name": "삼성전자",
      "close": 80000.0,
      "change_rate": 0.05,
      "volume": 10000000,
      "market_cap": 480000000000000,
      "per": 15.0,
      "dividend_yield": 0.03,
      "rsi": 45.5,
      "adx": 23.1,
      "sector": "Technology",  // Industry sector (can be null for some stocks)
      "analyst_buy": 15,  // Number of analyst buy ratings (default: 0)
      "analyst_hold": 3,  // Number of analyst hold ratings (default: 0)
      "analyst_sell": 2,  // Number of analyst sell ratings (default: 0)
      "avg_target": 85000.0,  // Average analyst target price (can be null)
      "upside_pct": 6.25,  // Upside percentage based on analyst targets (can be null)
      "market": "kr"
      "market": "kr"
    }
  ],
  "total_count": 2400,  // Total stocks that passed all filters (before sort/limit). If data source provides total, uses that; otherwise uses fetched candidates count.
  "returned_count": 20,  // Actual number of results returned (after limit)
  "filters_applied": {
    "market": "kr",
    "asset_type": "stock",
    "sector": "Technology",  // Applied sector filter (if specified)
    "min_market_cap": 100000,
    "max_per": 20,
    "min_dividend_yield": 0.03,
    "min_dividend_yield_input": 3.0,
    "min_dividend_yield_normalized": 0.03,
    "min_dividend_input": 3.0,  // Original min_dividend value if specified
    "min_analyst_buy": 5,  // Applied minimum analyst buy count filter
    "max_rsi": 70
    "asset_type": "stock",
    "min_market_cap": 100000,
    "max_per": 20,
    "min_dividend_yield": 0.03,
    "min_dividend_yield_input": 3.0,
    "min_dividend_yield_normalized": 0.03,
    "max_rsi": 70
  },
  "meta": {
    "source": "tvscreener",
    "rsi_enrichment": {
      "attempted": 0,
      "succeeded": 0,
      "failed": 0,
      "rate_limited": 0,
      "timeout": 0,
      "error_samples": []
    }
  },
  "timestamp": "2026-02-10T14:20:59.123456"
}
```

### `recommend_stocks` spec (DEPRECATED — registry-hidden, ROB-359)
> **This tool is no longer registered on the MCP surface.** It is parked, not
> deleted: `recommend_stocks_impl` remains in `analysis_tool_handlers` for a
> future narrow `build_buy_plan` tool. The spec below documents the retained
> implementation only. For candidate discovery use `screen_stocks`.

Parameters:
- `budget`: Total budget to allocate (required, must be positive)
- `market`: Market to screen - "kr", "us", "crypto" (default: "kr")
- `strategy`: Scoring strategy - "balanced", "growth", "value", "dividend", "momentum" (default: "balanced")
- `exclude_symbols`: List of symbols to exclude from recommendations (optional)
- `sectors`: List of sectors/categories to filter (uses first value only)
- `max_positions`: Maximum number of positions to recommend 1-20 (default: 5)

> Breaking change: `account` parameter is removed from `recommend_stocks`.

Strategy descriptions:
- **balanced**: 균형 잡힌 포트폴리오. RSI, 밸류에이션, 모멘텀, 배당을 균등하게 고려
- **growth**: 성장주 중심. 높은 모멘텀과 거래량 가중
- **value**: 가치투자 중심. 낮은 PER/PBR, 적정 RSI 가중 (max_per=20, max_pbr=1.5, min_market_cap=300억)
- **dividend**: 배당주 중심. 높은 배당수익률 가중 (min_dividend_yield=1.5%, min_market_cap=300억)
- **momentum**: 모멘텀 중심. 강한 상승 모멘텀과 거래량 가중

Strategy default thresholds (KR market):
- **value**: `max_per=20`, `max_pbr=1.5`, `min_market_cap=300` (억원)
- **dividend**: `min_dividend_yield=1.5` (percent), `min_market_cap=300` (억원)

2-stage relaxation (value/dividend only):
- When strict screening yields fewer candidates than `max_positions`, a fallback screening is triggered with relaxed thresholds:
  - **value**: `max_per=25`, `max_pbr=2.0`, `min_market_cap=200`
  - **dividend**: `min_dividend_yield=1.0`, `min_market_cap=200`
- Fallback candidates are added to fill remaining positions (deduped by symbol)
- For value strategy: candidates with missing PER/PBR receive score penalties (-12 for PER, -8 for PBR)
- For dividend strategy: candidates with missing or zero dividend_yield are excluded from fallback
- The `fallback_applied` field indicates whether fallback was used

Scoring weight factors:
- `rsi_weight`: RSI 기반 기술적 과매수/과매도 점수 비중
- `valuation_weight`: PER/PBR 기반 밸류에이션 점수 비중
- `momentum_weight`: 등락률 기반 모멘텀 점수 비중
- `volume_weight`: 거래량 기반 유동성 점수 비중
- `dividend_weight`: 배당수익률 기반 인컴 점수 비중

Behavior:
- Invalid `market` values raise `ValueError` (no silent fallback)
- Strategy-specific `screen_params` are applied per market and unsupported filters are ignored with warnings
- KR screens candidates using internal screener (max 100 candidates)
- Crypto prefilters top 30 candidates by 24h traded value, then enriches with RSI/composite metrics
- US uses `get_top_stocks(market="us", ranking_type="volume")` for candidate collection (max 50 candidates)
- Dividend threshold input is normalized as percent when `>= 1` (e.g., `1.0 -> 0.01`, `3.0 -> 0.03`)
- Excludes user holdings from all accounts (internal `account=None` query)
- Applies strategy-weighted composite scoring for KR/US (0-100); crypto uses dedicated composite score
- Sorts by score and allocates budget with integer quantities
- Remaining budget is added to top recommendation if possible

Response format:
```json
{
  "recommendations": [
    {
      "symbol": "005930",
      "name": "삼성전자",
      "price": 80000.0,
      "quantity": 10,
      "amount": 800000.0,
      "score": 75.5,
      "reason": "[balanced] RSI 45.0 (저평가 구간) | PER 12.0 (적정)",
      "rsi": 45.0,
      "per": 12.0,
      "change_rate": 2.5
    }
  ],
  "total_amount": 950000.0,
  "remaining_budget": 50000.0,
  "strategy": "balanced",
  "strategy_description": "균형 잡힌 포트폴리오 구성을 위한 전략...",
  "candidates_screened": 100,
  "diagnostics": {
    "raw_candidates": 100,
    "post_filter_candidates": 95,
    "per_none_count": 5,
    "pbr_none_count": 3,
    "dividend_none_count": 10,
    "dividend_zero_count": 2,
    "strict_candidates": 80,
    "fallback_candidates_added": 0,
    "fallback_applied": false,
    "active_thresholds": {
      "min_market_cap": 500,
      "max_per": null,
      "max_pbr": null,
      "min_dividend_yield": null
    }
  },
  "fallback_applied": false,
  "warnings": [],
  "timestamp": "2026-02-13T02:11:52.950534+00:00"
}
```

Crypto recommendation example (`market="crypto"`):
```json
{
  "recommendations": [
    {
      "symbol": "KRW-BTC",
      "name": "비트코인",
      "price": 142000000.0,
      "quantity": 1,
      "amount": 142000000.0,
      "score": 78.4,
      "reason": "Composite Score 78.4 | RSI 39.2(저평가) | 캔들 bullish | 거래량 1.3배",
      "rsi": 39.2,
      "per": null,
      "change_rate": 1.8,
      "volume_24h": 12543.21,
      "volume_ratio": 1.32,
      "candle_type": "bullish",
      "adx": 27.41,
      "plus_di": 31.52,
      "minus_di": 18.07
    }
  ],
  "total_amount": 142000000.0,
  "remaining_budget": 8000000.0,
  "strategy": "balanced",
  "warnings": [],
  "timestamp": "2026-02-15T00:00:00+00:00"
}
```

Diagnostics fields:
- `raw_candidates`: Number of candidates from screener
- `post_filter_candidates`: After normalization
- `per_none_count`: Candidates with missing PER
- `pbr_none_count`: Candidates with missing PBR
- `dividend_none_count`: Candidates with missing dividend_yield
- `dividend_zero_count`: Candidates with zero dividend_yield
- `strict_candidates`: After exclusion/dedup
- `fallback_candidates_added`: Additional candidates from 2-stage relaxation
- `fallback_applied`: Whether fallback screening was triggered
- `active_thresholds`: The strict stage thresholds used
- `fallback_thresholds`: (optional) Fallback thresholds if fallback was applied

Error response format (unexpected internal failure):
```json
{
  "error": "recommend_stocks failed: RuntimeError",
  "source": "recommend_stocks",
  "query": "market=kr,strategy=balanced,budget=5000000,max_positions=5",
  "details": "Traceback (most recent call last): ..."
}
```

### `get_cash_balance` spec
Parameters:
- `account`: optional operational account filter (`toss`, `paper`, or
  `paper:<name>`). `upbit` and KIS selectors fail closed.
- `account_mode`: defaults to `toss_live`; `kis_live`/`kis_mock` reject with
  `provider kis is not operational`

Supported contracts:
- **Toss (`account="toss"`)**
  - Returns supported KRW/USD cash and orderable evidence from Toss.
  - An explicit Toss read failure fails closed rather than returning a
    synthetic fallback.
- **PAPER (`account="paper"` or `paper:<name>`)**
  - Returns cash from the selected DB-backed paper account.

Response shape:
- `accounts`: per-account cash entries
- `summary.total_krw`: sum of KRW `balance` fields
- `summary.total_usd`: sum of USD `balance` fields
- `errors`: per-source partial failures in non-strict mode

### `get_available_capital` spec
Parameters:
- `account`: optional operational account filter (`toss`, `paper`, or
  `paper:<name>`)
- `include_manual`: whether to include owner-scoped manual cash (default: `true`)

Behavior:
- Aggregates orderable cash from operational Toss or DB-backed PAPER accounts.
- Converts supported USD orderable amounts to KRW equivalents using the current
  exchange rate.
- Marks manual cash as stale when older than 3 days.
- Upbit and KIS account selectors are non-operational and never trigger
  provider I/O.

### `get_holdings` spec
Parameters:
- `account`: optional persisted account filter (`upbit`, `toss`,
  `samsung_pension`, `isa`, `paper`, or `paper:<name>`)
- `market`: optional market filter (`kr`, `us`, `crypto`)
- `include_current_price`: if `True`, resolves supported current prices and PnL
- `minimum_value`: optional numeric threshold; when omitted, KRW/crypto uses
  5000 and USD uses 10

Filtering rules:
- Toss is the KR/US live account source. Persisted Upbit-provenance holdings
  remain readable, but no private Upbit account provider or order routing is
  available. KIS selectors remain historical and fail closed for operational
  holdings collection.
- If `include_current_price=False`, `minimum_value` filtering is skipped.
- Current equity prices use the Toss market-data boundary. Provider failure is
  explicit and no KIS/Yahoo value is synthesized as Toss evidence.
- Manual holdings remain owner-scoped and do not acquire broker sellability.
- Persisted crypto holdings may refresh prices through the unsigned public
  Upbit batch ticker endpoint (`/v1/ticker?markets=...`); no credential is used.
- During persisted crypto holdings name resolution, coins that raise `UpbitSymbolNotRegisteredError` or `UpbitSymbolInactiveError` are silently skipped (not added to `errors`).
- Before batch ticker request, tradable markets are loaded from `upbit_symbol_universe` and only valid holdings symbols are included in the batch
- Non-tradable symbols (delisted/unsupported) are excluded from ticker request and treated as 0 value for `minimum_value` filtering (counted in `filtered_count`)
- Value is primarily based on `evaluation_amount`
- If current price lookup fails (`current_price=null`), value is treated as `0` for minimum filtering

Response contract additions:
- `filtered_count`: number of positions excluded by `minimum_value` filter
- `filter_reason`: filter status string, e.g. `minimum_value < 1000` or `equity_kr < 5000, equity_us < 10, crypto < 5000`
- `errors`: includes per-symbol price lookup failures for holdings price refresh (example fields: `source`, `market`, `symbol`, `stage`, `error`)
- Refreshed US positions expose `price_source`, nullable `price_asof`,
  `data_state`, and `profit_rate_price_source`; provider metadata such as
  `session`, `venue`, and `delayed` is included when available. If the Toss
  refresh fails, retained valuation evidence is marked `data_state="stale"`
  with `data_state_reason="live_price_refresh_failed"` rather than silently
  reading as current.
- `filters.minimum_value`: when `minimum_value=None` in the request, this field contains the per-currency threshold dict that was applied
- When `TOSS_API_ENABLED=true`, Toss Open API holdings are emitted with `broker="toss"`, `source="toss_api"`. `order_routable` (and `get_cash_balance` `orderable`, `/invest` home `isTradeable`) remain gated on `TOSS_LIVE_ORDER_MUTATIONS_ENABLED` (ROB-549). General holdings/home/briefing reads omit `sellable_quantity` or return `sellableQuantity=null`; `need_sellable=false` paths skip Toss sellable reads entirely, and they never fan out to Toss `/api/v1/sellable-quantity`. Toss live order tools revalidate sellability directly at the broker immediately before a live sell mutation. Every live sell placement requires an explicit, finite, positive `quantity`: an orderAmount-only live sell is rejected with `error_code="sell_quantity_required"` before any broker mutation, because Toss's orderAmount shape carries no broker-authoritative quantity and one is never synthesized from holdings, snapshots, or sellable caches. Successful live sell place/modify responses publish the authorizing evidence as `fresh_sellable_quantity` and `sellable_quantity_source="toss_broker_preflight"`.
- The composed persisted-Upbit/manual/Toss portfolio read model used by general holdings, home, briefing, and calendar held-key reads uses a short-lived process-shared Redis snapshot with a Redis distributed singleflight (ROB-1310). A live owner renews its lock lease; corrupt entries re-enter the same singleflight recovery path, while Redis outages/owner death retain bounded direct read-only recovery and never fabricate sellable data. Calendar held-key reads never fall back to full live readers: a cold/invalid snapshot returns an explicit `portfolio_snapshot_unavailable` 503 with availability metadata.
- The Home-to-MCP snapshot projection preserves the read contract: persisted Upbit/Toss/manual account IDs use canonical groups, crypto symbols use the `KRW-` market prefix, US P/L remains in native currency, and Home ratio fields are exposed as percentage points. Snapshot serialization excludes sellable and pending-sell quantities.
- When Toss API holdings succeed, duplicate Toss `manual_holdings` rows for the same market/symbol are hidden from normal output.
- When Toss API holdings fail, existing Toss `manual_holdings` rows remain visible as fallback and the response includes a partial `source="toss_api"` error.

Market routing:
- `market` can override routing: `crypto|upbit`, `kr|toss|krx|kospi|kosdaq`, `us|toss|nasdaq|nyse`
- If `market` is omitted, routing is heuristic: KRW-/USDT- prefix -> crypto, 6-digit code -> KR equity, otherwise -> US equity
- Crypto symbols must include `KRW-` or `USDT-` prefix

### `get_portfolio_allocation` spec

Parameters:
- `account`: optional persisted account filter matching `get_holdings` and `get_cash_balance` (`upbit`, `toss`, `samsung_pension`, `isa`, `paper`, `paper:<name>`)
- `market`: optional holdings market filter (`kr`, `us`, `crypto`); cash is still included when `include_cash=true` unless `account` excludes the cash account
- `include_cash`: include cash balances in the allocation denominator, default `true`
- `include_positions`: include per-position normalized rows, default `false`
- `target_weights`: optional mapping from asset class to target percent; when omitted, no over/underweight flags are emitted
- `drift_threshold_pct`: threshold for `overweight` / `underweight` labels when `target_weights` is provided, default `5.0`
- `account_mode`: same routing selector as `get_holdings` (`db_simulated`, `toss_live`); KIS modes reject as non-operational

Behavior:
- Read-only only. The tool performs no order preview, order placement, mutation, reconciliation, or live approval action.
- Converts USD holdings and USD cash to KRW using the same exchange-rate service used by portfolio cash tools.
- Aggregates direct US equity as `us_equity`, KR equity as `kr_equity`,
  persisted Upbit-provenance holdings as `crypto`, and cash as `cash`.
- Looks through KR-listed ETFs when KRX ETF metadata is available. KR ETFs classified as `미국주식` by `app.services.krx.classify_etf_category()` are counted as effective `us_equity`, while their surface account remains KR/Toss.
- Non-US foreign, commodity, bond, and unclear ETF categories are counted as `other` rather than Korean equity.
- If KRX ETF metadata lookup fails, the tool records a degraded `krx_etf` error and keeps KR ETF positions in their surface `kr_equity` bucket.
- Positions whose valuation is unavailable are excluded from the denominator and listed in `warnings` with `reason="position_value_unavailable"`.

Response shape:
- `summary`: KRW total, invested value, cash value, valued/unvalued position counts
- `asset_classes`: value, weight, direct/look-through split, target/drift fields, and optional weight status
- `accounts`: account-level KRW roll-up with asset-class children and `profit_loss_krw` (cash sub-accounts carry 0)
- `lookthrough`: KR ETF rows whose effective exposure differs from surface exposure
- `positions`: returned only when `include_positions=true`
- `cash`: normalized cash rows when `include_cash=true`
- `errors`: broker, cash, exchange-rate, or KRX ETF partial failures
- `warnings`: non-fatal valuation omissions

### `get_trading_policy` spec

- `get_trading_policy(market, lane)`
  - Query trading policy judgment thresholds and lane-scoped decision rules.
  - Read-only, single source `config/trading_policy.yaml`, operator-PR-edited (no write tool).
  - Args `market ∈ {kr,us,crypto}` × `lane ∈ {buy,sell,discovery}`.
  - An unknown key maps to `success=false, error=unknown_key`.
  - `market_rules` contains market-specific advisory judgment rules filtered by
    lane. Crypto includes the recovery gate, support/resistance source priority,
    and no-chasing criteria. A `null` threshold is intentional and callers must
    not replace it with an inferred number. These rules do not replace
    code-owned fail-closed order guards.
  - Success returns
    `{market, lane, version, content_hash, thresholds, decision_rules, market_rules}`.
    `decision_rules` is lane-filtered and empty when no rule applies. For sell,
    `decision_rules["sell.trim_preplace"]` encodes the ROB-751 resistance-near
    vs upside-rich tie-break: RSI-confirmed resistance or ultra-near resistance
    permits only a small pre-placed trim ladder; RSI-neutral 2-6% resistance is
    a watch; `sell.upside_place_max_pct` limits size rather than blocking
    pre-placement eligibility.
    `decision_rules["sell.single_share_exit"]` is intentionally visible only
    for KR sell, but remains shadow metadata: `activation_state=shadow` and
    `proposal_enabled=false` mean that neither `get_trading_policy` nor
    `route_request` may interpret its candidate action as permission to create,
    approve, or execute a proposal. Capability labels in its snapshot API are
    descriptive, not authentication; any future live composition must pin the
    trusted read-adapter provenance separately.
  - **Version-stamping contract**: consumers cite `{version, content_hash}` (from `get_trading_policy` or the `policy_version` field of `get_operating_briefing`) in `report_item.evidence_snapshot`, `trade_retrospectives`, and forecast records so the judging criteria are recoverable.
  - The buy-preview `sector_concentration` field is **fail-open** advisory (never blocks).

### route_request — advisory lane router (ROB-649)

`route_request(intent, market, purpose=None)` maps a coarse intent
(`buy_analysis`/`profit_taking`/`discovery`/`market_brief`) to the standard tool
sequence, advisory allowed/blocked tools, `get_trading_policy` thresholds +
version stamp, and hard constraints for that lane. Deterministic; registered on
every profile; read-only.

**Divergence from tradingcodex:** the original has no route MCP tool — it
injects lane guidance via a hook and maps lane→role→tool indirectly. auto_trader
exposes a **direct lane→tool advisory** tool with **no enforcement**. Blocking
middleware (mutation tools only, reads unrestricted, caller-header-keyed because
MCP session state resets on reconnect — ROB-469) is a separate follow-up issue.
In particular, the contract below cannot physically prevent an enabled
auto-approval path.

**ROB-1239:** the canonical statement of what a `blocked_actions` verdict
does and does not mean is the `route_request` tool `description=` string in
`app/mcp_server/tooling/route_request_registration.py`, not this section.

Lane definitions come from the machine-readable `lanes:` blocks in
`docs/playbooks/trading-decision-playbook.md`; `route_request_lanes.LANE_SEQUENCES`
is kept in exact order by `tests/test_route_request_registry_diff.py`. Every
proposal-enabled DEFAULT tool must be classified into the read/advisory or
explicit mutation taxonomy or CI fails.

**Buy/sell proposal contract (ROB-1045):**

- `order_proposal_create` is the only generic order-intent step in the buy/sell
  `standard_tool_sequence`. `support_reserve_net_consume` is a separate,
  non-sequenced conditional helper allowed only in the buy lane; it requires a
  complete evidence packet and creates through the atomic watcher-scope seam.
  Registered direct broker place/cancel/modify tools are excluded from
  `allowed_tools` and included in `blocked_actions`.
- The route contract requires a human Telegram approval click. Fresh broker
  preview/revalidation and submit are owned by the proposal approval subsystem;
  accepted/resting is not a fill, and broker-evidence reconciliation remains
  required. Registered reconcile tools are conditional helpers rather than an
  ordered step because `route_request` has no broker/account-mode input.
- `route_contract` is machine-readable and carries
  `version="proposal-led-v1"`, `state`, `execution_mode`, `execution_ready`,
  `proposal_tool`, `approval_channel`, `human_approval_required`,
  `preview_owner`, `reconcile_requirement`, `required_tools`, and
  `missing_required_tools`.
- `execution_ready=true` means only that the required route tool is present in
  the live MCP registry. It does not assert Telegram publication, configuration,
  or approval-window readiness. The actual `order_proposal_create`
  `approval_dispatch` result remains authoritative; see
  [Order proposal approval tools](#order-proposal-approval-tools-rob-816).
- If the live registry is valid but `order_proposal_create` is absent, buy/sell
  return `success=false`, `error="required_route_tool_unavailable"`, and
  `execution_ready=false`, with no direct-place fallback.
- If registry introspection is missing, raises, is malformed, or is empty, the
  route returns `success=false`, `error="registry_introspection_unavailable"`,
  empty sequence/allowed lists, and the static direct-mutation deny list with
  `blocked_actions_basis="static_fail_closed"`. It never substitutes
  `ALL_KNOWN_TOOLS`.

Any non-blank `purpose` is rejected with `success=false` and
`error="unknown_purpose"`. No `account_cleanup` route or separate maintenance
submit sequence exists.

**Discovery non-regression:** discovery remains outside ROB-1045 and may retain
registered generic/Toss order steps. Route inclusion does not widen provider
admission: crypto execution still fails closed at the order tool boundary, and
bootstrap remains read-only.


### User Settings Tools

- `get_user_setting(key)` - Get a user setting value by key. Returns the JSON value or None if not found.
- `set_user_setting(key, value)` - Set a user setting value by key (upsert). Returns the serialized setting with key, value, and updated_at.

These tools provide a generic key-value storage for user preferences and settings. Values are stored as JSON and can be any valid JSON-serializable data structure.

Common settings:
- `manual_cash`: Stores manually-managed cash amounts (e.g., `{"amount": 15000000}`) for accounts not backed by APIs (Toss, etc.)
- `account_costs`: Stores broker fee/cost profiles and thresholds used for routing suggestions.

### `account_costs` user setting

`set_user_setting(key="account_costs", value={...})` stores operator-maintained
broker cost metadata used by `suggest_order_account`, `get_available_capital`,
and `get_operating_briefing`.

Required shape:

```json
{
  "version": 1,
  "routing": {
    "position_consolidation_threshold_bps": {"kr": 25, "us": 40}
  },
  "accounts": {
    "kis_domestic": {
      "broker": "kis",
      "markets": {"kr": {"commission_bps": 14.7, "fx_spread_bps": 0}}
    },
    "kis_overseas": {
      "broker": "kis",
      "markets": {"us": {"commission_bps": 25, "fx_spread_bps": 20}}
    },
    "toss": {
      "broker": "toss",
      "limits": {"max_order_notional_krw": 1000000},
      "markets": {
        "kr": {"commission_bps": 0, "fx_spread_bps": 0},
        "us": {"commission_bps": 10, "fx_spread_bps": 1.7}
      }
    }
  }
}
```

Values are basis points. `25` means 0.25%. If the setting is missing or invalid,
the system uses default seed values and marks the result `review_required`.

### `update_manual_holdings` spec

Parameters:
- `holdings`: List of holding objects to upsert/remove (required)
- `broker`: Broker identifier - `"toss"`, `"samsung"`, `"kis"` (required)
- `account_name`: Account name - `"기본 계좌"`, `"퇴직연금"`, `"ISA"` (default: `"기본 계좌"`)
- `dry_run`: Preview mode without DB changes (default: `true`)

Holding object fields:
- `symbol`: Ticker/symbol (e.g., `"AAPL"`, `"005930"`, `"KRW-BTC"`). Takes precedence over `stock_name`.
- `stock_name`: Company name or alias (e.g., `"삼성전자"`, `"애플"`). Used for symbol resolution when `symbol` is not provided.
- `quantity`: Number of shares/coins (required for upsert)
- `avg_buy_price`: Average purchase price (optional). If not provided, calculated from `eval_amount`, `profit_loss`, and `quantity`.
- `eval_amount`: Current evaluation amount (optional, used for avg_price calculation)
- `profit_loss`: Unrealized profit/loss (optional, used for avg_price calculation)
- `profit_rate`: Profit rate percentage (optional, informational)
- `market_section`: Market type - `"kr"`, `"us"`, `"crypto"` (required)
- `action`: Operation - `"upsert"` or `"remove"` (default: `"upsert"`)

Validation rules:
- **US ticker resolution**: US holdings must use a real ticker or a pre-registered alias in `stock_alias` table.
- **US name-like input**: If a US name-like string fails lookup, the tool raises an error asking to add `stock_alias` mapping or supply the ticker directly.
- **US avg_buy_price**: Must be in USD. Values above `1000` are rejected with error: `"USD 단위로 입력해주세요 (현재 값: {value}, KRW로 의심됩니다)"`.
- **Quantity zero/negative**: `qty <= 0` payloads are treated as delete/cleanup intent:
  - If a matching holding exists, it is removed (same as `action="remove"`)
  - If no matching holding exists, a warning is generated
- **dry_run behavior**: When `dry_run=True`, no DB mutations occur. The response still includes `added_count`, `updated_count`, `removed_count`, `unchanged_count`, and `diff` so callers can validate the planned changes before execution. Dry-run diff actions are `would_add`, `would_update`, `would_remove`, and `unchanged`; live execution actions remain `added`, `updated`, and `removed`.

Response format:
```json
{
  "success": true,
  "dry_run": false,
  "message": "Holdings updated successfully",
  "broker": "samsung",
  "account_name": "기본 계좌",
  "parsed_count": 3,
  "holdings": [...],
  "warnings": [],
  "added_count": 1,
  "updated_count": 1,
  "removed_count": 1,
  "unchanged_count": 0,
  "diff": [...]
}
```

Dry-run remove preview example:
```json
{
  "success": true,
  "dry_run": true,
  "message": "Preview only (set dry_run=False to update DB)",
  "broker": "toss",
  "account_name": "기본 계좌",
  "parsed_count": 0,
  "holdings": [],
  "warnings": [],
  "added_count": 0,
  "updated_count": 0,
  "removed_count": 2,
  "unchanged_count": 0,
  "diff": [
    {"action": "would_remove", "ticker": "IONQ", "market_type": "US"},
    {"action": "would_remove", "ticker": "TSM", "market_type": "US"}
  ]
}
```

Error response format:
```json
{
  "success": false,
  "error": "USD 단위로 입력해주세요 (현재 값: 14966.0, KRW로 의심됩니다)"
}
```

### `get_user_setting` spec
Parameters:
- `key`: Setting key string (required)

Returns:
- The JSON value stored for the key, or `None` if the key doesn't exist

### `set_user_setting` spec
Parameters:
- `key`: Setting key string (required)
- `value`: Any JSON-serializable value (required)

Returns:
```json
{
  "key": "manual_cash",
  "value": {"amount": 15000000},
  "updated_at": "2026-04-01T08:00:00+00:00"
}
```

Behavior:
- Creates the setting if it doesn't exist, updates it if it does (upsert)
- `updated_at` is automatically set to the current timestamp
- The (user_id, key) pair is unique; attempting to create a duplicate key for the same user will update the existing entry

## Caller Identity Header (required)

All MCP callers (Scout, Trader, CIO bridges, and any future client) MUST send
`x-paperclip-agent-id: <calling agent id>` on every `tools/call` request. The
header is a canonical legacy Paperclip-named compatibility surface and MUST NOT
be renamed; its value is the current caller agent id, not the target trader
agent id.

- The `CallerIdentityMiddleware` added in ROB-214 (ST-3.1) reads this header,
  stores it in a request-scoped contextvar, and records the extraction source
  (`http_header` | `env_fallback` | `none`) on each call.
- Caller-identity-gated tools (e.g. `place_order(..., defensive_trim=True)`
  after ST-3.2) reject calls where the contextvar is `None`, so a missing
  header in a production path is an outage, not a soft warning.
- Local dev / stdio transports that cannot send HTTP headers may export
  `MCP_CALLER_AGENT_ID` as an env fallback. This is a dev convenience only —
  production callers must send the header explicitly. `MCP_CALLER_AGENT_ID`
  MUST NOT be set in production HTTP deployments because it re-opens a caller
  spoofing vector for requests that omit `x-paperclip-agent-id`.

### Scout / Trader curl bridge

When an agent runs under a harness that does not register the auto_trader MCP
server in-process (current state for Scout and Trader on `claude_local`),
they use a JSON-RPC curl bridge at `/tmp/mcp_call.sh`. The canonical template
lives at `scripts/templates/mcp_call.sh.tmpl`; both agents MUST regenerate
their local `/tmp/mcp_call.sh` from that template so the header is present.

```bash
# From the repo root, per operator host/session:
export MCP_ENDPOINT="http://127.0.0.1:8765/mcp"
export MCP_AUTH_TOKEN="<value from env.MCP_AUTH_TOKEN>"
export MCP_SESSION_ID="<MCP session id>"
export PAPERCLIP_AGENT_ID="<calling agent id>"
envsubst '$MCP_ENDPOINT $MCP_AUTH_TOKEN $MCP_SESSION_ID $PAPERCLIP_AGENT_ID' \
  < scripts/templates/mcp_call.sh.tmpl > /tmp/mcp_call.sh
# 0700 — owner-only. The rendered script bakes MCP_AUTH_TOKEN in plaintext,
# so group/other read bits must be stripped.
chmod 700 /tmp/mcp_call.sh

# Smoke test — should return a tool payload, not 401/403/reject:
/tmp/mcp_call.sh get_quote '{"symbol":"005930","market":"kr"}'
```

The rendered bridge intentionally calls curl with `-N --max-time 15` and sends
`Connection: close`. It only consumes the first SSE `data:` line, so no-buffer
mode and the timeout keep the helper from holding a completed agent run open
if the server keeps the stream alive.

If the Trader adapter is later migrated to an in-process MCP client (for
example a Claude Code `.mcp.json` entry or an SDK-level `default_headers`
config), that client must also set `x-paperclip-agent-id`; do not rely on
the shell bridge as the long-term header injection point.

## Run (docker-compose.prod)
Environment variables:
- `MCP_TYPE` : `streamable-http` (default) | `sse` | `stdio`
- `MCP_HOST` : `0.0.0.0`
- `MCP_PORT` : `8765`
- `MCP_PATH` : `/mcp`
- `MCP_GRACEFUL_SHUTDOWN_TIMEOUT` : `10` (seconds, HTTP transports only: `sse` / `streamable-http`)
- `MCP_USER_ID` : `1` (manual holdings 조회에 사용할 기본 사용자 ID)
- `MCP_CALLER_AGENT_ID` : DEV/stdio only — MUST NOT be set in production HTTP deployments (re-opens caller spoofing vector)


Example:
```bash
docker compose -f docker-compose.prod.yml up -d mcp
```

> Note: current prod compose uses `network_mode: host`, so port publishing is handled by the host network.

---

## MCP Profiles (ROB-56)

### Overview

The `MCP_PROFILE` env var selects which tool subset is registered at startup.

| Profile | Value | Order surface |
|---|---|---|
| Default | `default` (or unset) | Toss KR/US equity orders plus all side-effect-free research and read-only portfolio tools. Non-equity markets are rejected because PAPER and Toss are the only operational providers. |
| Crypto | `crypto` | Default research/read-only surface plus the generic order tools. Crypto *execution* is registered on no profile — no crypto broker is operational. |
| DB paper simulator | `db-paper` | Default research/read-only surface plus the internal DB paper simulator tools. |
| Shadow replay | `shadow-replay` | Frozen-context replay only: exactly `investment_report_get_hermes_context`, `get_trading_policy`, and `route_request`. No live-fetch, mutation, or order tool. |
| Analysis readonly | `analysis_readonly` | Codex/headless read/analysis allowlist only; no order, preview, reconcile, settings, or watch-mutation tool. |
| Account read | `account_read` | TradingCodex account-read allowlist: holdings, cash, and read-only order history only. |
| TradingCodex execution | `tradingcodex_execution` | Reviewed Toss/PAPER execution allowlist. Requires dedicated auth and approval-hash modes. |
| Watch repricing | `watch_repricing` | Proposal-only allowlist: a spawned repricing session can create a proposal and can reach no broker order tool. |


### Profile: `analysis_readonly` (ROB-745)

Use `MCP_PROFILE=analysis_readonly` for Codex/headless consumers that need market analysis tools but must not see the operator's full order-capable MCP surface.

Allowed tools:
- `get_operating_briefing`
- `route_request`
- `get_trading_policy`
- `get_market_index`
- `get_quote`
- `analyze_stock_batch`
- `get_support_resistance`
- `get_indicators`
- `screen_stocks`
- `screen_stocks_snapshot`
- `get_krx_session_health`
- `get_top_stocks`
- `get_news`
- `get_fx_rate`
- `get_holdings`
- `toss_get_positions`
- `analysis_artifact_save`
- `analysis_artifact_get`
- `analysis_bundle_get` (only when `ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED=true`)
- `forecast_save`
- `session_context_append`
- `session_context_get_recent`

Forbidden by physical non-registration:
- order placement, cancel, modify, history, reconcile, and preview tools
- Toss place/modify/cancel/history/orderable-cash/reconcile/preview
- manual holdings mutation
- user settings tools
- watch/admin/report-writing surfaces

Persistence tools on this profile require explicit provenance:
- pass `created_by="codex"` for `analysis_artifact_save`
- pass `created_by="codex"` in every `session_context_append` entry
- pass `created_by="codex"` to `forecast_save`

### Codex Config Example

Here is an example Codex config file to connect to the analysis-readonly MCP server with relaxed approval:

```toml
# ~/.codex/config.toml
# Relaxed approval is scoped to the analysis-readonly MCP server only.
[mcp_servers.auto_trader_analysis_readonly]
url = "http://127.0.0.1:8768/mcp"
bearer_token_env_var = "MCP_ANALYSIS_READONLY_AUTH_TOKEN"
default_tools_approval_mode = "auto"

http_headers = { "x-paperclip-agent-id" = "codex-analysis-readonly" }
```

### Profile: `account_read` (ROB-760)

Use `MCP_PROFILE=account_read` for TradingCodex or other account-sync consumers that need broker account reads without any trading or persistence surface.

Allowed tools:
- `get_holdings`
- `toss_get_positions`
- `get_cash_balance`
- `toss_get_orderable_cash`
- `get_order_history`
- `toss_get_order_history`

Forbidden by physical non-registration:
- order placement, cancel, modify, preview, and reconcile tools
- manual holdings mutation
- user settings tools
- watch/admin/report-writing surfaces
- analysis persistence and session context write/read tools

Authentication is mandatory for this profile. `MCP_PROFILE=account_read` fails at startup unless `MCP_AUTH_TOKEN` is non-empty. Deployment wrappers must source that value from `MCP_ACCOUNT_READ_AUTH_TOKEN`; do not fall back to the operator `MCP_AUTH_TOKEN`.

TradingCodex config example:

```toml
[mcp_servers.auto_trader_account_read]
url = "http://127.0.0.1:8769/mcp"
bearer_token_env_var = "MCP_ACCOUNT_READ_AUTH_TOKEN"
default_tools_approval_mode = "auto"

http_headers = { "x-paperclip-agent-id" = "tradingcodex-account-read" }
```

### Profile: `tradingcodex_execution` (ROB-768, ROB-778)

Use `MCP_PROFILE=tradingcodex_execution` for the reviewed TradingCodex BrokerAdapter surface. This profile is order-capable, but still allowlist-only and narrower than `default`.

Allowed read/advisory tools:
- `get_holdings`
- `toss_get_positions`
- `get_cash_balance`
- `toss_get_orderable_cash`
- `get_order_history`
- `toss_get_order_history`
- `get_fx_rate`
- `route_request`
- `get_trading_policy`
- `list_active_watches`
- `investment_watch_events_list_recent`
- `get_forecasts`
- `get_trade_retrospectives`
- `trade_retrospective_pending`

Allowed write/order tools:
- `place_order`
- `cancel_order`
- `toss_preview_order`
- `toss_place_order`
- `toss_cancel_order`
- `sell_ladder_fill_preview`
- `buy_ladder_fill_preview`
- `forecast_save`
- `save_trade_retrospective`
- `investment_watch_create`

Write provenance requirements:
- pass `created_by="tradingcodex"` to `forecast_save`
- pass `created_by_profile="tradingcodex"` to `save_trade_retrospective`
- pass `created_by="tradingcodex"` to `investment_watch_create`
- missing or blank labels return `{"success": false, "error": "created_by_required", ...}` before any database write

Forbidden by physical non-registration:
- modify and reconcile tools
- manual holdings mutation
- user settings tools
- analysis artifact and session context persistence
- forecast resolution/calibration
- retrospective aggregate
- watch activation/mutation
- report-writing and report-decision tools

Authentication is mandatory for this profile. `MCP_PROFILE=tradingcodex_execution` fails at startup unless `MCP_AUTH_TOKEN` is non-empty, and the runtime also requires the TradingCodex approval-hash modes configured in `app/mcp_server/main.py`.

### Forecast resolution semantics

`forecast_resolve` auto-closes a due placeholder whose
`forecast_target.kind` is `no_resolvable_forecast`. A dry run reports
`would_close_no_claim`; a persisted run assigns `closed_no_claim`. These rows
keep `outcome` and `brier_score` null and are excluded from calibration
aggregates. Other non-price forecast kinds continue to require an explicit
manual outcome and evidence.

New `price_target` rows must stamp
`outcome_rule_version="window-touch-v1-high-gte-low-lte"` and keep the original
window-touch contract: `at_or_above` uses the window `max(high)` and
`at_or_below` uses `min(low)`. Versionless legacy rows are quarantined before
candle lookup/backfill and are selected separately so they do not consume the
normal due limit. The additive `terminal_close` kind is different. It accepts
`direction="up"|"down"` with
`outcome_rule_version="terminal-close-v1-up-gte-down-lt"` and uses exactly one
allowlisted review-date daily `close` after the exchange-calendar final-session
gate: equality is `up`, while `down` is strictly below. It never reads window
high/low, extended-hours prices, or `adj_close`. Missing, stale, duplicate,
non-final-session, untrusted-source, or invalid-close data leaves the forecast
open with a typed status.

Corporate-action adjustment fields are rejected pending ROB-1043. Legacy rows
are never automatically reinterpreted or superseded; create a new typed
terminal forecast ID and retain the old row in quarantine. See
[`docs/runbooks/forecast-terminal-close.md`](../../docs/runbooks/forecast-terminal-close.md)
for source-basis limits, the legacy-row procedure, read-only dry-run settings,
and the ROB-1041/1042/1043 split.

### Inactive historical KIS adapters

No deployed MCP profile registers `kis_live_*`, `kis_mock_*`, KIS reconcile, or
KIS mirror tools. `account_mode="kis_live"` and `"kis_mock"` are retained only
where historical ledger rows require their original provenance; operational
dispatch fails closed and never falls back to Toss. The provider transport,
client, and service modules were removed; only the ledger models and their
historical `account_mode` values remain.

