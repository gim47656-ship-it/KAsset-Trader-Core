"""Portfolio cash-balance helper utilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.core.exceptions import describe_exception
from app.core.timezone import now_kst
from app.mcp_server.tooling.account_modes import toss_live_mutations_enabled
from app.mcp_server.tooling.shared import (
    logger,
    to_float,
)
from app.mcp_server.tooling.shared import (
    normalize_account_filter as _normalize_account_filter,
)
from app.mcp_server.tooling.user_settings_tools import (
    get_manual_cash_setting,
    get_user_setting,
)
from app.services.account_routing import compact_cost_profile
from app.services.exchange_rate_service import get_usd_krw_rate as _get_usd_krw_rate
from app.services.toss_portfolio_service import fetch_toss_cash_snapshot


async def get_account_costs_setting() -> dict[str, Any] | None:
    value = await get_user_setting("account_costs")
    return value if isinstance(value, dict) else None


def is_us_nation_name(value: Any) -> bool:
    normalized = str(value or "").strip().casefold()
    return normalized in {
        "미국",
        "us",
        "usa",
        "united states",
        "united states of america",
    }


def extract_usd_orderable_from_row(row: dict[str, Any] | None) -> float:
    if not isinstance(row, dict):
        return 0.0
    return to_float(row.get("frcr_gnrl_ord_psbl_amt"), default=0.0)


def select_usd_row_for_us_order(
    rows: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not rows:
        return None

    usd_rows = [
        row for row in rows if str(row.get("crcy_cd", "")).strip().upper() == "USD"
    ]
    if not usd_rows:
        return None

    us_row = next(
        (row for row in usd_rows if is_us_nation_name(row.get("natn_name"))), None
    )
    if us_row is not None:
        return us_row

    return max(usd_rows, key=extract_usd_orderable_from_row)


def _decimal_to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _format_cash_amount(value: float, currency: str) -> str:
    if currency == "KRW":
        return f"{int(value):,} KRW"
    return f"{value:,.2f} {currency}"


async def get_cash_balance_impl(
    account: str | None = None,
    *,
    is_mock: bool = False,
) -> dict[str, Any]:
    from app.mcp_server.tooling.paper_portfolio_handler import (
        collect_paper_cash_balances,
        is_paper_account_token,
        parse_paper_account_token,
    )

    account_filter = _normalize_account_filter(account)
    if is_mock or (account_filter is not None and account_filter.startswith("kis")):
        return {
            "success": False,
            "error": "provider kis is not operational",
            "accounts": [],
            "summary": {
                "total_krw": 0.0,
                "total_usd": 0.0,
                "unavailable_sources": {},
            },
            "errors": [],
        }

    if is_paper_account_token(account):
        selector = parse_paper_account_token(account)
        rows, errors = await collect_paper_cash_balances(selector=selector)
        total_krw = sum(
            float(r.get("balance", 0) or 0) for r in rows if r.get("currency") == "KRW"
        )
        total_usd = sum(
            float(r.get("balance", 0) or 0) for r in rows if r.get("currency") == "USD"
        )
        return {
            "accounts": rows,
            "summary": {
                "total_krw": total_krw,
                "total_usd": total_usd,
                "unavailable_sources": {},
            },
            "errors": errors,
        }

    accounts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    unavailable_sources: dict[str, str] = {}
    total_krw = 0.0
    total_usd = 0.0

    strict_mode = account_filter is not None

    if account_filter is None or account_filter == "toss":
        if bool(getattr(settings, "toss_api_enabled", False)):
            try:
                # ROB-549: surface buying power as orderable only once Toss live
                # mutations are armed; otherwise keep it reference-only (0.0) so
                # the cash signal matches the holdings sellability gate.
                toss_orderable_enabled = toss_live_mutations_enabled()
                toss_snapshot = await fetch_toss_cash_snapshot()
                toss_krw = _decimal_to_float(toss_snapshot.cash_krw)
                toss_usd = _decimal_to_float(toss_snapshot.cash_usd)
                if toss_krw is not None:
                    accounts.append(
                        {
                            "account": "toss",
                            "account_name": "Toss",
                            "broker": "toss",
                            "currency": "KRW",
                            "balance": toss_krw,
                            "orderable": toss_krw if toss_orderable_enabled else 0.0,
                            "formatted": _format_cash_amount(toss_krw, "KRW"),
                        }
                    )
                    total_krw += toss_krw
                if toss_usd is not None:
                    accounts.append(
                        {
                            "account": "toss",
                            "account_name": "Toss",
                            "broker": "toss",
                            "currency": "USD",
                            "balance": toss_usd,
                            "orderable": toss_usd if toss_orderable_enabled else 0.0,
                            "formatted": _format_cash_amount(toss_usd, "USD"),
                        }
                    )
                    total_usd += toss_usd
                errors.extend(toss_snapshot.errors)
            except Exception as exc:
                if strict_mode:
                    raise RuntimeError(
                        f"Toss cash balance query failed: {exc}"
                    ) from exc
                reason = describe_exception(exc)
                errors.append({"source": "toss_api", "market": "cash", "error": reason})
                unavailable_sources["toss"] = reason

    if account_filter is None or account_filter == "upbit":
        try:
            summary = await upbit_service.fetch_krw_cash_summary()
            krw_balance = float(summary.get("balance", 0.0))
            krw_orderable = float(summary.get("orderable", 0.0))
            accounts.append(
                {
                    "account": "upbit",
                    "account_name": "기본 계좌",
                    "broker": "upbit",
                    "currency": "KRW",
                    "balance": krw_balance,
                    "orderable": krw_orderable,
                    "formatted": f"{int(krw_balance):,} KRW",
                }
            )
            total_krw += krw_balance
        except Exception as exc:
            reason = describe_exception(exc)
            errors.append({"source": "upbit", "market": "crypto", "error": reason})
            unavailable_sources["upbit"] = reason

    return {
        "accounts": accounts,
        "summary": {
            "total_krw": total_krw,
            "total_usd": total_usd,
            "unavailable_sources": unavailable_sources,
        },
        "errors": errors,
    }


async def get_usd_krw_rate() -> float:
    """Get the current USD to KRW exchange rate."""
    try:
        return await _get_usd_krw_rate()
    except Exception as exc:
        logger.warning("Failed to fetch USD/KRW rate: %s", exc)
        return 1300.0


def _is_stale_manual_cash(updated_at_iso: str | None) -> bool:
    """Check if manual cash is stale (older than 3 days)."""
    if not updated_at_iso:
        return True
    try:
        updated_at = datetime.fromisoformat(updated_at_iso)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        cutoff = now_kst() - timedelta(days=3)
        return updated_at < cutoff
    except (ValueError, TypeError):
        return True


async def get_available_capital_impl(
    account: str | None = None,
    include_manual: bool = True,
    is_mock: bool = False,
) -> dict[str, Any]:
    """Toss, Upbit 및 수동 현금의 주문 가능 자금을 조회한다."""
    errors: list[dict[str, Any]] = []
    account_filter = _normalize_account_filter(account)
    if is_mock or (account_filter is not None and account_filter.startswith("kis")):
        return {
            "success": False,
            "error": "provider kis is not operational",
            "accounts": [],
            "manual_cash": None,
            "summary": {
                "total_orderable_krw": 0.0,
                "manual_cash_excluded_krw": 0.0,
                "exchange_rate_usd_krw": None,
                "unavailable_sources": {},
            },
            "errors": [],
        }

    cash_result = await get_cash_balance_impl(account=account, is_mock=is_mock)
    accounts = cash_result.get("accounts", [])
    errors.extend(cash_result.get("errors", []))

    has_usd_account = any(acc.get("currency") == "USD" for acc in accounts)
    exchange_rate = None
    if has_usd_account:
        try:
            exchange_rate = await get_usd_krw_rate()
        except Exception as exc:
            logger.warning("Failed to get exchange rate: %s", exc)
            errors.append({"source": "exchange_rate", "error": str(exc)})
            exchange_rate = 1300.0

    total_orderable_krw = 0.0
    manual_cash_excluded_krw = 0.0
    processed_accounts: list[dict[str, Any]] = []

    for acc in accounts:
        processed_acc = dict(acc)
        currency = acc.get("currency", "KRW")
        orderable = float(acc.get("orderable", 0.0) or 0.0)

        if currency == "KRW":
            total_orderable_krw += orderable
        elif currency == "USD" and exchange_rate is not None:
            krw_equivalent = orderable * exchange_rate
            processed_acc["krw_equivalent"] = krw_equivalent
            total_orderable_krw += krw_equivalent

        processed_accounts.append(processed_acc)

    from app.mcp_server.tooling.paper_portfolio_handler import (
        is_paper_account_token,
    )

    manual_cash_result: dict[str, Any] | None = None
    if include_manual and not is_mock and not is_paper_account_token(account):
        try:
            manual_setting = await get_manual_cash_setting()
            if manual_setting is not None:
                value = manual_setting.get("value", {})
                amount = (
                    float(value.get("amount", 0.0)) if isinstance(value, dict) else 0.0
                )
                updated_at = manual_setting.get("updated_at")
                stale_warning = _is_stale_manual_cash(updated_at)

                # ROB-467: stale manual cash is no longer trustworthy as
                # deployable capital. Keep it visible for transparency, but
                # exclude it from the orderable total so it is not mistaken
                # for real ammunition every session.
                manual_cash_result = {
                    "amount": amount,
                    "updated_at": updated_at,
                    "stale_warning": stale_warning,
                    "included_in_total": not stale_warning,
                }
                if stale_warning:
                    manual_cash_excluded_krw += amount
                else:
                    total_orderable_krw += amount
        except Exception as exc:
            logger.warning("Failed to get manual cash setting: %s", exc)
            errors.append({"source": "manual_cash", "error": str(exc)})

    try:
        account_costs = await get_account_costs_setting()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to get account cost setting: %s", exc)
        errors.append({"source": "account_costs", "error": str(exc)})
        account_costs = None
    for processed_acc in processed_accounts:
        account_id = str(processed_acc.get("account") or "")
        market = "us" if processed_acc.get("currency") == "USD" else "kr"
        profile = compact_cost_profile(account_id, market, account_costs)
        if profile is not None:
            processed_acc["cost_profile"] = profile

    return {
        "accounts": processed_accounts,
        "manual_cash": manual_cash_result,
        "summary": {
            "total_orderable_krw": total_orderable_krw,
            "manual_cash_excluded_krw": manual_cash_excluded_krw,
            "exchange_rate_usd_krw": exchange_rate,
            "as_of": now_kst().isoformat(),
            # 공급자 조회 실패를 0원 잔액으로 오해하지 않도록 그대로 전달한다.
            "unavailable_sources": cash_result.get("summary", {}).get(
                "unavailable_sources", {}
            ),
        },
        "errors": errors,
    }


__all__ = [
    "get_cash_balance_impl",
    "get_available_capital_impl",
    "get_usd_krw_rate",
    "get_account_costs_setting",
    "is_us_nation_name",
    "extract_usd_orderable_from_row",
    "select_usd_row_for_us_order",
]
