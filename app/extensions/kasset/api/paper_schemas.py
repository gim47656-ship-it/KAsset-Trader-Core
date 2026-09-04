"""Android wire contracts for PAPER account, market, and orders."""

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import Field, model_validator

from app.extensions.kasset.api.schemas import AndroidWireModel, MarketSessionState


class CashBalance(AndroidWireModel):
    currency: str
    cash: str
    available: str


class Balance(AndroidWireModel):
    broker: str
    account_id: str
    base_currency: str
    cash: list[CashBalance]
    evaluation_amount: str | None
    total_assets: str | None
    unrealized_pnl: str | None
    realized_pnl: str | None = None
    fx_rate: str | None = None
    updated_at: str


class Position(AndroidWireModel):
    """PAPER 보유 종목. 평가 필드는 시세 출처와 함께만 의미가 있다.

    평가값(`currentPrice`/`marketValue`/`unrealizedPnl`/`unrealizedPnlRate`)이
    채워지면 `quoteSource`가 그 값을 만든 채널을 증명한다. USD의 native
    `marketValue`는 덮어쓰지 않으며 `marketValueKrwReference`는 USD/KRW 방향,
    Decimal 환율, provider 유효 구간이 모두 입증되고 fresh일 때만 채우는 표시
    전용 값이다. 증거가 불완전하면 참고값은 `null`이고 안정된 오류 코드만 담는다.
    `quoteAsOf`가 없으면 `quoteIsStale`도 없다.
    """

    broker: str
    account_id: str
    market: str
    symbol: str
    name: str | None = None
    currency: str
    quantity: str
    average_price: str
    current_price: str | None = None
    market_value: str | None = None
    market_value_krw_reference: str | None = None
    market_value_krw_fx_rate: str | None = None
    market_value_krw_fx_source: Literal["toss", "open_er_api"] | None = None
    market_value_krw_fx_as_of: str | None = None
    market_value_krw_fx_valid_until: str | None = None
    market_value_krw_fx_is_stale: bool | None = None
    market_value_krw_reference_error: str | None = None
    unrealized_pnl: str | None = None
    unrealized_pnl_rate: str | None = None
    realized_pnl: str | None = None
    quote_source: str | None = None
    quote_as_of: str | None = None
    quote_session: MarketSessionState | None = None
    quote_is_stale: bool | None = None
    valuation_error: str | None = None
    updated_at: str

    @model_validator(mode="after")
    def validate_krw_reference_provenance(self) -> "Position":
        if self.market_value_krw_reference is None:
            return self
        try:
            rate = Decimal(self.market_value_krw_fx_rate or "")
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                "marketValueKrwReference requires a valid USD/KRW rate"
            ) from exc
        if (
            self.currency != "USD"
            or self.market_value is None
            or not rate.is_finite()
            or rate <= 0
            or not self.market_value_krw_fx_source
            or not self.market_value_krw_fx_as_of
            or not self.market_value_krw_fx_valid_until
            or self.market_value_krw_fx_is_stale is not False
            or self.market_value_krw_reference_error is not None
        ):
            raise ValueError(
                "marketValueKrwReference requires complete fresh USD/KRW provenance"
            )
        return self


class PositionsResponse(AndroidWireModel):
    positions: list[Position]


class Quote(AndroidWireModel):
    broker: str
    market: str
    symbol: str
    name: str | None = None
    currency: str
    price: str
    previous_close: str | None = None
    change_amount: str | None = None
    change_rate: str | None = None
    session: MarketSessionState | None = None
    regular_close: str | None = None
    session_change_amount: str | None = None
    session_change_rate: str | None = None
    as_of: str
    source: str


class QuotesResponse(AndroidWireModel):
    quotes: list[Quote]


class SymbolItem(AndroidWireModel):
    market: str
    symbol: str
    name: str | None = None
    currency: str


class SymbolsResponse(AndroidWireModel):
    symbols: list[SymbolItem]


class RiskReason(AndroidWireModel):
    code: str
    message: str


class RiskAssessment(AndroidWireModel):
    decision: str
    reasons: list[RiskReason]
    estimated_amount: str | None = None
    estimated_fee: str | None = None
    reference_price: str | None = None
    currency: str | None = None


class OrderRequest(AndroidWireModel):
    client_order_id: str | None = Field(default=None, min_length=1, max_length=200)
    broker: str
    account_id: str | None = None
    market: str
    symbol: str = Field(min_length=1, max_length=32)
    side: str
    order_type: str
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_order_shape(self) -> "OrderRequest":
        self.broker = self.broker.strip().upper()
        self.market = self.market.strip().upper()
        self.symbol = self.symbol.strip().upper()
        self.side = self.side.strip().upper()
        self.order_type = self.order_type.strip().upper()
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if self.order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("orderType must be MARKET or LIMIT")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("MARKET order must not include limitPrice")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT order requires limitPrice")
        return self


class AmendRequest(AndroidWireModel):
    quantity: Decimal | None = Field(default=None, gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_change(self) -> "AmendRequest":
        if self.quantity is None and self.limit_price is None:
            raise ValueError("quantity or limitPrice is required")
        return self


class Order(AndroidWireModel):
    id: str
    client_order_id: str
    broker_order_id: str
    broker: str = "PAPER"
    account_id: str
    market: str
    symbol: str
    name: str | None = None
    currency: str
    side: str
    order_type: str
    quantity: str
    limit_price: str | None = None
    status: str
    filled_quantity: str
    average_fill_price: str | None = None
    reject_reason: str | None = None
    created_at: str
    updated_at: str


class Fill(AndroidWireModel):
    id: str
    order_id: str
    broker_order_id: str
    market: str
    symbol: str
    side: str
    quantity: str
    price: str
    fee: str
    filled_at: str


class OrderEnvelope(AndroidWireModel):
    order: Order
    risk: RiskAssessment | None = None
    fills: list[Fill]
    idempotent_replay: bool = False


class OrdersResponse(AndroidWireModel):
    orders: list[Order]


class FillsResponse(AndroidWireModel):
    fills: list[Fill]


class OrderDetail(AndroidWireModel):
    order: Order
    fills: list[Fill]


class ClosedTrade(AndroidWireModel):
    """청산이 끝난 매매 한 건의 확정 손익.

    보유 중 평가손익과 달리 시세에 의존하지 않는다. `returnRate`는 매수 원가
    대비 백분율이고, 통화가 다른 매매는 합산하지 않으므로 `currency`로 갈라
    읽는다.
    """

    market: str
    symbol: str
    name: str | None = None
    currency: str
    quantity: str
    cost_basis: str
    realized_pnl: str
    return_rate: str
    holding_days: int
    entry_at: str
    exit_at: str


class ClosedTradeTotal(AndroidWireModel):
    currency: str
    trade_count: int
    win_count: int
    realized_pnl: str
    cost_basis: str
    return_rate: str


class ClosedTradesResponse(AndroidWireModel):
    """확정 매매 목록과 통화별 합계. 통화가 섞이면 합산하지 않는다."""

    trades: list[ClosedTrade]
    totals: list[ClosedTradeTotal]


class RiskPolicy(AndroidWireModel):
    max_order_ratio: str
    max_symbol_ratio: str
    allow_short_sell: bool
    updated_at: str


class RiskPolicyUpdate(AndroidWireModel):
    max_order_ratio: Decimal | None = Field(default=None, gt=0, le=1)
    max_symbol_ratio: Decimal | None = Field(default=None, gt=0, le=1)


class KillSwitchRequest(AndroidWireModel):
    enabled: bool
    reason: str = Field(min_length=1, max_length=500)


class TradingModeRequest(AndroidWireModel):
    mode: str
