"""Android wire contracts for PAPER account, market, and orders."""

from decimal import Decimal

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
    evaluation_amount: str
    total_assets: str
    unrealized_pnl: str
    realized_pnl: str | None = None
    fx_rate: str | None = None
    updated_at: str


class Position(AndroidWireModel):
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
    unrealized_pnl: str | None = None
    unrealized_pnl_rate: str | None = None
    realized_pnl: str | None = None
    updated_at: str


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
