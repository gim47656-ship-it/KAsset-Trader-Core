"""Deterministic, provider-free strategies over normalized price bars."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from app.extensions.kasset.automation.contracts import (
    Action,
    PriceBar,
    RationaleEvidence,
    StrategyName,
    StrategyResult,
    utc_datetime,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_QUANTUM = Decimal("0.000001")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _clamp_confidence(value: Decimal) -> Decimal:
    return _quantize(max(_ZERO, min(_ONE, value)))


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values))


def _standard_deviation(values: Sequence[Decimal]) -> Decimal:
    mean = _mean(values)
    variance = sum(((value - mean) ** 2 for value in values), _ZERO) / Decimal(
        len(values)
    )
    return variance.sqrt()


def _average_true_range(bars: Sequence[PriceBar], period: int = 14) -> Decimal:
    window = bars[-(period + 1) :]
    ranges: list[Decimal] = []
    for previous, current in zip(window, window[1:], strict=False):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return _mean(ranges)


def _trade_levels(
    action: Action,
    *,
    entry: Decimal,
    atr: Decimal,
) -> tuple[Decimal | None, Decimal | None]:
    if action == Action.HOLD:
        return None, None
    risk = max(atr * Decimal("1.5"), entry * Decimal("0.01"))
    if action == Action.BUY:
        return max(entry - risk, entry * Decimal("0.01")), entry + risk * 2
    return entry + risk, max(entry - risk * 2, entry * Decimal("0.01"))


class _StrategyBase:
    name: StrategyName
    version = "1.0.0"
    minimum_bars: int
    #: 완료 일봉으로 만든 판단이 유효한 창. 이 판단은 "직전 완료 세션의 종가로
    #: 본 상태"이고 실제 진입은 다음 정규장에서 일어난다. 1일이면 다음 세션이
    #: 열리기 전에 만료돼 진행 중인 부분 일봉을 쓰지 않는 한 어떤 setup도 살아
    #: 남지 못한다. 주말·연휴를 건너 다음 거래일까지 덮되, 그 이상 오래된 일봉은
    #: ranker의 ``maximum_bar_age``와 추천 ``valid_until``이 따로 잘라낸다.
    validity = timedelta(days=4)

    def evaluate(
        self,
        bars: Sequence[PriceBar],
        *,
        symbol: str,
        market: Literal["KRX", "US"],
        as_of: datetime,
    ) -> StrategyResult:
        cutoff = utc_datetime(as_of, field_name="as_of")
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            return self._closed(
                symbol="UNKNOWN",
                market=market,
                as_of=cutoff,
                code="INVALID_SYMBOL",
                description="A non-empty symbol is required.",
            )
        if market not in {"KRX", "US"}:
            return self._closed(
                symbol=normalized_symbol,
                market=market,
                as_of=cutoff,
                code="INVALID_MARKET",
                description="Only KRX and US price bars are supported.",
            )
        failure = self._validate_bars(bars, cutoff=cutoff)
        if failure is not None:
            code, description = failure
            return self._closed(
                symbol=normalized_symbol,
                market=market,
                as_of=cutoff,
                code=code,
                description=description,
            )
        if len(bars) < self.minimum_bars:
            return self._closed(
                symbol=normalized_symbol,
                market=market,
                as_of=cutoff,
                code="INSUFFICIENT_BARS",
                description=(
                    f"{self.name} requires at least {self.minimum_bars} complete bars."
                ),
                value=str(len(bars)),
            )

        data_as_of = utc_datetime(bars[-1].timestamp, field_name="bar.timestamp")
        valid_until = data_as_of + self.validity
        if valid_until <= cutoff:
            return self._closed(
                symbol=normalized_symbol,
                market=market,
                as_of=data_as_of,
                code="STALE_PRICE_BARS",
                description="The newest price bar is outside the strategy validity window.",
                value=data_as_of.isoformat(),
            )
        return self._evaluate_valid(
            bars,
            symbol=normalized_symbol,
            market=market,
            data_as_of=data_as_of,
            valid_until=valid_until,
        )

    @staticmethod
    def _validate_bars(
        bars: Sequence[PriceBar],
        *,
        cutoff,
    ) -> tuple[str, str] | None:
        previous_timestamp = None
        for bar in bars:
            try:
                timestamp = utc_datetime(bar.timestamp, field_name="bar.timestamp")
            except (AttributeError, TypeError, ValueError):
                return (
                    "INVALID_TIMESTAMP",
                    "Every price bar needs a timezone-aware timestamp.",
                )
            if timestamp > cutoff:
                return (
                    "FUTURE_PRICE_BAR",
                    "Price bars after the evaluation time are rejected.",
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                return (
                    "NON_MONOTONIC_BARS",
                    "Price bar timestamps must be unique and strictly increasing.",
                )
            previous_timestamp = timestamp

            prices = (bar.open, bar.high, bar.low, bar.close)
            if any(not value.is_finite() for value in (*prices, bar.volume)):
                return "NON_FINITE_BAR", "NaN and infinite OHLCV values are rejected."
            if any(value <= _ZERO for value in prices):
                return "NON_POSITIVE_PRICE", "OHLC prices must be greater than zero."
            if bar.volume < _ZERO:
                return "NEGATIVE_VOLUME", "Volume cannot be negative."
            if bar.high < max(bar.open, bar.close) or bar.low > min(
                bar.open, bar.close
            ):
                return "INVALID_OHLC", "High/low do not contain open and close."
        return None

    def _closed(
        self,
        *,
        symbol: str,
        market: Literal["KRX", "US"],
        as_of,
        code: str,
        description: str,
        value: str = "",
    ) -> StrategyResult:
        return StrategyResult(
            action=Action.HOLD,
            confidence=_ZERO,
            entry=None,
            stop=None,
            target=None,
            rationale=(description,),
            evidence=(
                RationaleEvidence(code=code, value=value, description=description),
            ),
            strategy=self.name,
            version=self.version,
            symbol=symbol,
            market=market,
            as_of=as_of,
            valid_until=as_of,
        )

    def _result(
        self,
        *,
        action: Action,
        confidence: Decimal,
        entry: Decimal,
        atr: Decimal,
        rationale: tuple[str, ...],
        evidence: tuple[RationaleEvidence, ...],
        symbol: str,
        market: Literal["KRX", "US"],
        as_of,
        valid_until,
        levels: tuple[Decimal, Decimal] | None = None,
    ) -> StrategyResult:
        stop, target = levels or _trade_levels(action, entry=entry, atr=atr)
        return StrategyResult(
            action=action,
            confidence=_clamp_confidence(confidence),
            entry=_quantize(entry),
            stop=_quantize(stop) if stop is not None else None,
            target=_quantize(target) if target is not None else None,
            rationale=rationale,
            evidence=evidence,
            strategy=self.name,
            version=self.version,
            symbol=symbol,
            market=market,
            as_of=as_of,
            valid_until=valid_until,
        )


class MomentumStrategy(_StrategyBase):
    name = StrategyName.MOMENTUM
    minimum_bars = 30

    def _evaluate_valid(self, bars, *, symbol, market, data_as_of, valid_until):
        close = bars[-1].close
        short_return = close / bars[-6].close - _ONE
        long_return = close / bars[-21].close - _ONE
        if short_return >= Decimal("0.02") and long_return >= Decimal("0.05"):
            action = Action.BUY
        elif short_return <= Decimal("-0.02") and long_return <= Decimal("-0.05"):
            action = Action.SELL
        else:
            action = Action.HOLD
        strength = min(
            _ONE,
            (abs(short_return) / Decimal("0.06") + abs(long_return) / Decimal("0.15"))
            / 2,
        )
        return self._result(
            action=action,
            confidence=Decimal("0.35") + strength * Decimal("0.60"),
            entry=close,
            atr=_average_true_range(bars),
            rationale=(
                "Short- and medium-horizon returns must agree before momentum acts.",
            ),
            evidence=(
                RationaleEvidence(
                    "RETURN_5", str(_quantize(short_return)), "Five-bar close return."
                ),
                RationaleEvidence(
                    "RETURN_20", str(_quantize(long_return)), "Twenty-bar close return."
                ),
            ),
            symbol=symbol,
            market=market,
            as_of=data_as_of,
            valid_until=valid_until,
        )


class MeanReversionStrategy(_StrategyBase):
    name = StrategyName.MEAN_REVERSION
    minimum_bars = 20

    def _evaluate_valid(self, bars, *, symbol, market, data_as_of, valid_until):
        closes = [bar.close for bar in bars[-20:]]
        average = _mean(closes)
        deviation = _standard_deviation(closes)
        close = closes[-1]
        if deviation == _ZERO:
            z_score = _ZERO
        else:
            z_score = (close - average) / deviation
        if z_score <= Decimal("-1.25"):
            action = Action.BUY
        elif z_score >= Decimal("1.25"):
            action = Action.SELL
        else:
            action = Action.HOLD
        strength = min(_ONE, abs(z_score) / Decimal("3"))
        levels = None
        if action != Action.HOLD:
            target = average
            target_distance = abs(target - close)
            risk = max(target_distance / Decimal("2"), close * Decimal("0.01"))
            stop = (
                max(close - risk, close * Decimal("0.01"))
                if action == Action.BUY
                else close + risk
            )
            levels = (stop, target)
        return self._result(
            action=action,
            confidence=Decimal("0.30") + strength * Decimal("0.65"),
            entry=close,
            atr=_average_true_range(bars),
            levels=levels,
            rationale=(
                "The latest close is compared with its 20-bar population distribution.",
            ),
            evidence=(
                RationaleEvidence(
                    "ZSCORE_20", str(_quantize(z_score)), "20-bar z-score."
                ),
                RationaleEvidence(
                    "MEAN_20", str(_quantize(average)), "20-bar mean close."
                ),
            ),
            symbol=symbol,
            market=market,
            as_of=data_as_of,
            valid_until=valid_until,
        )


class BreakoutStrategy(_StrategyBase):
    name = StrategyName.BREAKOUT
    minimum_bars = 21

    def _evaluate_valid(self, bars, *, symbol, market, data_as_of, valid_until):
        prior = bars[-21:-1]
        upper = max(bar.high for bar in prior)
        lower = min(bar.low for bar in prior)
        close = bars[-1].close
        if close > upper:
            action = Action.BUY
            distance = close / upper - _ONE
        elif close < lower:
            action = Action.SELL
            distance = lower / close - _ONE
        else:
            action = Action.HOLD
            distance = _ZERO
        strength = min(_ONE, abs(distance) / Decimal("0.05"))
        return self._result(
            action=action,
            confidence=Decimal("0.40") + strength * Decimal("0.55"),
            entry=close,
            atr=_average_true_range(bars),
            rationale=("The current close must clear the prior 20-bar price channel.",),
            evidence=(
                RationaleEvidence(
                    "CHANNEL_HIGH_20", str(_quantize(upper)), "Prior high."
                ),
                RationaleEvidence(
                    "CHANNEL_LOW_20", str(_quantize(lower)), "Prior low."
                ),
                RationaleEvidence(
                    "BREAKOUT_DISTANCE",
                    str(_quantize(distance)),
                    "Distance beyond the broken channel edge.",
                ),
            ),
            symbol=symbol,
            market=market,
            as_of=data_as_of,
            valid_until=valid_until,
        )


class VolatilityTrendStrategy(_StrategyBase):
    name = StrategyName.VOLATILITY_TREND
    minimum_bars = 30

    def _evaluate_valid(self, bars, *, symbol, market, data_as_of, valid_until):
        closes = [bar.close for bar in bars[-30:]]
        short_mean = _mean(closes[-10:])
        long_mean = _mean(closes)
        returns = [
            current / previous - _ONE
            for previous, current in zip(closes, closes[1:], strict=False)
        ]
        recent_volatility = _standard_deviation(returns[-10:])
        baseline_volatility = _standard_deviation(returns)
        volatility_ratio = (
            recent_volatility / baseline_volatility
            if baseline_volatility > _ZERO
            else _ZERO
        )
        trend = short_mean / long_mean - _ONE
        if trend >= Decimal("0.015") and volatility_ratio >= Decimal("0.80"):
            action = Action.BUY
        elif trend <= Decimal("-0.015") and volatility_ratio >= Decimal("0.80"):
            action = Action.SELL
        else:
            action = Action.HOLD
        strength = min(
            _ONE,
            abs(trend) / Decimal("0.05") * min(_ONE, volatility_ratio),
        )
        return self._result(
            action=action,
            confidence=Decimal("0.35") + strength * Decimal("0.60"),
            entry=closes[-1],
            atr=_average_true_range(bars),
            rationale=(
                "The 10/30-bar trend acts only when recent volatility is not dormant.",
            ),
            evidence=(
                RationaleEvidence(
                    "TREND_10_30", str(_quantize(trend)), "10/30 mean spread."
                ),
                RationaleEvidence(
                    "VOLATILITY_RATIO",
                    str(_quantize(volatility_ratio)),
                    "Recent versus 30-bar return volatility.",
                ),
            ),
            symbol=symbol,
            market=market,
            as_of=data_as_of,
            valid_until=valid_until,
        )


STRATEGIES = (
    MomentumStrategy(),
    MeanReversionStrategy(),
    BreakoutStrategy(),
    VolatilityTrendStrategy(),
)

__all__ = [
    "BreakoutStrategy",
    "MeanReversionStrategy",
    "MomentumStrategy",
    "STRATEGIES",
    "VolatilityTrendStrategy",
]
