"""Portfolio data collection — broker/manual asset fetch helpers.

Extracted from PortfolioOverviewService to isolate broker-API side-effects
from aggregation / price-fill logic.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

import app.services.brokers.upbit.client as upbit_service
from app.core.normalizers import to_float as _to_float
from app.models.manual_holdings import MarketType
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot
from app.services.upbit_symbol_universe_service import get_active_upbit_markets

# Market-type constants (kept in sync with portfolio_overview_service.py)
_MARKET_KR = "KR"
_MARKET_US = "US"
_MARKET_CRYPTO = "CRYPTO"

logger = logging.getLogger(__name__)


def _normalize_market_type(value: Any) -> str | None:
    if isinstance(value, MarketType):
        normalized = value.value.upper()
    elif value is None:
        return None
    else:
        normalized = str(value).strip().upper()

    if normalized == "COIN":
        return _MARKET_CRYPTO
    if normalized in {_MARKET_KR, _MARKET_US, _MARKET_CRYPTO}:
        return normalized
    return None


def _normalize_symbol(symbol: str, market_type: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if market_type == _MARKET_CRYPTO:
        if "-" in normalized:
            return normalized
        return f"KRW-{normalized}"
    return normalized


def _log_broker_failure(
    broker_name: str,
    exc: Exception,
    warnings: list[str],
) -> None:
    """Log a broker fetch failure and append a user-facing warning string."""
    logger.warning("Failed to fetch %s holdings: %s", broker_name, exc)
    warnings.append(f"{broker_name} holdings fetch failed: {exc}")


class PortfolioDataCollector:
    """Responsible for fetching raw holding data from each broker."""

    def __init__(self, db: AsyncSession) -> None:
        self.manual_holdings_service = ManualHoldingsService(db)

    async def _collect_toss_components(
        self,
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        try:
            snapshot = await fetch_toss_portfolio_snapshot(
                need_sellable=False,
                need_cash=False,
            )
        except Exception as exc:
            _log_broker_failure("Toss", exc, warnings)
            return []

        components: list[dict[str, Any]] = []
        for position in snapshot.positions:
            if position.instrument_type == "equity_kr":
                market_type = _MARKET_KR
            elif position.instrument_type == "equity_us":
                market_type = _MARKET_US
            else:
                continue

            symbol = _normalize_symbol(position.symbol, market_type)
            quantity = _to_float(position.quantity)
            if not symbol or quantity <= 0:
                continue
            components.append(
                {
                    "market_type": market_type,
                    "symbol": symbol,
                    "name": position.name or symbol,
                    "account_key": "live:toss",
                    "broker": "toss",
                    "account_name": position.account_name or "Toss 실계좌",
                    "source": "live",
                    "quantity": quantity,
                    "avg_price": _to_float(position.avg_buy_price),
                    "current_price": (
                        _to_float(position.current_price, default=0.0) or None
                    ),
                    "evaluation": (
                        _to_float(position.evaluation_amount, default=0.0) or None
                    ),
                    "profit_loss": (
                        _to_float(position.profit_loss, default=0.0)
                        if position.profit_loss is not None
                        else None
                    ),
                    "profit_rate": (
                        _to_float(position.profit_rate, default=0.0)
                        if position.profit_rate is not None
                        else None
                    ),
                }
            )

        for error in snapshot.errors:
            code = error.get("code") if isinstance(error, dict) else None
            warnings.append(f"Toss holdings partial: {code or 'unknown'}")
        return components

    # ------------------------------------------------------------------
    # Upbit
    # ------------------------------------------------------------------

    async def _collect_upbit_components(
        self,
        warnings: list[str],
        active_upbit_markets: set[str] | None = None,
        enforce_upbit_universe: bool = True,
    ) -> list[dict[str, Any]]:
        components: list[dict[str, Any]] = []

        try:
            coins = await upbit_service.fetch_my_coins()
        except Exception as exc:
            _log_broker_failure("Upbit", exc, warnings)
            return components

        tradable_set: set[str] | None = None
        if enforce_upbit_universe:
            tradable_set = active_upbit_markets
            if tradable_set is None:
                tradable_set = await get_active_upbit_markets(quote_currency=None)
            tradable_set = {
                str(market).strip().upper()
                for market in tradable_set
                if str(market).strip()
            }

        for coin in coins:
            currency = str(coin.get("currency", "")).strip().upper()
            if not currency or currency == "KRW":
                continue

            unit_currency = str(coin.get("unit_currency") or "KRW").strip().upper()
            symbol = _normalize_symbol(f"{unit_currency}-{currency}", _MARKET_CRYPTO)
            if tradable_set is not None and symbol not in tradable_set:
                logger.info("Skipping non-tradable Upbit holding symbol=%s", symbol)
                continue
            quantity = _to_float(coin.get("balance")) + _to_float(coin.get("locked"))
            if quantity <= 0:
                continue

            components.append(
                {
                    "market_type": _MARKET_CRYPTO,
                    "symbol": symbol,
                    "name": symbol,
                    "account_key": "live:upbit",
                    "broker": "upbit",
                    "account_name": "Upbit 실계좌",
                    "source": "live",
                    "quantity": quantity,
                    "avg_price": _to_float(coin.get("avg_buy_price")),
                    "current_price": None,
                    "evaluation": None,
                    "profit_loss": None,
                    "profit_rate": None,
                }
            )

        # Price fill for Upbit components is handled by PortfolioOverviewService
        # (_fetch_upbit_prices_resilient / _fill_missing_crypto_prices).
        return components

    # ------------------------------------------------------------------
    # Manual holdings
    # ------------------------------------------------------------------

    async def _collect_manual_components(
        self,
        user_id: int,
        warnings: list[str],
        active_upbit_markets: set[str] | None = None,
        enforce_upbit_universe: bool = True,
    ) -> list[dict[str, Any]]:
        try:
            holdings = await self.manual_holdings_service.get_holdings_by_user(user_id)
        except Exception as exc:
            _log_broker_failure("Manual", exc, warnings)
            return []

        components: list[dict[str, Any]] = []
        tradable_crypto_symbols = active_upbit_markets
        if tradable_crypto_symbols is not None:
            tradable_crypto_symbols = {
                str(market).strip().upper()
                for market in tradable_crypto_symbols
                if str(market).strip()
            }
        for holding in holdings:
            market_type = _normalize_market_type(getattr(holding, "market_type", None))
            if market_type is None:
                continue

            symbol = _normalize_symbol(getattr(holding, "ticker", ""), market_type)
            if not symbol:
                continue

            if market_type == _MARKET_CRYPTO and enforce_upbit_universe:
                if tradable_crypto_symbols is None:
                    tradable_crypto_symbols = await get_active_upbit_markets(
                        quote_currency=None
                    )
                    tradable_crypto_symbols = {
                        str(market).strip().upper()
                        for market in tradable_crypto_symbols
                        if str(market).strip()
                    }
                if symbol not in tradable_crypto_symbols:
                    logger.info(
                        "Skipping non-tradable manual CRYPTO holding symbol=%s",
                        symbol,
                    )
                    continue

            broker_account = getattr(holding, "broker_account", None)
            broker_value = getattr(broker_account, "broker_type", "manual")
            if hasattr(broker_value, "value"):
                broker_value = broker_value.value
            broker = str(broker_value or "manual").strip().lower()

            account_id = getattr(broker_account, "id", None)
            account_name = str(
                getattr(broker_account, "account_name", "기본 계좌") or "기본 계좌"
            )
            account_key = (
                f"manual:{account_id}" if account_id is not None else "manual:unknown"
            )

            quantity = _to_float(getattr(holding, "quantity", Decimal("0")))
            if quantity <= 0:
                continue

            components.append(
                {
                    "market_type": market_type,
                    "symbol": symbol,
                    "name": str(getattr(holding, "display_name", None) or symbol),
                    "account_key": account_key,
                    "broker": broker,
                    "account_name": account_name,
                    "source": "manual",
                    "quantity": quantity,
                    "avg_price": _to_float(getattr(holding, "avg_price", Decimal("0"))),
                    "current_price": None,
                    "evaluation": None,
                    "profit_loss": None,
                    "profit_rate": None,
                }
            )

        return components

    # ------------------------------------------------------------------
    # Task runner (isolates warnings per collection task)
    # ------------------------------------------------------------------

    async def _run_collection_task(
        self,
        func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Run a collection task and return its results and warnings."""
        local_warnings: list[str] = []
        try:
            result = await func(*args, warnings=local_warnings, **kwargs)
        except Exception as exc:
            logger.warning("Collection task failed: %s", exc)
            local_warnings.append(str(exc))
            result = []
        return result, local_warnings
