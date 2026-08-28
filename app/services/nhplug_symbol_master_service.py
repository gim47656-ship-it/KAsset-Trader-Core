from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import cast

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.core.symbol import to_db_symbol
from app.models.symbol_master import SymbolMaster

logger = logging.getLogger(__name__)

_MASTER_BASE_URL = "https://www.nhplug.com/instruments"
_DOMESTIC_MASTER_FILE = "m_new_stock.mst"
_GLOBAL_MASTER_FILE = "m_gtsstock.mst"

# Verified against the vendor structure files published at:
# https://www.nhplug.com/instruments/m_new_stock.h
# https://www.nhplug.com/instruments/m_gtsstock.h
_DOMESTIC_RECORD_SIZE = 237
_GLOBAL_RECORD_SIZE = 164


@dataclass(frozen=True, slots=True)
class SymbolMasterRecord:
    market: str
    symbol: str
    name: str
    name_en: str | None
    security_type: str


def _decode_field(record: bytes, offset: int, length: int) -> str:
    return record[offset : offset + length].decode("cp949", errors="replace").rstrip()


def _fixed_records(payload: bytes, record_size: int, source: str):
    remainder = len(payload) % record_size
    if remainder:
        raise ValueError(
            f"{source} record size mismatch: bytes={len(payload)} "
            f"record_size={record_size} remainder={remainder}"
        )
    for offset in range(0, len(payload), record_size):
        record = payload[offset : offset + record_size]
        if record[-1:] != b"\n":
            raise ValueError(f"{source} record terminator mismatch at offset={offset}")
        yield record


def parse_domestic_master(payload: bytes) -> list[SymbolMasterRecord]:
    """Parse tradable KRX common stocks and ETFs from ``m_new_stock.mst``."""

    rows: list[SymbolMasterRecord] = []
    for record in _fixed_records(
        payload, _DOMESTIC_RECORD_SIZE, _DOMESTIC_MASTER_FILE
    ):
        symbol = _decode_field(record, 0, 6).upper()
        market_code = _decode_field(record, 6, 1)
        name = _decode_field(record, 7, 41)
        name_en = _decode_field(record, 48, 41) or None
        is_managed = _decode_field(record, 160, 1) == "Y"
        is_suspended = _decode_field(record, 161, 1) == "Y"
        security_group = _decode_field(record, 165, 1)
        is_liquidation = _decode_field(record, 189, 1) == "Y"

        if name[:1] in {"*", "#"}:
            name = name[1:].lstrip()
        if (
            market_code not in {"1", "4"}
            or len(symbol) != 6
            or not symbol.isalnum()
            or not name
            or is_managed
            or is_suspended
            or is_liquidation
        ):
            continue

        if security_group == "8":
            security_type = "ETF"
        elif symbol.endswith("0"):
            security_type = "COMMON_STOCK"
        else:
            continue

        rows.append(
            SymbolMasterRecord(
                market="KRX",
                symbol=symbol,
                name=name,
                name_en=name_en,
                security_type=security_type,
            )
        )
    return rows


def parse_global_master(payload: bytes) -> list[SymbolMasterRecord]:
    """Parse tradable US-listed common stocks and ETFs from ``m_gtsstock.mst``."""

    rows: list[SymbolMasterRecord] = []
    for record in _fixed_records(payload, _GLOBAL_RECORD_SIZE, _GLOBAL_MASTER_FILE):
        name_kr = _decode_field(record, 15, 40)
        name_en = _decode_field(record, 55, 40)
        nation = _decode_field(record, 95, 3)
        symbol = to_db_symbol(_decode_field(record, 98, 12)).upper()
        issue_type = _decode_field(record, 125, 2)
        is_tradable = _decode_field(record, 137, 1) == "1"

        if (
            nation != "USA"
            or issue_type not in {"01", "12"}
            or not is_tradable
            or not symbol
            or len(symbol) > 32
            or not symbol.isascii()
            or any(character.isspace() for character in symbol)
        ):
            continue

        name = name_kr or name_en
        if not name:
            continue
        rows.append(
            SymbolMasterRecord(
                market="US",
                symbol=symbol,
                name=name,
                name_en=name_en or None,
                security_type="ETF" if issue_type == "12" else "COMMON_STOCK",
            )
        )
    return rows


async def _download_master(client: httpx.AsyncClient, filename: str) -> bytes:
    response = await client.get(f"{_MASTER_BASE_URL}/{filename}")
    response.raise_for_status()
    return response.content


async def build_nhplug_symbol_master_snapshot() -> dict[tuple[str, str], SymbolMasterRecord]:
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        domestic_payload, global_payload = await asyncio.gather(
            _download_master(client, _DOMESTIC_MASTER_FILE),
            _download_master(client, _GLOBAL_MASTER_FILE),
        )

    domestic_rows = parse_domestic_master(domestic_payload)
    us_rows = parse_global_master(global_payload)
    logger.info(
        "NH PLUG symbol master parsed domestic=%d us=%d",
        len(domestic_rows),
        len(us_rows),
    )
    return {(row.market, row.symbol): row for row in (*domestic_rows, *us_rows)}


async def _apply_snapshot(
    db: AsyncSession,
    snapshot: dict[tuple[str, str], SymbolMasterRecord],
    *,
    dry_run: bool,
) -> dict[str, int]:
    existing_result = await db.execute(select(SymbolMaster))
    existing_rows = {
        (row.market, row.symbol): row for row in existing_result.scalars().all()
    }
    inserted = 0
    updated = 0
    deactivated = 0

    for key, incoming in snapshot.items():
        existing = existing_rows.get(key)
        if existing is None:
            inserted += 1
            if not dry_run:
                db.add(
                    SymbolMaster(
                        market=incoming.market,
                        symbol=incoming.symbol,
                        name=incoming.name,
                        name_en=incoming.name_en,
                        security_type=incoming.security_type,
                        is_active=True,
                    )
                )
            continue

        changed = (
            existing.name != incoming.name
            or existing.name_en != incoming.name_en
            or existing.security_type != incoming.security_type
            or not existing.is_active
        )
        if not changed:
            continue
        updated += 1
        if not dry_run:
            existing.name = incoming.name
            existing.name_en = incoming.name_en
            existing.security_type = incoming.security_type
            existing.is_active = True

    current_keys = set(snapshot)
    for key, existing in existing_rows.items():
        if key in current_keys or not existing.is_active:
            continue
        deactivated += 1
        if not dry_run:
            existing.is_active = False

    if not dry_run:
        await db.flush()
    return {
        "total": len(snapshot),
        "krx": sum(1 for market, _symbol in snapshot if market == "KRX"),
        "us": sum(1 for market, _symbol in snapshot if market == "US"),
        "inserted": inserted,
        "updated": updated,
        "deactivated": deactivated,
    }


async def sync_nhplug_symbol_master(
    db: AsyncSession | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    snapshot = await build_nhplug_symbol_master_snapshot()
    if db is not None:
        return await _apply_snapshot(db, snapshot, dry_run=dry_run)

    session = cast(AsyncSession, cast(object, AsyncSessionLocal()))
    try:
        if dry_run:
            result = await _apply_snapshot(session, snapshot, dry_run=True)
        else:
            async with session.begin():
                result = await _apply_snapshot(session, snapshot, dry_run=False)
    finally:
        await session.close()

    logger.info(
        "NH PLUG symbol master %s total=%d krx=%d us=%d inserted=%d updated=%d "
        "deactivated=%d",
        "dry-run" if dry_run else "synced",
        result["total"],
        result["krx"],
        result["us"],
        result["inserted"],
        result["updated"],
        result["deactivated"],
    )
    return result


__all__ = [
    "SymbolMasterRecord",
    "build_nhplug_symbol_master_snapshot",
    "parse_domestic_master",
    "parse_global_master",
    "sync_nhplug_symbol_master",
]
