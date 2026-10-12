"""Sizing-evidence readers and the AUTO_PAPER BUY chase guard.

The recommendation's ``positionSizing`` evidence records the entry price, the
strategy stop and the risk budget a BUY quantity was sized with. Two consumers
read it: the Position Manager (so the managed stop is never looser than the
stop the size was based on) and the consumer (so a BUY that fills far above the
sized entry is blocked or shrunk back to the planned risk).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation

#: A BUY may fill at most this fraction of the planned risk ``R = entry - stop``
#: above the sized entry price. Past that the sized stop is no longer a
#: reasonable stop for the actual fill: 2026-10-12 PAPER losses came from fills
#: ~1.3R above the sized entry (009155: R=12000, fill +15500), which turned a
#: 150000 risk budget into a loss 3.7x larger.
CHASE_LIMIT_RISK_FRACTION = Decimal("0.5")

CHASE_LIMIT_EXCEEDED = "chase_limit_exceeded"
CHASE_GUARD_UNAVAILABLE = "chase_guard_unavailable"
#: Executable price at or below the sized stop: the setup is already invalidated.
BELOW_STRATEGY_STOP = "below_strategy_stop"

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class EntrySizing:
    """The sizing facts a BUY recommendation was planned with."""

    entry_price: Decimal
    strategy_stop: Decimal
    risk_budget: Decimal
    lot_size: Decimal


@dataclass(frozen=True, slots=True)
class ChaseGuardDecision:
    allowed: bool
    #: ``None`` when allowed.
    reason: str | None
    #: Quantity to submit; never larger than the requested quantity. ``None``
    #: when blocked.
    quantity: Decimal | None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _sizing_block(
    evidence: object,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Return ``(evidence item, positionSizing)`` of the vertical-slice item."""

    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        return {}, {}
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        portfolio = item.get("portfolio")
        if not isinstance(portfolio, Mapping):
            continue
        sizing = portfolio.get("positionSizing")
        if isinstance(sizing, Mapping):
            return item, sizing
    return {}, {}


def strategy_stop_from_evidence(evidence: object) -> Decimal | None:
    """The strategy stop a BUY was sized with, or ``None`` when not recorded."""

    item, sizing = _sizing_block(evidence)
    for raw in (sizing.get("strategyStop"), item.get("stopPrice")):
        stop = _decimal(raw)
        if stop is not None and stop > _ZERO:
            return stop
    return None


def entry_sizing_from_evidence(evidence: object) -> EntrySizing | None:
    """Full sizing facts, or ``None`` if any needed fact is missing or invalid."""

    item, sizing = _sizing_block(evidence)
    entry = _decimal(sizing.get("entryPrice"))
    if entry is None:
        entry = _decimal(item.get("entryPrice"))
    stop = strategy_stop_from_evidence(evidence)
    budget = _decimal(sizing.get("riskBudget"))
    lot = _decimal(sizing.get("lotSize"))
    if entry is None or stop is None or budget is None or lot is None:
        return None
    if entry <= _ZERO or stop >= entry or budget <= _ZERO or lot <= _ZERO:
        return None
    return EntrySizing(
        entry_price=entry,
        strategy_stop=stop,
        risk_budget=budget,
        lot_size=lot,
    )


def evaluate_chase_guard(
    sizing: EntrySizing | None,
    *,
    executable_price: Decimal | None,
    quantity: Decimal,
) -> ChaseGuardDecision:
    """Block or shrink an AUTO_PAPER BUY by the price it would actually fill at.

    Fails closed when the sizing facts or the executable price are unavailable.
    """

    if (
        sizing is None
        or executable_price is None
        or not executable_price.is_finite()
        or executable_price <= _ZERO
    ):
        return ChaseGuardDecision(False, CHASE_GUARD_UNAVAILABLE, None)
    planned_risk = sizing.entry_price - sizing.strategy_stop
    chase = executable_price - sizing.entry_price
    if chase > CHASE_LIMIT_RISK_FRACTION * planned_risk:
        return ChaseGuardDecision(False, CHASE_LIMIT_EXCEEDED, None)
    risk_per_unit = executable_price - sizing.strategy_stop
    if risk_per_unit <= _ZERO:
        # The setup is invalidated; Position Manager would also drop the strategy
        # stop (it must sit below the fill) and fall back to the wide ATR stop.
        return ChaseGuardDecision(False, BELOW_STRATEGY_STOP, None)
    lots = (sizing.risk_budget / risk_per_unit / sizing.lot_size).to_integral_value(
        rounding=ROUND_DOWN
    )
    capped = min(quantity, lots * sizing.lot_size)
    if capped <= _ZERO:
        return ChaseGuardDecision(False, CHASE_LIMIT_EXCEEDED, None)
    return ChaseGuardDecision(True, None, capped)


__all__ = [
    "BELOW_STRATEGY_STOP",
    "CHASE_GUARD_UNAVAILABLE",
    "CHASE_LIMIT_EXCEEDED",
    "CHASE_LIMIT_RISK_FRACTION",
    "ChaseGuardDecision",
    "EntrySizing",
    "entry_sizing_from_evidence",
    "evaluate_chase_guard",
    "strategy_stop_from_evidence",
]
