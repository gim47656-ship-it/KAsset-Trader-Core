"""Toss/Upbit 및 사용자 수동 보유분의 read-only 포트폴리오 스냅샷.

KR/US ``toss_live``는 Toss 일반 포트폴리오 스냅샷을 primary source로
사용한다. 이 경로의 수량은 매도가능 근거가 아니므로 sellable 조회와
``sellable_summary``를 사용하지 않는다. 수동 보유분은 reference로만
노출하며 primary NAV에 합산하지 않는다.

``crypto + upbit_live``와 비-live 수동 primary 계약은 그대로 유지한다.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import BrokerAccount, ManualHolding, MarketType
from app.schemas.invest_home import Holding
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
from app.services.toss_portfolio_service import (
    TossPortfolioPosition,
    fetch_toss_portfolio_snapshot,
)

logger = logging.getLogger(__name__)

_MARKET_TO_TYPES: dict[str, tuple[MarketType, ...]] = {
    "kr": (MarketType.KR,),
    "us": (MarketType.US,),
    "crypto": (MarketType.CRYPTO,),
}

# Toss 일반 스냅샷은 KR/US 포지션을 한 번에 반환하므로 요청 시장별
# instrument_type으로 분리한다.
_REQUEST_MARKET_TO_INSTRUMENT_TYPE: dict[str, str] = {
    "kr": "equity_kr",
    "us": "equity_us",
}


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _manual_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "ticker": row.ticker,
        "market_type": (
            row.market_type.value
            if isinstance(row.market_type, MarketType)
            else str(row.market_type)
        ),
        "quantity": row.quantity,
        "avg_price": row.avg_price,
        "display_name": row.display_name,
        "updated_at": row.updated_at,
        "source": "manual",
    }


def _classify_fetch_status(
    holdings_dicts: list[dict[str, Any]],
    account: Any | None,
    fetch_warnings: list[str],
) -> str:
    """Classify a live read-only fetch as ``ok`` / ``partial`` / ``failed``.

    Shared by live account readers so status semantics stay consistent:

    * ``failed`` — nothing usable returned (no holdings AND no account), or
      warnings present with no holdings (data-quality gate).
    * ``partial`` — holdings present but the reader flagged a warning.
    * ``ok`` — holdings (and/or account) present with no warnings.
    """
    if not holdings_dicts and account is None:
        return "failed"
    if fetch_warnings and not holdings_dicts:
        return "failed"
    if fetch_warnings:
        return "partial"
    return "ok"


def _krw_or_zero(value: float | None) -> float:
    """Coerce an Upbit KRW figure to an explicit float.

    Upbit에서 KRW row 부재는 실제 0 KRW를 뜻한다. 명시적 ``0.0``을
    내보내 portfolio stage의 ``$.buying_power.krw`` citation이 null을
    가리키지 않게 한다.
    """
    return float(value) if value is not None else 0.0


def _reader_holding_to_dict(h: Holding) -> dict[str, Any]:
    """Upbit read-only Holding을 snapshot dict shape으로 변환한다."""
    return {
        "ticker": h.symbol,
        "market": h.market,
        "asset_type": h.assetType,
        "asset_category": h.assetCategory,
        "quantity": h.quantity,
        "avg_price": h.averageCost,
        "cost_basis": h.costBasis,
        "currency": h.currency,
        "display_name": h.displayName,
        "value_native": h.valueNative,
        "value_krw": h.valueKrw,
        "pnl_krw": h.pnlKrw,
        "pnl_rate": h.pnlRate,
        "sellable_quantity": h.sellableQuantity,
        "pending_sell_quantity": h.pendingSellQuantity,
        "source": h.source,
    }


def _float_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None


def _toss_position_to_dict(position: TossPortfolioPosition) -> dict[str, Any]:
    """Toss 일반 포지션을 매도가능 근거 없이 snapshot shape으로 변환한다."""
    return {
        "ticker": position.symbol,
        "market": position.market.upper(),
        "asset_type": "equity",
        "asset_category": (
            "kr_stock" if position.instrument_type == "equity_kr" else "us_stock"
        ),
        "quantity": float(position.quantity),
        "avg_price": float(position.avg_buy_price),
        "cost_basis": float(position.quantity * position.avg_buy_price),
        "currency": "KRW" if position.instrument_type == "equity_kr" else "USD",
        "display_name": position.name,
        "value_native": _float_or_none(position.evaluation_amount),
        "value_krw": (
            _float_or_none(position.evaluation_amount)
            if position.instrument_type == "equity_kr"
            else None
        ),
        "pnl_krw": (
            _float_or_none(position.profit_loss)
            if position.instrument_type == "equity_kr"
            else None
        ),
        "pnl_rate": _float_or_none(position.profit_rate),
        "sellable_quantity": None,
        "pending_sell_quantity": None,
        "source": "toss_api",
    }


def _apply_kr_name_fallback(
    rows: list[dict[str, Any]], name_map: dict[str, str]
) -> None:
    """Fill ``display_name`` for rows whose name is missing or equals the code.

    In-place. A row is fixed only when ``name_map`` has a real name for its
    ``ticker``; otherwise the code is kept (never fabricate a name).
    """
    for row in rows:
        ticker = row.get("ticker")
        if not isinstance(ticker, str):
            continue
        current = row.get("display_name")
        is_code_as_name = not current or current == ticker
        if is_code_as_name and ticker in name_map:
            row["display_name"] = name_map[ticker]


class PortfolioSnapshotCollector:
    """Toss/Upbit live와 수동 reference를 합성하는 portfolio collector."""

    snapshot_kind: str = "portfolio"

    def __init__(
        self,
        session: AsyncSession,
        *,
        toss_snapshot_fetcher: Any | None = None,
        upbit_reader: Any | None = None,
    ) -> None:
        self._session = session
        self._toss_snapshot_fetcher = (
            toss_snapshot_fetcher or fetch_toss_portfolio_snapshot
        )
        self._upbit_reader = upbit_reader

    def _get_upbit_reader(self) -> Any:
        if self._upbit_reader is not None:
            return self._upbit_reader
        from app.services.invest_home_readers import UpbitHomeReader

        self._upbit_reader = UpbitHomeReader(self._session)
        return self._upbit_reader

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        market_types = _MARKET_TO_TYPES.get(request.market)
        now = utcnow()
        if request.account_scope in {"kis_live", "kis_mock"}:
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason="provider kis is not operational",
                    as_of=now,
                )
            ]
        if not market_types:
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"no portfolio mapping for market={request.market!r}",
                    as_of=now,
                )
            ]

        if request.account_scope == "toss_live" and request.market in (
            "kr",
            "us",
        ):
            return await self._collect_toss_live(request, market_types, now=now)
        # ROB-369 E9 — crypto + upbit_live reads the live Upbit account so the
        # portfolio stage gets real NAV / cash / orderable instead of the
        # manual-primary empty payload that produced "NAV=0".
        if request.account_scope == "upbit_live" and request.market == "crypto":
            return await self._collect_upbit_live(request, market_types, now=now)
        return await self._collect_manual_primary(request, market_types, now=now)

    async def _collect_manual_primary(
        self,
        request: CollectorRequest,
        market_types: tuple[MarketType, ...],
        *,
        now: dt.datetime,
    ) -> list[SnapshotCollectResult]:
        manual_rows = await self._read_manual_rows(
            market_types, user_id=request.user_id
        )
        holdings = [_manual_row_to_dict(r) for r in manual_rows]
        payload: dict[str, Any] = {
            "holdings": holdings,
            "count": len(holdings),
            "market": request.market,
            "primary_source": "manual",
            "reference_holdings": [],
            "cash": None,
            "buying_power": None,
            "sellable_summary": None,
            "provenance": {
                "toss_fetch_status": "skipped",
                "account_scope": request.account_scope,
                "fetched_at": _iso(now),
                "warnings": [],
                "errors": [],
            },
        }
        if not holdings:
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"holdings_found": False},
                )
            ]
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                coverage={"holdings_count": len(holdings)},
            )
        ]

    async def _collect_toss_live(
        self,
        request: CollectorRequest,
        market_types: tuple[MarketType, ...],
        *,
        now: dt.datetime,
    ) -> list[SnapshotCollectResult]:
        """KR/US Toss 일반 포트폴리오를 primary로 수집한다."""
        manual_rows = await self._read_manual_rows(
            market_types, user_id=request.user_id
        )
        reference_holdings = [_manual_row_to_dict(row) for row in manual_rows]

        if request.user_id is None:
            payload: dict[str, Any] = {
                "holdings": [],
                "count": 0,
                "market": request.market,
                "primary_source": "none",
                "reference_holdings": reference_holdings,
                "cash": None,
                "buying_power": None,
                "sellable_summary": None,
                "provenance": {
                    "toss_fetch_status": "skipped",
                    "account_scope": request.account_scope,
                    "fetched_at": _iso(now),
                    "warnings": [],
                    "errors": [],
                },
            }
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="unavailable",
                    coverage={"holdings_count": 0},
                    errors={
                        "reason_code": "user_id_missing",
                        "reason": (
                            "toss_live portfolio requires explicit user_id; "
                            "none supplied"
                        ),
                    },
                )
            ]

        fetch_warnings: list[str] = []
        fetch_errors: list[str] = []
        toss_snapshot: Any = None
        try:
            toss_snapshot = await self._toss_snapshot_fetcher(
                need_sellable=False,
                need_cash=True,
            )
        except Exception as exc:  # noqa: BLE001 - unavailable snapshot, not crash
            logger.warning(
                "Toss read-only portfolio fetch failed (%s)",
                type(exc).__name__,
                exc_info=True,
            )
            fetch_errors.append(type(exc).__name__)

        toss_holdings: list[dict[str, Any]] = []
        buying_power_payload: dict[str, Any] | None = None
        if toss_snapshot is None:
            toss_fetch_status = "failed"
        else:
            instrument_type = _REQUEST_MARKET_TO_INSTRUMENT_TYPE[request.market]
            positions = [
                position
                for position in (toss_snapshot.positions or [])
                if position.instrument_type == instrument_type
            ]
            toss_holdings = [_toss_position_to_dict(position) for position in positions]
            for error in toss_snapshot.errors or []:
                code = error.get("code") if isinstance(error, dict) else None
                fetch_warnings.append(str(code or "toss_snapshot_partial"))
            buying_power_payload = {
                "krw": _float_or_none(toss_snapshot.cash_krw),
                "usd": _float_or_none(toss_snapshot.cash_usd),
            }
            has_account_evidence = (
                toss_snapshot.cash_krw is not None or toss_snapshot.cash_usd is not None
            )
            if fetch_warnings and not toss_holdings and not has_account_evidence:
                toss_fetch_status = "failed"
            elif fetch_warnings:
                toss_fetch_status = "partial"
            else:
                toss_fetch_status = "ok"

        if toss_fetch_status == "failed":
            primary_source = "none"
            holdings_out: list[dict[str, Any]] = []
            buying_power_payload = None
            freshness = "unavailable"
        else:
            primary_source = "toss"
            holdings_out = toss_holdings
            freshness = "fresh" if toss_fetch_status == "ok" else "partial"

        if request.market == "kr":
            name_rows = [*holdings_out, *reference_holdings]
            need = sorted(
                {
                    row["ticker"]
                    for row in name_rows
                    if isinstance(row.get("ticker"), str)
                    and (
                        not row.get("display_name")
                        or row.get("display_name") == row["ticker"]
                    )
                }
            )
            if need:
                try:
                    name_map = await get_kr_names_by_symbols(need, db=self._session)
                except Exception:  # noqa: BLE001 - best-effort display name
                    name_map = {}
                _apply_kr_name_fallback(name_rows, name_map)

        payload = {
            "holdings": holdings_out,
            "count": len(holdings_out),
            "market": request.market,
            "primary_source": primary_source,
            "reference_holdings": reference_holdings,
            # Toss snapshot value is cash buying power, not settled-cash evidence.
            "cash": None,
            "buying_power": buying_power_payload,
            # 일반 snapshot 수량은 매도가능 권한 근거가 아니다.
            "sellable_summary": None,
            "provenance": {
                "toss_fetch_status": toss_fetch_status,
                "account_scope": request.account_scope,
                "fetched_at": _iso(now),
                "warnings": fetch_warnings,
                "errors": fetch_errors,
            },
        }
        if request.market == "kr":
            payload["nav_scope"] = "toss_primary_general_snapshot"
            payload["nav_scope_label"] = (
                "NAV는 Toss 일반 보유 + 주문가능 현금 기준 · "
                "매도가능 수량 근거로 사용하지 않음 · "
                "수동 참조분(reference_holdings)은 제외"
            )

        coverage = {
            "holdings_count": len(holdings_out),
            "reference_count": len(reference_holdings),
            "toss_fetch_status": toss_fetch_status,
        }
        if freshness == "unavailable":
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="unavailable",
                    coverage=coverage,
                    errors={
                        "reason_code": "toss_fetch_failed",
                        "reason": "Toss live portfolio fetch failed",
                        "warnings": fetch_warnings,
                        "errors": fetch_errors,
                    },
                )
            ]

        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                freshness_status=freshness,
                coverage=coverage,
            )
        ]

    async def _collect_upbit_live(
        self,
        request: CollectorRequest,
        market_types: tuple[MarketType, ...],
        *,
        now: dt.datetime,
    ) -> list[SnapshotCollectResult]:
        """``crypto + upbit_live``의 live read-only 경로.

        Upbit holdings와 KRW cash/orderable은 ``primary_source="upbit"``로,
        수동 ``CRYPTO`` rows는 ``reference_holdings``로만 노출한다. 실패는
        수동 값을 primary로 승격하지 않고 ``freshness="unavailable"``로
        반환한다. Upbit 계정은 사용자별 provider credential이 아니므로
        ``user_id``가 없으면 reader에는 ``0``을 넘긴다. Crypto에는
        sellable/pending-sell 개념이 없어 ``sellable_summary=None``이다.
        """
        manual_rows = await self._read_manual_rows(
            market_types, user_id=request.user_id
        )
        reference_holdings = [_manual_row_to_dict(r) for r in manual_rows]

        reader = self._get_upbit_reader()
        fetch_warnings: list[str] = []
        fetch_errors: list[str] = []
        upbit_result: Any = None
        try:
            upbit_result = await reader.fetch(user_id=request.user_id or 0)
        except Exception as exc:  # noqa: BLE001 — collector must never crash
            logger.warning("Upbit read-only fetch failed: %s", exc, exc_info=True)
            fetch_errors.append(f"{type(exc).__name__}: {exc}")

        upbit_holdings_dicts: list[dict[str, Any]] = []
        cash_payload: dict[str, Any] | None = None
        buying_power_payload: dict[str, Any] | None = None
        upbit_fetch_status: str
        if upbit_result is None:
            upbit_fetch_status = "failed"
        else:
            holdings = list(upbit_result.holdings or [])
            upbit_holdings_dicts = [_reader_holding_to_dict(h) for h in holdings]
            if upbit_result.warning is not None:
                fetch_warnings.append(
                    f"{upbit_result.warning.source}: {upbit_result.warning.message}"
                )
            account = next(iter(upbit_result.accounts or []), None)
            if account is not None:
                # Upbit는 KRW-only 계정이며 consumer shape 일관성을 위해
                # 두 currency key를 낸다. None은 명시적 0 KRW로 정규화한다.
                cash_payload = {
                    "krw": _krw_or_zero(account.cashBalances.krw),
                    "usd": account.cashBalances.usd,
                }
                buying_power_payload = {
                    "krw": _krw_or_zero(account.buyingPower.krw),
                    "usd": account.buyingPower.usd,
                }
            upbit_fetch_status = _classify_fetch_status(
                upbit_holdings_dicts, account, fetch_warnings
            )

        if upbit_fetch_status == "failed":
            primary_source = "none"
            holdings_out: list[dict[str, Any]] = []
            cash_payload = None
            buying_power_payload = None
            freshness = "unavailable"
        else:
            primary_source = "upbit"
            holdings_out = upbit_holdings_dicts
            freshness = "fresh" if upbit_fetch_status == "ok" else "partial"

        payload: dict[str, Any] = {
            "holdings": holdings_out,
            "count": len(holdings_out),
            "market": request.market,
            "primary_source": primary_source,
            "reference_holdings": reference_holdings,
            "cash": cash_payload,
            "buying_power": buying_power_payload,
            "sellable_summary": None,
            "provenance": {
                "upbit_fetch_status": upbit_fetch_status,
                "account_scope": request.account_scope,
                "fetched_at": _iso(now),
                "warnings": fetch_warnings,
                "errors": fetch_errors,
            },
        }

        coverage = {
            "holdings_count": len(holdings_out),
            "reference_count": len(reference_holdings),
            "upbit_fetch_status": upbit_fetch_status,
        }
        # Surface the reader's dust (<5000 KRW) / inactive filtering so the
        # snapshot NAV's divergence from the raw Upbit eval is auditable rather
        # than a silent drop.
        hidden_counts = getattr(upbit_result, "hidden_counts", None)
        if hidden_counts is not None:
            coverage["hidden_dust_count"] = getattr(hidden_counts, "upbitDust", 0)
            coverage["hidden_inactive_count"] = getattr(
                hidden_counts, "upbitInactive", 0
            )

        if freshness == "unavailable":
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="unavailable",
                    coverage=coverage,
                    errors={
                        "reason_code": "upbit_fetch_failed",
                        "reason": "Upbit live portfolio fetch failed",
                        "warnings": fetch_warnings,
                        "errors": fetch_errors,
                    },
                )
            ]

        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                freshness_status=freshness,
                coverage=coverage,
            )
        ]

    async def _read_manual_rows(
        self,
        market_types: tuple[MarketType, ...],
        *,
        user_id: int | None,
    ) -> list[Any]:
        if user_id is None:
            return []
        stmt = (
            select(ManualHolding)
            .join(
                BrokerAccount,
                ManualHolding.broker_account_id == BrokerAccount.id,
            )
            .where(
                ManualHolding.market_type.in_(market_types),
                BrokerAccount.user_id == user_id,
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
