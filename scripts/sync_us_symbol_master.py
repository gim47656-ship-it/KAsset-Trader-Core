"""Add US securities missing from symbol_master using us_symbol_universe; dry-run by default.

symbol_master is the search/watchlist master but nothing refreshes it since the NH
sync was removed. us_symbol_universe is refreshed daily from Toss. This inserts active
STOCK/ETF/DEPOSITARY_RECEIPT rows that symbol_master lacks. Existing rows are never
updated or deactivated.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.db import AsyncSessionLocal
from app.models.symbol_master import SymbolMaster
from app.models.us_symbol_universe import USSymbolUniverse

# us_symbol_universe.security_type → symbol_master.security_type
SECURITY_TYPES = {
    "STOCK": "COMMON_STOCK",
    "ETF": "ETF",
    "DEPOSITARY_RECEIPT": "DEPOSITARY_RECEIPT",
}


def build_rows(universe: list[USSymbolUniverse], existing: set[str]) -> list[dict]:
    rows = []
    for item in universe:
        security_type = SECURITY_TYPES.get(item.security_type or "")
        symbol = item.symbol.strip().upper()
        name = (item.name_kr or "").strip() or (item.name_en or "").strip()
        if security_type is None or not name or symbol in existing:
            continue
        rows.append(
            {
                "market": "US",
                "symbol": symbol,
                "name": name[:200],
                "name_en": (item.name_en or "").strip()[:200] or None,
                "security_type": security_type,
                "is_active": True,
            }
        )
    return rows


async def main(commit: bool) -> None:
    async with AsyncSessionLocal() as db:
        universe = list(
            (
                await db.scalars(
                    select(USSymbolUniverse).where(
                        USSymbolUniverse.is_active.is_(True),
                        USSymbolUniverse.listing_status.is_distinct_from("DELISTED"),
                    )
                )
            ).all()
        )
        existing = set(
            (
                await db.scalars(
                    select(SymbolMaster.symbol).where(SymbolMaster.market == "US")
                )
            ).all()
        )
        rows = build_rows(universe, existing)
        print(
            f"추가 대상 {len(rows)}건 {dict(Counter(r['security_type'] for r in rows))}"
        )
        print("샘플", [(r["symbol"], r["name"]) for r in rows[:10]])
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
