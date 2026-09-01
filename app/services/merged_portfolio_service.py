"""Toss 실계좌와 사용자 수동 보유분을 합성하는 read-only 포트폴리오."""

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import MarketType
from app.services.exchange_rate_service import get_usd_krw_rate
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.toss_portfolio_service import (
    TossPortfolioPosition,
    fetch_toss_portfolio_snapshot,
)

logger = logging.getLogger(__name__)


@dataclass
class HoldingInfo:
    """단일 브로커의 보유 정보"""

    broker: str
    quantity: float
    avg_price: float


@dataclass
class ReferencePrices:
    """참조 평단가 정보"""

    kis_avg: float | None = None
    kis_quantity: int = 0
    toss_avg: float | None = None
    toss_quantity: int = 0
    combined_avg: float | None = None
    total_quantity: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kis_avg": self.kis_avg,
            "kis_quantity": self.kis_quantity,
            "toss_avg": self.toss_avg,
            "toss_quantity": self.toss_quantity,
            "combined_avg": self.combined_avg,
            "total_quantity": self.total_quantity,
        }


@dataclass
class MergedHolding:
    """통합 보유 종목 정보"""

    ticker: str
    name: str
    market_type: str
    holdings: list[HoldingInfo] = field(default_factory=list)
    kis_quantity: int = 0
    kis_avg_price: float = 0.0
    toss_quantity: int = 0
    toss_avg_price: float = 0.0
    other_quantity: int = 0
    other_avg_price: float = 0.0
    combined_avg_price: float = 0.0
    total_quantity: int = 0
    current_price: float = 0.0
    evaluation: float = 0.0
    profit_loss: float = 0.0
    profit_rate: float = 0.0
    # AI 분석 정보
    analysis_id: int | None = None
    last_analysis_at: str | None = None
    last_analysis_decision: str | None = None
    analysis_confidence: int | None = None
    # 거래 설정
    settings_quantity: float | None = None
    settings_price_levels: int | None = None
    settings_active: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "market_type": self.market_type,
            "holdings": [
                {"broker": h.broker, "quantity": h.quantity, "avg_price": h.avg_price}
                for h in self.holdings
            ],
            "kis_quantity": self.kis_quantity,
            "kis_avg_price": self.kis_avg_price,
            "toss_quantity": self.toss_quantity,
            "toss_avg_price": self.toss_avg_price,
            "other_quantity": self.other_quantity,
            "other_avg_price": self.other_avg_price,
            "combined_avg_price": self.combined_avg_price,
            "total_quantity": self.total_quantity,
            "current_price": self.current_price,
            "evaluation": self.evaluation,
            "profit_loss": self.profit_loss,
            "profit_rate": self.profit_rate,
            "analysis_id": self.analysis_id,
            "last_analysis_at": self.last_analysis_at,
            "last_analysis_decision": self.last_analysis_decision,
            "analysis_confidence": self.analysis_confidence,
            "settings_quantity": self.settings_quantity,
            "settings_price_levels": self.settings_price_levels,
            "settings_active": self.settings_active,
        }


class MergedPortfolioService:
    """통합 포트폴리오 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.manual_holdings_service = ManualHoldingsService(db)

    @staticmethod
    def calculate_combined_avg(holdings: list[HoldingInfo]) -> float:
        """가중 평균 평단가 계산"""
        total_value = 0.0
        total_quantity = 0.0

        for h in holdings:
            total_value += h.quantity * h.avg_price
            total_quantity += h.quantity

        if total_quantity == 0:
            return 0.0

        return total_value / total_quantity

    @staticmethod
    def _resolve_broker_type(raw_broker_type: Any) -> str:
        """브로커 타입을 문자열로 정규화 (enum 또는 plain string 모두 지원)."""
        if isinstance(raw_broker_type, str):
            return raw_broker_type.lower()

        value = getattr(raw_broker_type, "value", None)
        if isinstance(value, str):
            return value.lower()

        return str(raw_broker_type).lower()

    @staticmethod
    def _get_or_create_holding(
        merged: dict[str, MergedHolding],
        ticker: str,
        name: str,
        market_type: MarketType,
        current_price: float = 0.0,
    ) -> MergedHolding:
        if ticker not in merged:
            merged[ticker] = MergedHolding(
                ticker=ticker,
                name=name,
                market_type=market_type.value,
                current_price=current_price,
            )
        elif current_price:
            merged[ticker].current_price = current_price

        return merged[ticker]

    async def _fetch_toss_holdings(
        self, market_type: MarketType
    ) -> list[TossPortfolioPosition]:
        """단일 운영자 Toss 계좌의 일반 스냅샷을 조회한다."""

        try:
            snapshot = await fetch_toss_portfolio_snapshot(
                need_sellable=False,
                need_cash=False,
            )
        except Exception as exc:
            logger.error("Failed to fetch Toss %s stocks: %s", market_type.value, exc)
            return []

        instrument_type = "equity_kr" if market_type == MarketType.KR else "equity_us"
        return [
            position
            for position in snapshot.positions
            if position.instrument_type == instrument_type
        ]

    def _apply_toss_holdings(
        self,
        merged: dict[str, MergedHolding],
        positions: list[TossPortfolioPosition],
        market_type: MarketType,
    ) -> None:
        for position in positions:
            ticker = str(position.symbol).strip().upper()
            quantity = int(position.quantity)
            if not ticker or quantity <= 0:
                continue

            avg_price = float(position.avg_buy_price)
            current_price = float(position.current_price)
            holding = self._get_or_create_holding(
                merged,
                ticker,
                position.name or ticker,
                market_type,
                current_price,
            )
            previous_quantity = holding.toss_quantity
            total_quantity = previous_quantity + quantity
            if total_quantity > 0:
                holding.toss_avg_price = (
                    (holding.toss_avg_price * previous_quantity)
                    + (avg_price * quantity)
                ) / total_quantity
            holding.toss_quantity = total_quantity
            holding.current_price = current_price
            if position.evaluation_amount is not None:
                holding.evaluation = float(position.evaluation_amount)
            if position.profit_loss is not None:
                holding.profit_loss = float(position.profit_loss)
            if position.profit_rate is not None:
                holding.profit_rate = float(position.profit_rate)
            holding.holdings.append(
                HoldingInfo(broker="toss", quantity=quantity, avg_price=avg_price)
            )

    async def _apply_manual_holdings(
        self,
        merged: dict[str, MergedHolding],
        user_id: int,
        market_type: MarketType,
    ) -> None:
        manual_holdings = await self.manual_holdings_service.get_holdings_by_user(
            user_id, market_type=market_type
        )

        live_toss_symbols = {
            ticker
            for ticker, item in merged.items()
            if any(source.broker == "toss" for source in item.holdings)
        }
        for holding in manual_holdings:
            ticker = str(holding.ticker).strip().upper()
            broker_type = self._resolve_broker_type(holding.broker_account.broker_type)
            qty = int(holding.quantity)
            avg_price = float(holding.avg_price)
            name = holding.display_name or ticker

            merged_holding = merged.get(ticker)
            if broker_type == "toss" and ticker in live_toss_symbols:
                # InvestHomeService와 같은 규칙: 실계좌 Toss 보유분이 있으면
                # 같은 종목의 Toss 수동 행은 중복 참조값이므로 게시하지 않는다.
                continue
            if not merged_holding:
                merged_holding = self._get_or_create_holding(
                    merged, ticker, name, market_type
                )

            if broker_type == "toss":
                merged_holding.toss_quantity = qty
                merged_holding.toss_avg_price = avg_price
            else:
                merged_holding.other_quantity += qty

            merged_holding.holdings.append(
                HoldingInfo(broker=broker_type, quantity=qty, avg_price=avg_price)
            )

    async def _fetch_missing_prices(
        self,
        merged: dict[str, MergedHolding],
        market_type: MarketType,
    ) -> None:
        """현재가가 없는 수동 종목을 shared market-data 경로로 보강한다."""

        from app.services.market_data.service import get_quote

        market = "kr" if market_type == MarketType.KR else "us"
        for ticker, holding in merged.items():
            if holding.current_price > 0 or holding.total_quantity <= 0:
                continue
            try:
                quote = await get_quote(ticker, market)
            except Exception as exc:
                logger.warning("Failed to fetch price for %s: %s", ticker, exc)
                continue
            if quote.price > 0:
                holding.current_price = float(quote.price)

    def _finalize_holdings(
        self, merged: dict[str, MergedHolding], *, usd_krw: float | None = None
    ) -> None:
        for holding in merged.values():
            # For US stocks, detect if any manual broker has avg_price in KRW and convert to USD
            if (
                holding.market_type == MarketType.US
                and holding.current_price > 0
                and usd_krw
            ):
                for h in holding.holdings:
                    if (
                        h.avg_price > 1000
                        and (h.avg_price / holding.current_price) > 100
                    ):
                        h.avg_price = h.avg_price / usd_krw
                        # Also update specific broker fields for display
                        if h.broker == "toss":
                            holding.toss_avg_price = h.avg_price
                        else:
                            holding.other_avg_price = h.avg_price

            holding.total_quantity = sum(
                int(item.quantity) for item in holding.holdings
            )
            holding.combined_avg_price = self.calculate_combined_avg(holding.holdings)

            if holding.combined_avg_price > 0 and holding.current_price > 0:
                holding.evaluation = holding.current_price * holding.total_quantity
                holding.profit_loss = (
                    holding.current_price - holding.combined_avg_price
                ) * holding.total_quantity
                holding.profit_rate = (
                    holding.current_price - holding.combined_avg_price
                ) / holding.combined_avg_price
            elif holding.current_price > 0:
                # Handle zero/missing avg_price: treat as bought at current price
                holding.evaluation = holding.current_price * holding.total_quantity
                holding.profit_loss = 0.0
                holding.profit_rate = 0.0

    async def _attach_analysis_and_settings(
        self, merged: dict[str, MergedHolding]
    ) -> None:
        from app.services.stock_info_service import StockAnalysisService
        from app.services.symbol_trade_settings_service import (
            SymbolTradeSettingsService,
        )

        stock_service = StockAnalysisService(self.db)
        settings_service = SymbolTradeSettingsService(self.db)

        tickers = list(merged.keys())
        analysis_map = await stock_service.get_latest_analysis_results_for_coins(
            tickers
        )

        for ticker, merged_holding in merged.items():
            analysis = analysis_map.get(ticker)
            if analysis:
                merged_holding.analysis_id = analysis.id
                merged_holding.last_analysis_at = (
                    analysis.created_at.isoformat() if analysis.created_at else None
                )
                merged_holding.last_analysis_decision = analysis.decision
                merged_holding.analysis_confidence = analysis.confidence

            settings = await settings_service.get_by_symbol(ticker)
            if settings and settings.is_active:
                merged_holding.settings_quantity = float(
                    settings.buy_quantity_per_order
                )
                merged_holding.settings_price_levels = settings.buy_price_levels
                merged_holding.settings_active = settings.is_active

    async def _build_merged_portfolio(
        self,
        user_id: int,
        market_type: MarketType,
        *,
        attach_metadata: bool = True,
    ) -> list[MergedHolding]:
        merged: dict[str, MergedHolding] = {}

        toss_positions = await self._fetch_toss_holdings(market_type)
        self._apply_toss_holdings(merged, toss_positions, market_type)
        await self._apply_manual_holdings(merged, user_id, market_type)

        for holding in merged.values():
            holding.total_quantity = sum(
                int(item.quantity) for item in holding.holdings
            )
        await self._fetch_missing_prices(merged, market_type)

        usd_krw_rate = (
            await get_usd_krw_rate() if market_type == MarketType.US else None
        )
        self._finalize_holdings(merged, usd_krw=usd_krw_rate)
        if attach_metadata:
            await self._attach_analysis_and_settings(merged)

        return list(merged.values())

    async def get_reference_prices(
        self,
        user_id: int,
        ticker: str,
        market_type: MarketType,
        _legacy_holdings: dict[str, Any] | None = None,
    ) -> ReferencePrices:
        """특정 종목의 Toss·수동 통합 참조 평단가를 조회한다."""

        rows = await self._build_merged_portfolio(
            user_id,
            market_type,
            attach_metadata=False,
        )
        normalized_ticker = str(ticker).strip().upper()
        holding = next(
            (row for row in rows if row.ticker.strip().upper() == normalized_ticker),
            None,
        )
        if holding is None:
            return ReferencePrices()
        return ReferencePrices(
            toss_avg=(holding.toss_avg_price if holding.toss_quantity > 0 else None),
            toss_quantity=holding.toss_quantity,
            combined_avg=holding.combined_avg_price,
            total_quantity=holding.total_quantity,
        )

    async def get_merged_portfolio_domestic(
        self,
        user_id: int,
    ) -> list[MergedHolding]:
        """국내주식 Toss·수동 통합 포트폴리오 조회."""

        return await self._build_merged_portfolio(user_id, MarketType.KR)

    async def get_merged_portfolio_overseas(
        self,
        user_id: int,
    ) -> list[MergedHolding]:
        """해외주식 Toss·수동 통합 포트폴리오 조회."""

        return await self._build_merged_portfolio(user_id, MarketType.US)
