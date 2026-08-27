"""Deterministic daily-candle features for KAsset market events."""

from __future__ import annotations

from collections.abc import Sequence

from app.services.daily_candles.repository import DailyCandleRow

_FEATURE_NAMES = (
    "change_pct",
    "volume_ratio",
    "rsi14",
    "sma20",
    "sma20_distance_pct",
    "high20_break",
    "low20_break",
)


class FeatureEngine:
    """Calculate event features without database or provider access."""

    @staticmethod
    def calculate(
        candles: Sequence[DailyCandleRow],
    ) -> dict[str, float | bool | None]:
        if len(candles) < 21:
            return {"insufficient": True, **dict.fromkeys(_FEATURE_NAMES)}

        latest = candles[-1]
        previous = candles[-2]
        previous_twenty = candles[-21:-1]
        sma_window = candles[-20:]

        previous_close = float(previous.close)
        latest_close = float(latest.close)
        change_pct = (
            ((latest_close - previous_close) / previous_close) * 100.0
            if previous_close != 0.0
            else None
        )

        average_volume = sum(float(row.volume) for row in previous_twenty) / 20.0
        volume_ratio = (
            float(latest.volume) / average_volume if average_volume != 0.0 else None
        )

        sma20 = sum(float(row.close) for row in sma_window) / 20.0
        sma20_distance_pct = (
            ((latest_close - sma20) / sma20) * 100.0 if sma20 != 0.0 else None
        )

        return {
            "insufficient": False,
            "change_pct": change_pct,
            "volume_ratio": volume_ratio,
            "rsi14": _wilder_rsi14(candles),
            "sma20": sma20,
            "sma20_distance_pct": sma20_distance_pct,
            "high20_break": float(latest.high)
            > max(float(row.high) for row in previous_twenty),
            "low20_break": float(latest.low)
            < min(float(row.low) for row in previous_twenty),
        }


def _wilder_rsi14(candles: Sequence[DailyCandleRow]) -> float:
    closes = [float(row.close) for row in candles]
    deltas = [
        current - prior for prior, current in zip(closes[:-1], closes[1:], strict=True)
    ]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [max(-delta, 0.0) for delta in deltas]

    average_gain = sum(gains[:14]) / 14.0
    average_loss = sum(losses[:14]) / 14.0
    for gain, loss in zip(gains[14:], losses[14:], strict=True):
        average_gain = (average_gain * 13.0 + gain) / 14.0
        average_loss = (average_loss * 13.0 + loss) / 14.0

    if average_loss == 0.0:
        return 50.0 if average_gain == 0.0 else 100.0
    if average_gain == 0.0:
        return 0.0
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


__all__ = ["FeatureEngine"]
