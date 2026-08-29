"""Deterministic market-regime classification and strategy weight selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from statistics import median

from app.extensions.kasset.automation.contracts import PriceBar, StrategyName


class MarketRegime(StrEnum):
    BULL = "TRENDING_UP"
    BEAR = "TRENDING_DOWN"
    SIDEWAYS = "RANGING"
    VOLATILE = "VOLATILE"


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    regime: MarketRegime
    detail: str
    breadth_above_sma20: Decimal
    median_return20: Decimal
    median_atr_ratio: Decimal
    weights: Mapping[StrategyName, Decimal]


_REGIME_WEIGHTS: dict[MarketRegime, dict[StrategyName, Decimal]] = {
    MarketRegime.BULL: {
        StrategyName.MOMENTUM: Decimal("0.35"),
        StrategyName.MEAN_REVERSION: Decimal("0.10"),
        StrategyName.BREAKOUT: Decimal("0.30"),
        StrategyName.VOLATILITY_TREND: Decimal("0.25"),
    },
    MarketRegime.BEAR: {
        StrategyName.MOMENTUM: Decimal("0.20"),
        StrategyName.MEAN_REVERSION: Decimal("0.35"),
        StrategyName.BREAKOUT: Decimal("0.15"),
        StrategyName.VOLATILITY_TREND: Decimal("0.30"),
    },
    MarketRegime.SIDEWAYS: {
        StrategyName.MOMENTUM: Decimal("0.15"),
        StrategyName.MEAN_REVERSION: Decimal("0.40"),
        StrategyName.BREAKOUT: Decimal("0.20"),
        StrategyName.VOLATILITY_TREND: Decimal("0.25"),
    },
    MarketRegime.VOLATILE: {
        StrategyName.MOMENTUM: Decimal("0.15"),
        StrategyName.MEAN_REVERSION: Decimal("0.20"),
        StrategyName.BREAKOUT: Decimal("0.25"),
        StrategyName.VOLATILITY_TREND: Decimal("0.40"),
    },
}


def weights_for_regime(regime: MarketRegime | str) -> Mapping[StrategyName, Decimal]:
    """Return a defensive copy so one run cannot mutate global policy."""

    return dict(_REGIME_WEIGHTS[MarketRegime(regime)])


def assess_market_regime(
    bars_by_symbol: Mapping[str, Sequence[PriceBar]],
) -> RegimeAssessment:
    """Classify one market from the same candidate bars consumed by strategies.

    A minimum of twenty complete bars is required per contributing symbol. Sparse
    symbols are ignored rather than padded. An entirely sparse universe is
    SIDEWAYS with explicit zero evidence, which keeps the ensemble deterministic
    and prevents missing market data from manufacturing a trend.
    """

    above_sma = 0
    returns: list[Decimal] = []
    atr_ratios: list[Decimal] = []
    contributors = 0
    for bars in bars_by_symbol.values():
        if len(bars) < 20:
            continue
        window = bars[-20:]
        close = window[-1].close
        first_close = window[0].close
        if close <= 0 or first_close <= 0:
            continue
        contributors += 1
        sma20 = sum((bar.close for bar in window), Decimal("0")) / Decimal(20)
        if close > sma20:
            above_sma += 1
        returns.append((close / first_close) - Decimal("1"))

        ranges: list[Decimal] = []
        previous_close = window[0].close
        for bar in window[1:]:
            ranges.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - previous_close),
                    abs(bar.low - previous_close),
                )
            )
            previous_close = bar.close
        if ranges:
            atr = sum(ranges, Decimal("0")) / Decimal(len(ranges))
            atr_ratios.append(atr / close)

    quantum = Decimal("0.000001")
    if contributors == 0:
        breadth = return20 = atr_ratio = Decimal("0")
        regime = MarketRegime.SIDEWAYS
    else:
        breadth = Decimal(above_sma) / Decimal(contributors)
        return20 = Decimal(str(median(returns))) if returns else Decimal("0")
        atr_ratio = Decimal(str(median(atr_ratios))) if atr_ratios else Decimal("0")
        if atr_ratio >= Decimal("0.035"):
            regime = MarketRegime.VOLATILE
        elif breadth >= Decimal("0.60") and return20 > Decimal("0.02"):
            regime = MarketRegime.BULL
        elif breadth <= Decimal("0.40") and return20 < Decimal("-0.02"):
            regime = MarketRegime.BEAR
        else:
            regime = MarketRegime.SIDEWAYS

    breadth = breadth.quantize(quantum, rounding=ROUND_HALF_EVEN)
    return20 = return20.quantize(quantum, rounding=ROUND_HALF_EVEN)
    atr_ratio = atr_ratio.quantize(quantum, rounding=ROUND_HALF_EVEN)
    detail = (
        f"contributors={contributors}; breadthAboveSma20={breadth}; "
        f"medianReturn20={return20}; medianAtrRatio={atr_ratio}"
    )
    return RegimeAssessment(
        regime=regime,
        detail=detail,
        breadth_above_sma20=breadth,
        median_return20=return20,
        median_atr_ratio=atr_ratio,
        weights=weights_for_regime(regime),
    )


__all__ = [
    "MarketRegime",
    "RegimeAssessment",
    "assess_market_regime",
    "weights_for_regime",
]
