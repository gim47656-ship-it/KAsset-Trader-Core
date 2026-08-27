"""Pure next-bar backtesting for deterministic automation strategies."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from app.extensions.kasset.automation.contracts import (
    Action,
    BacktestResult,
    BacktestSignal,
    BacktestTrade,
    DeterministicStrategy,
    PriceBar,
    utc_datetime,
)
from app.extensions.kasset.automation.strategies import STRATEGIES

_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_capital: Decimal = Decimal("100000")
    fee_rate: Decimal = Decimal("0.001")
    slippage_rate: Decimal = Decimal("0.0005")

    def __post_init__(self) -> None:
        for name in ("initial_capital", "fee_rate", "slippage_rate"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                object.__setattr__(self, name, Decimal(str(value)))
        if not self.initial_capital.is_finite() or self.initial_capital <= 0:
            raise ValueError("initial_capital must be finite and positive")
        for name in ("fee_rate", "slippage_rate"):
            value = getattr(self, name)
            if not value.is_finite() or not Decimal("0") <= value < Decimal("1"):
                raise ValueError(f"{name} must be finite and in [0, 1)")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _validate_bars(bars: Sequence[PriceBar]) -> None:
    if not bars:
        raise ValueError("at least one price bar is required")
    previous = None
    for bar in bars:
        timestamp = utc_datetime(bar.timestamp, field_name="bar.timestamp")
        if previous is not None and timestamp <= previous:
            raise ValueError("price bars must be strictly increasing")
        previous = timestamp
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not value.is_finite() or value <= 0 for value in prices):
            raise ValueError("OHLC prices must be finite and positive")
        if not bar.volume.is_finite() or bar.volume < 0:
            raise ValueError("volume must be finite and non-negative")
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
            raise ValueError("invalid OHLC range")


def run_backtest(
    strategy: DeterministicStrategy,
    bars: Sequence[PriceBar],
    *,
    symbol: str,
    market: str,
    config: BacktestConfig,
) -> BacktestResult:
    """Run a long-only strategy with signals filled at the next bar's open.

    A strategy sees only ``bars[: signal_index + 1]``. BUY opens a long and SELL
    closes it; redundant signals do nothing. Every fill pays the same configured
    adverse slippage and proportional fee. Any remaining position is liquidated
    at the final close under the same costs.
    """

    _validate_bars(bars)
    normalized_market = market.strip().upper()
    if normalized_market not in {"KRX", "US"}:
        raise ValueError("market must be KRX or US")
    if len(bars) < strategy.minimum_bars:
        raise ValueError(
            f"{strategy.name} requires at least {strategy.minimum_bars} bars"
        )

    cash = config.initial_capital
    quantity = Decimal("0")
    entry_at = None
    entry_price = None
    entry_cost = None
    peak_equity = config.initial_capital
    max_drawdown = Decimal("0")
    trades: list[BacktestTrade] = []
    signals: list[BacktestSignal] = []

    for signal_index in range(strategy.minimum_bars - 1, len(bars) - 1):
        signal_bar = bars[signal_index]
        execution_bar = bars[signal_index + 1]
        result = strategy.evaluate(
            bars[: signal_index + 1],
            symbol=symbol,
            market=normalized_market,  # type: ignore[arg-type]
            as_of=signal_bar.timestamp,
        )
        signals.append(
            BacktestSignal(
                signal_at=utc_datetime(signal_bar.timestamp, field_name="signal_at"),
                execute_at=utc_datetime(
                    execution_bar.timestamp, field_name="execute_at"
                ),
                action=result.action,
            )
        )

        if result.action == Action.BUY and quantity == 0:
            fill_price = execution_bar.open * (Decimal("1") + config.slippage_rate)
            quantity = cash / (fill_price * (Decimal("1") + config.fee_rate))
            notional = quantity * fill_price
            fee = notional * config.fee_rate
            entry_cost = notional + fee
            cash -= entry_cost
            entry_at = utc_datetime(execution_bar.timestamp, field_name="entry_at")
            entry_price = fill_price
        elif result.action == Action.SELL and quantity > 0:
            fill_price = execution_bar.open * (Decimal("1") - config.slippage_rate)
            notional = quantity * fill_price
            fee = notional * config.fee_rate
            cash += notional - fee
            assert (
                entry_at is not None
                and entry_price is not None
                and entry_cost is not None
            )
            trades.append(
                BacktestTrade(
                    entry_at=entry_at,
                    exit_at=utc_datetime(execution_bar.timestamp, field_name="exit_at"),
                    entry_price=_q(entry_price),
                    exit_price=_q(fill_price),
                    quantity=_q(quantity),
                    net_pnl=_q(cash - entry_cost),
                )
            )
            quantity = Decimal("0")
            entry_at = entry_price = entry_cost = None

        marked_equity = cash + quantity * execution_bar.close
        peak_equity = max(peak_equity, marked_equity)
        if peak_equity > 0:
            max_drawdown = max(
                max_drawdown,
                (peak_equity - marked_equity) / peak_equity,
            )

    if quantity > 0:
        final_bar = bars[-1]
        fill_price = final_bar.close * (Decimal("1") - config.slippage_rate)
        notional = quantity * fill_price
        fee = notional * config.fee_rate
        cash += notional - fee
        assert (
            entry_at is not None and entry_price is not None and entry_cost is not None
        )
        trades.append(
            BacktestTrade(
                entry_at=entry_at,
                exit_at=utc_datetime(final_bar.timestamp, field_name="exit_at"),
                entry_price=_q(entry_price),
                exit_price=_q(fill_price),
                quantity=_q(quantity),
                net_pnl=_q(cash - entry_cost),
            )
        )
        quantity = Decimal("0")
        peak_equity = max(peak_equity, cash)
        if peak_equity > 0:
            max_drawdown = max(max_drawdown, (peak_equity - cash) / peak_equity)

    final_equity = _q(cash)
    total_return = final_equity / config.initial_capital - Decimal("1")
    wins = sum(trade.net_pnl > 0 for trade in trades)
    win_rate = Decimal(wins) / Decimal(len(trades)) if trades else Decimal("0")
    return BacktestResult(
        strategy=strategy.name,
        strategy_version=strategy.version,
        initial_capital=_q(config.initial_capital),
        final_equity=final_equity,
        trade_count=len(trades),
        total_return=_q(total_return),
        max_drawdown=_q(max_drawdown),
        win_rate=_q(win_rate),
        trades=tuple(trades),
        signals=tuple(signals),
    )


def run_all_backtests(
    bars: Sequence[PriceBar],
    *,
    symbol: str,
    market: str,
    config: BacktestConfig,
    strategies: Sequence[DeterministicStrategy] = STRATEGIES,
) -> tuple[BacktestResult, ...]:
    """Evaluate every supplied strategy under one shared cost configuration."""

    return tuple(
        run_backtest(
            strategy,
            bars,
            symbol=symbol,
            market=market,
            config=config,
        )
        for strategy in strategies
    )


__all__ = ["BacktestConfig", "run_all_backtests", "run_backtest"]
