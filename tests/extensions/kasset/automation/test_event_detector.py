from __future__ import annotations

import pytest

from app.extensions.kasset.automation.event_detector import EventDetector


@pytest.mark.parametrize("change_pct", [-2.0, 2.0])
def test_price_spike_includes_both_threshold_boundaries(change_pct: float) -> None:
    assert EventDetector.detect({"change_pct": change_pct}) == ["price_spike"]


def test_detector_returns_all_triggers_once_in_stable_rule_order() -> None:
    features = {
        "change_pct": -2.5,
        "volume_ratio": 2.0,
        "rsi14": 30.0,
        "high20_break": False,
        "low20_break": True,
    }
    news = [
        {"summary": "ordinary", "importance": 69},
        {"summary": "material", "importance": 70},
        {"summary": "also material", "importance": 95},
    ]

    assert EventDetector.detect(features, news) == [
        "price_spike",
        "volume_surge",
        "rsi_extreme",
        "breakout",
        "important_news",
    ]


@pytest.mark.parametrize("rsi14", [30.0, 70.0])
def test_rsi_extreme_includes_both_threshold_boundaries(rsi14: float) -> None:
    assert EventDetector.detect({"rsi14": rsi14}) == ["rsi_extreme"]


def test_no_rule_match_returns_empty_list() -> None:
    assert (
        EventDetector.detect(
            {
                "change_pct": 1.99,
                "volume_ratio": 1.99,
                "rsi14": 50.0,
                "high20_break": False,
                "low20_break": False,
            },
            [{"summary": "ordinary", "importance": 69}],
        )
        == []
    )
