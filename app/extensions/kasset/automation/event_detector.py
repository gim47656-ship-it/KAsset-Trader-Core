"""Pure rule-based event detection for KAsset market scans."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class EventDetector:
    """Convert calculated features and compact news metadata into triggers."""

    @staticmethod
    def detect(
        features: Mapping[str, object],
        news_summaries: Sequence[Mapping[str, object]] | None = None,
    ) -> list[str]:
        triggers: list[str] = []

        change_pct = _number(features.get("change_pct"))
        if change_pct is not None and abs(change_pct) >= 2.0:
            triggers.append("price_spike")

        volume_ratio = _number(features.get("volume_ratio"))
        if volume_ratio is not None and volume_ratio >= 2.0:
            triggers.append("volume_surge")

        rsi14 = _number(features.get("rsi14"))
        if rsi14 is not None and (rsi14 <= 30.0 or rsi14 >= 70.0):
            triggers.append("rsi_extreme")

        if features.get("high20_break") is True or features.get("low20_break") is True:
            triggers.append("breakout")

        if any(
            (importance := _number(item.get("importance"))) is not None
            and importance >= 70.0
            for item in news_summaries or ()
        ):
            triggers.append("important_news")

        return triggers


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


__all__ = ["EventDetector"]
