# app/services/execution_ledger/opening_lots.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.schemas.execution_ledger import (
    AccountMode,
    Broker,
    Currency,
    ExecutionLedgerUpsert,
    InstrumentTypeValue,
)

MatchKey = tuple[str, str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class OpeningLotCandidate:
    broker: Broker
    account_mode: AccountMode
    venue: str
    instrument_type: InstrumentTypeValue
    symbol: str
    raw_symbol: str
    currency: Currency
    current_qty: Decimal
    avg_price: Decimal
    avg_price_modified: bool = False


@dataclass(frozen=True, slots=True)
class OpeningLotSkip:
    key: MatchKey
    reason: Literal[
        "covered_by_ledger_net",
        "non_positive_current_qty",
        "non_positive_avg_price",
        "upbit_avg_price_modified",
    ]
    current_qty: Decimal
    ledger_net_qty: Decimal


@dataclass(slots=True)
class OpeningLotPlan:
    upserts: list[ExecutionLedgerUpsert] = field(default_factory=list)
    skipped: list[OpeningLotSkip] = field(default_factory=list)


def _match_key(candidate: OpeningLotCandidate) -> MatchKey:
    return (
        candidate.broker,
        candidate.account_mode,
        candidate.venue,
        candidate.instrument_type,
        candidate.symbol,
        candidate.currency,
    )


def _seed_order_id(candidate: OpeningLotCandidate, cutover: datetime) -> str:
    return (
        f"SEED-{cutover:%Y%m%d}-{candidate.broker}-{candidate.venue}-{candidate.symbol}"
    )


def build_opening_lot_plan(
    *,
    candidates: list[OpeningLotCandidate],
    ledger_net_by_key: dict[MatchKey, Decimal],
    cutover: datetime,
) -> OpeningLotPlan:
    plan = OpeningLotPlan()
    for candidate in candidates:
        key = _match_key(candidate)
        ledger_net_qty = ledger_net_by_key.get(key, Decimal("0"))
        if candidate.current_qty <= 0:
            plan.skipped.append(
                OpeningLotSkip(
                    key,
                    "non_positive_current_qty",
                    candidate.current_qty,
                    ledger_net_qty,
                )
            )
            continue
        if candidate.avg_price <= 0:
            plan.skipped.append(
                OpeningLotSkip(
                    key, "non_positive_avg_price", candidate.current_qty, ledger_net_qty
                )
            )
            continue
        if candidate.broker == "upbit" and candidate.avg_price_modified:
            plan.skipped.append(
                OpeningLotSkip(
                    key,
                    "upbit_avg_price_modified",
                    candidate.current_qty,
                    ledger_net_qty,
                )
            )
            continue

        opening_qty = candidate.current_qty - ledger_net_qty
        if opening_qty <= 0:
            plan.skipped.append(
                OpeningLotSkip(
                    key, "covered_by_ledger_net", candidate.current_qty, ledger_net_qty
                )
            )
            continue

        plan.upserts.append(
            ExecutionLedgerUpsert(
                broker=candidate.broker,
                account_mode=candidate.account_mode,
                venue=candidate.venue,
                instrument_type=candidate.instrument_type,
                symbol=candidate.symbol,
                raw_symbol=candidate.raw_symbol,
                side="buy",
                broker_order_id=_seed_order_id(candidate, cutover),
                fill_seq=0,
                filled_qty=opening_qty,
                filled_price=candidate.avg_price,
                filled_at=cutover,
                currency=candidate.currency,
                source="manual_import",
                raw_payload_json={
                    "seed_kind": "opening_lot",
                    "current_qty": str(candidate.current_qty),
                    "ledger_net_qty": str(ledger_net_qty),
                    "cutover": cutover.isoformat(),
                },
            )
        )
    return plan


async def load_opening_lot_candidates(
    brokers: list[str],
) -> list[OpeningLotCandidate]:
    requested = {str(broker).strip().lower() for broker in brokers}
    if "kis" in requested:
        raise ValueError("provider kis is not operational")
    candidates: list[OpeningLotCandidate] = []
    if "toss" in requested:
        candidates.extend(await load_toss_opening_lot_candidates())
    if "upbit" in requested:
        candidates.extend(await load_upbit_opening_lot_candidates())
    return candidates


async def load_toss_opening_lot_candidates() -> list[OpeningLotCandidate]:
    from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot

    snapshot = await fetch_toss_portfolio_snapshot(
        need_sellable=False,
        need_cash=False,
    )
    candidates: list[OpeningLotCandidate] = []
    for position in snapshot.positions:
        symbol = str(position.symbol or "").strip().upper()
        if not symbol:
            continue
        if position.instrument_type == "equity_kr":
            venue = "toss_kr"
            currency = "KRW"
        elif position.instrument_type == "equity_us":
            venue = "toss_us"
            currency = "USD"
        else:
            continue
        candidates.append(
            OpeningLotCandidate(
                broker="toss",
                account_mode="live",
                venue=venue,
                instrument_type=position.instrument_type,
                symbol=symbol,
                raw_symbol=symbol,
                currency=currency,
                current_qty=Decimal(str(position.quantity)),
                avg_price=Decimal(str(position.avg_buy_price)),
            )
        )
    return candidates


async def load_upbit_opening_lot_candidates() -> list[OpeningLotCandidate]:
    from app.services.brokers.upbit.client import (
        fetch_my_coins,
        parse_upbit_account_row,
    )

    rows = await fetch_my_coins()
    candidates: list[OpeningLotCandidate] = []
    for row in rows:
        currency = str(row.get("currency") or "").strip().upper()
        if not currency or currency == "KRW":
            continue
        unit_currency = str(row.get("unit_currency") or "KRW").strip().upper()
        if unit_currency != "KRW":
            # The ledger normalizer only ever writes venue='upbit_krw' with
            # currency='KRW'; a BTC/USDT-market seed could never match a sell
            # and its avg price is not KRW-denominated.
            continue
        parsed = parse_upbit_account_row(row)
        current_qty = Decimal(str(parsed["total_quantity"]))
        avg_price = Decimal(str(parsed["avg_buy_price"]))
        candidates.append(
            OpeningLotCandidate(
                broker="upbit",
                account_mode="live",
                venue="upbit_krw",
                instrument_type="crypto",
                symbol=currency,
                raw_symbol=f"KRW-{currency}",
                currency="KRW",
                current_qty=current_qty,
                avg_price=avg_price,
                avg_price_modified=bool(parsed["avg_buy_price_modified"]),
            )
        )
    return candidates
