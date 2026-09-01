#!/usr/bin/env python3
"""비활성 KIS↔Toss 시세 비교 규칙을 확인하는 운영자 도구.

KIS가 운영 경로에서 제거되었으므로 이 도구는 어떤 공급자도 구성하거나
호출하지 않는다. 대상 유니버스 크기와 ``disabled``/``blocked`` 상태만
출력하며, Toss 단독 비교로 과거 승격 기준을 약화하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.symbol import to_db_symbol
from app.models.manual_holdings import MarketType
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.quote_parity_shadow import run_quote_parity_probe

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_SENSITIVE_HEADER_RE = re.compile(
    r"(cookie|authorization|x[-_]?csrf|token|secret|password|session)", re.I
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(bearer\s+[A-Za-z0-9._~+/-]+|cookie\s*:|authorization\s*:"
    r"|token=|secret=|password=|session=)",
    re.I,
)


def _reject_if_sensitive(label: str, value: Any) -> None:
    text = str(value or "")
    if _SENSITIVE_HEADER_RE.search(label) or _SENSITIVE_VALUE_RE.search(text):
        raise ValueError(
            "--symbols-file must not contain cookies, headers, tokens, or "
            "secrets; remove sensitive fields and retry."
        )


def load_symbols_file(path: Path) -> tuple[list[str], list[str]]:
    """비밀값을 거부하고 KR/US 심볼을 중복 없이 읽는다."""

    raw_text = path.read_text(encoding="utf-8-sig")
    _reject_if_sensitive("file", raw_text)
    rows: list[dict[str, Any]]
    if path.suffix.lower() == ".json":
        parsed = json.loads(raw_text)
        if not isinstance(parsed, list):
            raise ValueError(
                "--symbols-file JSON must be a list of {market, symbol} rows"
            )
        rows = [row for row in parsed if isinstance(row, dict)]
    else:
        reader = csv.DictReader(raw_text.splitlines())
        for field in reader.fieldnames or []:
            _reject_if_sensitive(field, "")
        rows = list(reader)

    kr: list[str] = []
    us: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        for key, value in row.items():
            _reject_if_sensitive(str(key), value)
        market = str(row.get("market") or "").strip().upper()
        symbol_raw = str(row.get("symbol") or "").strip()
        if not symbol_raw or market not in {"KR", "US"}:
            continue
        symbol = to_db_symbol(symbol_raw)
        key = (market, symbol)
        if key in seen:
            continue
        seen.add(key)
        (kr if market == "KR" else us).append(symbol)
    return kr, us


async def enumerate_db_universe(
    session: AsyncSession, *, user_id: int, limit: int
) -> tuple[list[str], list[str]]:
    """소유자 범위 Toss 수동 보유 심볼을 시장별로 읽는다."""

    holdings = await ManualHoldingsService(session).get_holdings_by_user(
        user_id, broker_type="toss"
    )
    kr = [
        to_db_symbol(row.ticker) for row in holdings if row.market_type == MarketType.KR
    ]
    us = [
        to_db_symbol(row.ticker) for row in holdings if row.market_type == MarketType.US
    ]
    return kr[:limit], us[:limit]


def exit_code_for(decision: str) -> int:
    return {"go": 0, "no_go": 2, "blocked": 2}.get(decision, 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="비활성 KIS/Toss 시세 비교 규칙 상태를 출력합니다."
    )
    parser.add_argument(
        "--symbols-file",
        type=Path,
        default=None,
        help="CSV/JSON {market, symbol} 목록. --user-id보다 우선합니다.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="ManualHoldingsService에서 소유자 유니버스를 읽습니다.",
    )
    parser.add_argument("--limit", type=int, default=200, help="시장별 최대 심볼 수")
    return parser.parse_args(argv)


async def _resolve_universe(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    if args.symbols_file is not None:
        return load_symbols_file(args.symbols_file)
    if args.user_id is not None:
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            return await enumerate_db_universe(
                session, user_id=args.user_id, limit=args.limit
            )
    raise ValueError("Provide --symbols-file or --user-id to enumerate a universe")


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        kr_symbols, us_symbols = await _resolve_universe(args)
        report = await run_quote_parity_probe(
            kr_symbols=kr_symbols,
            us_symbols=us_symbols,
        )
        report["mode"] = "disabled"
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return exit_code_for(report["go_no_go"]["decision"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("quote_parity_shadow_probe failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
