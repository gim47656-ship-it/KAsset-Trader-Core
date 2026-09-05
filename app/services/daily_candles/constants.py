"""Batch-ingest horizon constants for the daily candle store.

These constants govern how many bars per symbol the daily candle batch
ingest job and the initial backfill CLI are permitted to request from
the external API. They are intentionally separate from the wrapper-level
safety clamp `app.services.brokers.kis.constants.DEFAULT_CANDLES`, which
protects ad-hoc MCP/API display calls (`get_ohlcv(count)` style) from
accidentally requesting huge windows.

Raising these values does not raise the display clamp; the two knobs
remain independent on purpose.
"""

DAILY_CANDLE_BACKFILL_BARS_KR: int = 400
DAILY_CANDLE_BACKFILL_BARS_US: int = 400
DAILY_CANDLE_BACKFILL_BARS_CRYPTO: int = 400

# Daily scheduled syncs are intentionally incremental. Full 400-bar windows are
# for explicit backfill only; running them for every active symbol on every cron
# tick would put unnecessary pressure on provider rate limits.
DAILY_CANDLE_SYNC_BARS_KR: int = 10
DAILY_CANDLE_SYNC_BARS_US: int = 10
DAILY_CANDLE_SYNC_BARS_CRYPTO: int = 10

# US 벤치마크는 유니버스 동기화와 분리된 sync_benchmark 경로가 단독으로 쓴다.
# 유니버스 경로가 같은 심볼을 다른 거래소 파티션(AMEX)으로 쓰면 partition=None
# 조회에서 같은 날짜가 두 번 나와 60세션 벤치마크 계산이 fail-closed된다.
US_BENCHMARK_SYMBOL: str = "SPY"
US_BENCHMARK_PARTITION: str = "NASD"
