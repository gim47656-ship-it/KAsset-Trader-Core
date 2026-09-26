"""Add securities missing from symbol_master using the Toss universes; dry-run by default.

symbol_master is the search/watchlist master, but nothing refreshed it after the NH
sync was removed. kr_symbol_universe/us_symbol_universe are refreshed daily from Toss.
This inserts active rows that symbol_master lacks:
- KRX: common STOCK → COMMON_STOCK, ETF → ETF (preferred shares stay out, as before)
- US: STOCK → COMMON_STOCK, ETF → ETF, DEPOSITARY_RECEIPT (ADR)
Existing rows are never updated or deactivated.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.db import AsyncSessionLocal
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.symbol_master import SymbolMaster
from app.models.us_symbol_universe import USSymbolUniverse

US_TYPES = {
    "STOCK": "COMMON_STOCK",
    "ETF": "ETF",
    "DEPOSITARY_RECEIPT": "DEPOSITARY_RECEIPT",
}


def _row(market: str, symbol: str, name: str, name_en: str | None, kind: str) -> dict:
    return {
        "market": market,
        "symbol": symbol,
        "name": name[:200],
        "name_en": (name_en or "").strip()[:200] or None,
        "security_type": kind,
        "is_active": True,
    }


def build_kr_rows(universe: list[Any], existing: set[str]) -> list[dict]:
    rows = []
    for item in universe:
        symbol, name = item.symbol.strip().upper(), (item.name or "").strip()
        if item.security_type == "ETF":
            kind = "ETF"
        elif item.security_type == "STOCK" and item.is_common_share is True:
            kind = "COMMON_STOCK"
        else:
            continue
        if name and symbol not in existing:
            rows.append(_row("KRX", symbol, name, None, kind))
    return rows


def build_us_rows(universe: list[Any], existing: set[str]) -> list[dict]:
    rows = []
    for item in universe:
        kind = US_TYPES.get(item.security_type or "")
        symbol = item.symbol.strip().upper()
        name = (item.name_kr or "").strip() or (item.name_en or "").strip()
        if kind and name and symbol not in existing:
            rows.append(_row("US", symbol, name, item.name_en, kind))
    return rows


async def _load(db, model) -> list[Any]:
    return list(
        (
            await db.scalars(
                select(model).where(
                    model.is_active.is_(True),
                    model.listing_status.is_distinct_from("DELISTED"),
                )
            )
        ).all()
    )


async def _existing(db, market: str) -> set[str]:
    return set(
        (
            await db.scalars(
                select(SymbolMaster.symbol).where(SymbolMaster.market == market)
            )
        ).all()
    )


async def main(commit: bool) -> None:
    async with AsyncSessionLocal() as db:
        rows = build_kr_rows(
            await _load(db, KRSymbolUniverse), await _existing(db, "KRX")
        ) + build_us_rows(await _load(db, USSymbolUniverse), await _existing(db, "US"))
        counts = Counter((r["market"], r["security_type"]) for r in rows)
        print(f"추가 대상 {len(rows)}건 {dict(counts)}")
        print("샘플", [(r["market"], r["symbol"], r["name"]) for r in rows[:10]])
        if not commit:
            print("dry-run: 쓰지 않음 (--commit 으로 저장)")
            return
        saved = 0
        for start in range(0, len(rows), 500):
            result = await db.execute(
                insert(SymbolMaster)
                .values(rows[start : start + 500])
                .on_conflict_do_nothing(index_elements=["market", "symbol"])
                .returning(SymbolMaster.symbol)
            )
            saved += len(result.all())
        await db.commit()
        print(f"저장 {saved}건")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true")
    asyncio.run(main(parser.parse_args().commit))
