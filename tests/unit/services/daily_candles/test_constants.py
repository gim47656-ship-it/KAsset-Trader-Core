def test_horizon_constants_are_ints():
    from app.services.daily_candles.constants import (
        DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
        DAILY_CANDLE_BACKFILL_BARS_KR,
        DAILY_CANDLE_BACKFILL_BARS_US,
    )

    for value in (
        DAILY_CANDLE_BACKFILL_BARS_KR,
        DAILY_CANDLE_BACKFILL_BARS_US,
        DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
    ):
        assert isinstance(value, int)
        assert value > 0


def test_scheduled_sync_horizons_are_smaller_than_backfill_horizons():
    from app.services.daily_candles.constants import (
        DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
        DAILY_CANDLE_BACKFILL_BARS_KR,
        DAILY_CANDLE_BACKFILL_BARS_US,
        DAILY_CANDLE_SYNC_BARS_CRYPTO,
        DAILY_CANDLE_SYNC_BARS_KR,
        DAILY_CANDLE_SYNC_BARS_US,
    )

    assert 0 < DAILY_CANDLE_SYNC_BARS_KR < DAILY_CANDLE_BACKFILL_BARS_KR
    assert 0 < DAILY_CANDLE_SYNC_BARS_US < DAILY_CANDLE_BACKFILL_BARS_US
    assert 0 < DAILY_CANDLE_SYNC_BARS_CRYPTO < DAILY_CANDLE_BACKFILL_BARS_CRYPTO
