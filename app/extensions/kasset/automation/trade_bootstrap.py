"""거래별 PnL의 결정론적 block-bootstrap 분포 지표.

분포 퍼센타일은 ``zachisit/july-backtester``의
``helpers/monte_carlo.py`` 방법론을 따르되, 임의 점수나 verdict는 만들지 않는다.
기본 표집은 연승·연패 군집을 보존하는 순환 block bootstrap이다.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import isqrt
from random import Random

from app.extensions.kasset.automation.strategy_promotion import (
    DEFAULT_PROMOTION_THRESHOLDS,
)

_ZERO = Decimal("0")
DEFAULT_TRADE_BOOTSTRAP_SEED = 0
_SAMPLING_METHODS = ("block", "iid")


@dataclass(frozen=True, slots=True)
class TradeBootstrapResult:
    sample_count: int
    simulations: int
    block_size: int
    sampling: str
    seed: int
    pnl_p5: Decimal
    pnl_p50: Decimal
    historical_pnl_percentile: Decimal
    max_drawdown_p50: Decimal
    max_drawdown_p95: Decimal

    def as_payload(self) -> dict[str, object]:
        """기존 승격 payload와 같이 Decimal을 JSON 숫자가 아닌 문자열로 보존한다."""

        return {
            "sampleCount": self.sample_count,
            "simulations": self.simulations,
            "blockSize": self.block_size,
            "sampling": self.sampling,
            "seed": self.seed,
            "pnlP5": _decimal_text(self.pnl_p5),
            "pnlP50": _decimal_text(self.pnl_p50),
            "historicalPnlPercentile": _decimal_text(self.historical_pnl_percentile),
            "maxDrawdownP50": _decimal_text(self.max_drawdown_p50),
            "maxDrawdownP95": _decimal_text(self.max_drawdown_p95),
        }


def calculate_trade_bootstrap(
    trade_pnls: Sequence[Decimal],
    initial_capital: Decimal,
    *,
    simulations: int = 1000,
    sampling: str = "block",
    seed: int = DEFAULT_TRADE_BOOTSTRAP_SEED,
) -> TradeBootstrapResult | None:
    """거래 PnL을 재표집해 PnL과 최대 낙폭의 advisory 분포를 계산한다.

    PnL, equity, drawdown, percentile 보간은 처음부터 끝까지 ``Decimal``이다.
    block 크기도 ``isqrt``로 구하므로 Decimal↔float 변환 지점이 없고, payload
    경계에서만 Decimal을 정규화된 문자열로 바꾼다.
    """

    pnls = tuple(trade_pnls)
    if len(pnls) < DEFAULT_PROMOTION_THRESHOLDS.min_trade_count:
        return None
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in pnls):
        raise ValueError("trade_pnls must contain only finite Decimal values")
    if not isinstance(initial_capital, Decimal) or not initial_capital.is_finite():
        raise ValueError("initial_capital must be a finite Decimal")
    if type(simulations) is not int or simulations < 1:
        raise ValueError("simulations must be a positive integer")
    if sampling not in _SAMPLING_METHODS:
        raise ValueError(f"unsupported sampling method: {sampling!r}")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")

    sample_count = len(pnls)
    block_size = max(1, isqrt(sample_count)) if sampling == "block" else 1
    rng = Random(seed)
    simulated_pnls: list[Decimal] = []
    simulated_drawdowns: list[Decimal] = []
    for _ in range(simulations):
        final_equity, max_drawdown = _equity_and_drawdown(
            _sampled_pnls(
                pnls,
                rng=rng,
                sampling=sampling,
                block_size=block_size,
            ),
            initial_capital,
        )
        simulated_pnls.append(final_equity - initial_capital)
        simulated_drawdowns.append(max_drawdown)

    ordered_pnls = sorted(simulated_pnls)
    ordered_drawdowns = sorted(simulated_drawdowns)
    historical_pnl = sum(pnls, _ZERO)
    historical_percentile = Decimal(
        sum(value <= historical_pnl for value in simulated_pnls)
    ) / Decimal(simulations)
    return TradeBootstrapResult(
        sample_count=sample_count,
        simulations=simulations,
        block_size=block_size,
        sampling=sampling,
        seed=seed,
        pnl_p5=_percentile(ordered_pnls, 5),
        pnl_p50=_percentile(ordered_pnls, 50),
        historical_pnl_percentile=historical_percentile,
        max_drawdown_p50=_percentile(ordered_drawdowns, 50),
        max_drawdown_p95=_percentile(ordered_drawdowns, 95),
    )


def _sampled_pnls(
    pnls: tuple[Decimal, ...],
    *,
    rng: Random,
    sampling: str,
    block_size: int,
) -> Iterator[Decimal]:
    if sampling == "iid":
        for _ in pnls:
            yield pnls[rng.randrange(len(pnls))]
        return

    produced = 0
    while produced < len(pnls):
        start = rng.randrange(len(pnls))
        take = min(block_size, len(pnls) - produced)
        for offset in range(take):
            yield pnls[(start + offset) % len(pnls)]
        produced += take


def _equity_and_drawdown(
    sampled_pnls: Iterator[Decimal], initial_capital: Decimal
) -> tuple[Decimal, Decimal]:
    equity = initial_capital
    running_max = initial_capital
    max_drawdown = _ZERO
    for pnl in sampled_pnls:
        equity += pnl
        running_max = max(running_max, equity)
        drawdown = (
            (running_max - equity) / running_max if running_max > _ZERO else _ZERO
        )
        max_drawdown = max(max_drawdown, drawdown)
    return equity, max_drawdown


def _percentile(ordered: Sequence[Decimal], percentile: int) -> Decimal:
    scaled_rank = (len(ordered) - 1) * percentile
    lower_index, remainder = divmod(scaled_rank, 100)
    lower = ordered[lower_index]
    if remainder == 0:
        return lower
    upper = ordered[lower_index + 1]
    return lower + (upper - lower) * Decimal(remainder) / Decimal(100)


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == _ZERO else format(normalized, "f")


__all__ = [
    "DEFAULT_TRADE_BOOTSTRAP_SEED",
    "TradeBootstrapResult",
    "calculate_trade_bootstrap",
]
