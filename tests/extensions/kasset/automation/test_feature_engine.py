from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.extensions.kasset.automation.feature_engine import FeatureEngine
from app.services.daily_candles.repository import DailyCandleRow


def _candles(
    closes: list[float],
    *,
    volumes: list[float] | None = None,
) -> list[DailyCandleRow]:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    resolved_volumes = volumes or [100.0] * len(closes)
    return [
        DailyCandleRow(
            time_utc=start + timedelta(days=index),
            symbol="005930",
            partition="KRX",
            open=close,
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            adj_close=close,
            volume=resolved_volumes[index],
            value=0.0,
            source="test",
        )
        for index, close in enumerate(closes)
    ]


def test_fewer_than_twenty_one_candles_returns_only_insufficient_features() -> None:
    features = FeatureEngine.calculate(_candles([100.0] * 20))

    assert features == {
        "insufficient": True,
        "change_pct": None,
        "volume_ratio": None,
        "rsi14": None,
        "sma20": None,
        "sma20_distance_pct": None,
        "high20_break": None,
        "low20_break": None,
    }


def test_features_match_hand_calculated_wilder_and_twenty_day_values() -> None:
    closes = [100.0]
    closes.extend(101.0 if index % 2 == 0 else 100.0 for index in range(14))
    closes.extend([101.0, 102.0, 103.0, 104.0, 105.0, 106.0])
    features = FeatureEngine.calculate(_candles(closes, volumes=[100.0] * 20 + [250.0]))

    decay = (13.0 / 14.0) ** 6
    expected_rsi = 100.0 * (1.0 - 0.5 * decay)
    assert features["insufficient"] is False
    assert features["change_pct"] == pytest.approx((1.0 / 105.0) * 100.0)
    assert features["volume_ratio"] == pytest.approx(2.5)
    assert features["rsi14"] == pytest.approx(expected_rsi)
    assert features["sma20"] == pytest.approx(101.4)
    assert features["sma20_distance_pct"] == pytest.approx(
        ((106.0 - 101.4) / 101.4) * 100.0
    )
    assert features["high20_break"] is True
    assert features["low20_break"] is False


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        ([float(value) for value in range(100, 121)], 100.0),
        ([float(value) for value in range(120, 99, -1)], 0.0),
        ([100.0] * 21, 50.0),
    ],
)
def test_rsi_wilder_boundaries(closes: list[float], expected: float) -> None:
    assert FeatureEngine.calculate(_candles(closes))["rsi14"] == expected
