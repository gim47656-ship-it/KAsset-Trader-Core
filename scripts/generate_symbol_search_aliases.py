"""Generate search aliases through the configured MCP sidecar; dry-run by default."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.ai.mcp_provider import McpStructuredJsonClient
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.symbol_master import SymbolMaster
from app.models.symbol_search_alias import SymbolSearchAlias

MODEL = "gpt-6-luna"
SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "aliases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 6,
                    },
                },
                "required": ["symbol", "aliases"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}
INSTRUCTIONS = (
    "한국 투자자가 실제 검색창에 입력하는 약칭, 영문, 한글 발음, 티커를 종목당 최대 6개 제안하세요. "
    "틀린 종목 매핑은 절대 하지 마세요. 확실하지 않으면 aliases를 빈 배열로 반환하세요. "
    "레버리지·인버스 ETF에는 기초자산 티커를 포함하세요. 입력 symbol을 그대로 반환하세요."
)


@dataclass
class Summary:
    targets: int = 0
    calls: int = 0
    failures: int = 0
    saved: int = 0
    sample: list[tuple[str, str, str]] | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=("KRX", "US", "ALL"), default="ALL")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--skip-existing", action=argparse.BooleanOptionalAction, default=True
    )
    args = parser.parse_args(argv)
    if args.limit < 1 or args.batch_size < 1:
        parser.error("--limit and --batch-size must be positive")
    return args


def normalize_response(
    payload: object, batch: list[SymbolMaster]
) -> list[dict[str, str]]:
    """Reject malformed output, unknown symbols and aliases shared by 3+ securities."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"items"}
        or not isinstance(payload["items"], list)
    ):
        raise ValueError("invalid alias batch response")
    requested = {row.symbol: row for row in batch}
    aliases_by_symbol: dict[str, set[str]] = {}
    for item in payload["items"]:
        if not isinstance(item, dict) or set(item) != {"symbol", "aliases"}:
            raise ValueError("invalid alias item")
        symbol, aliases = item["symbol"], item["aliases"]
        if (
            not isinstance(symbol, str)
            or not isinstance(aliases, list)
            or len(aliases) > 6
            or not all(isinstance(a, str) for a in aliases)
        ):
            raise ValueError("invalid alias values")
        if symbol not in requested:
            continue
        row = requested[symbol]
        existing = aliases_by_symbol.setdefault(symbol, set())
        own_names = {
            "".join(value.lower().split())
            for value in (row.symbol, row.name, row.name_en or "")
        }
        for alias in aliases:
            compact = "".join(alias.lower().split())
            if 2 <= len(compact) <= 30 and compact not in own_names:
                existing.add(compact)
    counts = Counter(
        alias for aliases in aliases_by_symbol.values() for alias in aliases
    )
    return [
        {
            "market": row.market,
            "symbol": row.symbol,
            "alias": alias,
            "source": "llm_batch",
            "model": MODEL,
        }
        for row in batch
        for alias in sorted(aliases_by_symbol.get(row.symbol, set()))
        if counts[alias] < 3
    ]


async def select_targets(
    db: AsyncSession, args: argparse.Namespace
) -> tuple[list[SymbolMaster], str]:
    stmt = select(SymbolMaster).where(SymbolMaster.is_active.is_(True))
    markets = ("KRX", "US") if args.market == "ALL" else (args.market,)
    stmt = stmt.where(SymbolMaster.market.in_(markets))
    if args.symbol:
        stmt = stmt.where(
            SymbolMaster.symbol.in_({symbol.strip().upper() for symbol in args.symbol})
        )
    if args.skip_existing:
        stmt = stmt.where(
            ~select(SymbolSearchAlias.id)
            .where(
                SymbolSearchAlias.market == SymbolMaster.market,
                SymbolSearchAlias.symbol == SymbolMaster.symbol,
            )
            .exists()
        )
    rows = list((await db.scalars(stmt)).all())
    if args.symbol:
        return sorted(rows, key=lambda row: (row.market, row.symbol))[
            : args.limit
        ], "지정 symbol 순"

    connection = await db.connection()
    has_snapshot = await connection.run_sync(
        lambda sync: inspect(sync).has_table("invest_screener_snapshots")
    )
    if not has_snapshot:
        return sorted(rows, key=lambda row: (row.market, row.symbol))[
            : args.limit
        ], "거래대금 테이블 없음: symbol 순"

    totals: dict[tuple[str, str], Any] = {}
    for market in markets:
        snapshot_market = market.lower()
        latest_dates = (
            select(InvestScreenerSnapshot.snapshot_date)
            .where(
                InvestScreenerSnapshot.market == snapshot_market,
                InvestScreenerSnapshot.snapshot_date
                >= date.today() - timedelta(days=40),
            )
            .distinct()
            .order_by(InvestScreenerSnapshot.snapshot_date.desc())
            .limit(20)
        )
        result = await db.execute(
            select(
                InvestScreenerSnapshot.symbol,
                func.sum(InvestScreenerSnapshot.daily_turnover),
            )
            .where(
                InvestScreenerSnapshot.market == snapshot_market,
                InvestScreenerSnapshot.snapshot_date
                >= date.today() - timedelta(days=40),
                InvestScreenerSnapshot.snapshot_date.in_(latest_dates),
            )
            .group_by(InvestScreenerSnapshot.symbol)
        )
        totals.update(
            {
                (market, symbol): amount
                for symbol, amount in result
                if amount is not None
            }
        )
    rows.sort(
        key=lambda row: (
            -(totals.get((row.market, row.symbol)) or 0),
            row.market,
            row.symbol,
        )
    )
    return (
        rows[: args.limit],
        "최근 20거래일 invest_screener_snapshots.daily_turnover 합계; 데이터 없는 시장은 symbol 순",
    )


async def generate(
    db: AsyncSession, client: McpStructuredJsonClient, args: argparse.Namespace
) -> Summary:
    targets, ordering = await select_targets(db, args)
    print(f"선정 근거: {ordering}")
    summary = Summary(targets=len(targets), sample=[])
    batches = [
        market_rows[start : start + args.batch_size]
        for market in ("KRX", "US")
        for market_rows in ([row for row in targets if row.market == market],)
        for start in range(0, len(market_rows), args.batch_size)
    ]
    for batch in batches:
        summary.calls += 1
        try:
            payload = await client.request_json(
                model=MODEL,
                input_payload={
                    "items": [
                        {
                            "symbol": row.symbol,
                            "market": row.market,
                            "name": row.name,
                            "name_en": row.name_en,
                            "security_type": row.security_type,
                        }
                        for row in batch
                    ]
                },
                reasoning_effort="low",
                schema_name="symbol_search_aliases",
                schema=SCHEMA,
                additional_instructions=INSTRUCTIONS,
            )
            values = normalize_response(payload, batch)
        except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
            summary.failures += 1
            print(f"배치 {summary.calls} 실패: {type(exc).__name__}")
            continue
        summary.sample.extend(
            (value["market"], value["symbol"], value["alias"])
            for value in values[: max(0, 10 - len(summary.sample))]
        )
        if args.commit:
            if values:
                statement = (
                    insert(SymbolSearchAlias)
                    .values(values)
                    .on_conflict_do_nothing(constraint="uq_symbol_search_aliases_key")
                    .returning(SymbolSearchAlias.id)
                )
                summary.saved += len((await db.scalars(statement)).all())
            await db.commit()
        else:
            print(f"배치 {summary.calls} 미리보기: {len(values)}행")
    print(
        f"대상 {summary.targets} 호출 {summary.calls} 실패 {summary.failures} 저장 {summary.saved}"
    )
    print(f"샘플 {summary.sample}")
    return summary


async def main(args: argparse.Namespace) -> None:
    url = settings.KASSET_AI_MCP_URL.strip()
    if not url:
        raise SystemExit("KASSET_AI_MCP_URL is required")
    token = settings.KASSET_AI_MCP_TOKEN
    client = McpStructuredJsonClient(
        url=url,
        token=token.get_secret_value() if token is not None else None,
        tool_name=settings.KASSET_AI_MCP_TOOL_NAME,
        timeout_seconds=settings.KASSET_AI_MCP_TIMEOUT_SECONDS,
    )
    async with AsyncSessionLocal() as db:
        await generate(db, client, args)


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
