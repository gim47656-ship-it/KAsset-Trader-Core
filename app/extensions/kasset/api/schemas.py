"""Wire schemas matching Android TraderApi JSON names."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.trading import UserRole


def _to_camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.capitalize() for part in rest)


class AndroidWireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


WatchlistMarket = Literal["KRX", "US", "CRYPTO"]
InstrumentSearchMarket = Literal["ALL", "KRX", "US"]
MarketOverviewStatus = Literal["fresh", "partial", "unavailable"]
MarketOverviewItemStatus = Literal["available", "stale", "unavailable"]
MarketSessionState = Literal[
    "DAY_MARKET",
    "PRE_MARKET",
    "REGULAR",
    "AFTER_MARKET",
    "CLOSED",
]
MarketOverviewErrorCode = Literal["UNAVAILABLE", "TIMEOUT"]
MarketIndexRange = Literal["1W", "1M", "3M", "6M"]
CandleRange = Literal["1D", "1W", "1M", "3M", "6M"]
CandleInterval = Literal["10m", "1h", "1d"]
MarketNewsFilterKind = Literal["all", "news", "disclosure"]
MarketNewsKind = Literal["news", "disclosure"]
MarketIndicatorKey = Literal[
    "VIX",
    "US10Y",
    "KR_BOND_2Y",
    "KR_BOND_3Y",
    "KR_BOND_5Y",
    "KR_BOND_10Y",
    "KR_BOND_20Y",
    "KR_BOND_30Y",
    "WTI",
    "BRENT",
    "GOLD",
    "BTC",
]
MarketIndicatorGroup = Literal["VOLATILITY", "RATE", "COMMODITY", "CRYPTO"]
MarketIndicatorUnit = Literal["POINT", "PERCENT", "USD", "KRW"]
MAX_WATCHLIST_ITEMS = 50


class WatchlistCreateRequest(AndroidWireModel):
    symbol: str = Field(min_length=1, max_length=64)
    market: WatchlistMarket


class WatchlistItem(AndroidWireModel):
    symbol: str
    name: str
    market: WatchlistMarket
    instrument_id: int


class WatchlistResponse(AndroidWireModel):
    items: list[WatchlistItem]
    max_items: int = MAX_WATCHLIST_ITEMS


class InstrumentSearchItem(AndroidWireModel):
    symbol: str
    name: str
    market: WatchlistMarket


class InstrumentSearchResponse(AndroidWireModel):
    items: list[InstrumentSearchItem]


class DailyCandle(AndroidWireModel):
    time: str
    open: str
    high: str
    low: str
    close: str
    volume: str


class DailyCandlesResponse(AndroidWireModel):
    interval: CandleInterval
    candles: list[DailyCandle]


class MarketNewsItem(AndroidWireModel):
    kind: MarketNewsKind
    title: str
    summary: str | None
    translated_title: str | None = None
    translated_excerpt: str | None = None
    source: str | None
    url: str
    published_at: str | None
    symbol: str | None
    stock_name: str | None


class MarketNewsResponse(AndroidWireModel):
    items: list[MarketNewsItem]
    next_cursor: str | None


class MarketOverviewItem(AndroidWireModel):
    symbol: str
    name: str
    market: Literal["KRX", "US", "FX"]
    currency: Literal["KRW", "USD"]
    price: str | None = Field(default=None, pattern=r"^-?\d+(?:\.\d+)?$")
    change_amount: str | None = Field(default=None, pattern=r"^-?\d+(?:\.\d+)?$")
    change_rate: str | None = Field(default=None, pattern=r"^-?\d+(?:\.\d+)?$")
    as_of: str | None
    status: MarketOverviewItemStatus
    session_state: MarketSessionState | None


class MarketIndexSummary(MarketOverviewItem):
    market: Literal["KRX", "US"]
    range: MarketIndexRange


class MarketIndexCandle(AndroidWireModel):
    """Index OHLC bar.

    Volume is optional because the KR index price source publishes no traded
    volume for an index bar. Emitting ``0`` there would invent a value, so the
    field stays null and the client renders price only.
    """

    time: str
    open: str
    high: str
    low: str
    close: str
    volume: str | None = None


class MarketIndexDetailResponse(AndroidWireModel):
    summary: MarketIndexSummary
    candles: list[MarketIndexCandle]


class MarketOverviewSession(AndroidWireModel):
    market: Literal["KRX", "US"]
    state: MarketSessionState | None


class MarketOverviewError(AndroidWireModel):
    scope: Literal["indices", "fx", "indicators"]
    symbol: str
    code: MarketOverviewErrorCode


class MarketIndicatorItem(AndroidWireModel):
    """비주식 시장 지표 한 줄(변동성·금리·원자재·암호화폐).

    ``market`` 필드가 없다: 이 지표들은 KRX/US 세션 상태와 결합하지 않으므로
    세션 딕셔너리를 심볼별 키로 인덱싱하는 경로를 아예 만들지 않는다. 단위는
    ``unit``으로만 구분하며, ``US10Y``는 가격이 아니라 % 값이라서
    ``unit="PERCENT"``로 그대로 전달한다.
    """

    key: MarketIndicatorKey
    name: str
    group: MarketIndicatorGroup
    # 공급자가 값을 주지 못하면 status="unavailable"과 함께 null로 내려간다
    # (0이나 전일값으로 채우지 않는다).
    value: str | None = Field(default=None, pattern=r"^-?\d+(?:\.\d+)?$")
    previous_close: str | None = Field(default=None, pattern=r"^-?\d+(?:\.\d+)?$")
    change_amount: str | None = Field(default=None, pattern=r"^-?\d+(?:\.\d+)?$")
    change_rate: str | None = Field(default=None, pattern=r"^-?\d+(?:\.\d+)?$")
    unit: MarketIndicatorUnit
    as_of: str | None
    status: MarketOverviewItemStatus


class MarketOverviewResponse(AndroidWireModel):
    as_of: str | None
    status: MarketOverviewStatus
    indices: list[MarketOverviewItem]
    indicators: list[MarketIndicatorItem]
    fx: list[MarketOverviewItem]
    sessions: list[MarketOverviewSession]
    errors: list[MarketOverviewError]


class OrderbookLevel(AndroidWireModel):
    price: str = Field(pattern=r"^\d+(?:\.\d+)?$")
    volume: str = Field(pattern=r"^\d+(?:\.\d+)?$")


class OrderbookResponse(AndroidWireModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    market: Literal["KRX"]
    ready: bool
    as_of: str | None
    source: Literal["NH_PLUG_WS"]
    asks: list[OrderbookLevel] = Field(max_length=10)
    bids: list[OrderbookLevel] = Field(max_length=10)
    total_ask_volume: str = Field(pattern=r"^\d+(?:\.\d+)?$")
    total_bid_volume: str = Field(pattern=r"^\d+(?:\.\d+)?$")


class RegisterRequest(AndroidWireModel):
    username: str = Field(min_length=1, max_length=50)
    email: EmailStr
    password: str = Field(min_length=1, max_length=128, repr=False)
    device_id: str = Field(alias="deviceId", min_length=1, max_length=200)
    device_name: str = Field(alias="deviceName", min_length=1, max_length=200)


class LoginRequest(AndroidWireModel):
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128, repr=False)
    device_id: str = Field(alias="deviceId", min_length=1, max_length=200)
    device_name: str = Field(alias="deviceName", min_length=1, max_length=200)


class GoogleLoginRequest(AndroidWireModel):
    id_token: str = Field(alias="idToken", min_length=1, repr=False)
    device_id: str = Field(alias="deviceId", min_length=1, max_length=200)
    device_name: str = Field(alias="deviceName", min_length=1, max_length=200)


class CurrentUserResponse(AndroidWireModel):
    id: int
    username: str
    email: str
    nickname: str
    role: UserRole


class NicknameUpdateRequest(AndroidWireModel):
    nickname: str


class RefreshRequest(AndroidWireModel):
    refresh_token: str = Field(alias="refreshToken", min_length=1, repr=False)


class CredentialRequest(AndroidWireModel):
    app_key: str = Field(min_length=1, max_length=2048, repr=False)
    app_secret: str = Field(min_length=1, max_length=2048, repr=False)
    account_no: str = Field(min_length=1, max_length=2048, repr=False)


class SessionTokens(AndroidWireModel):
    access_token: str = Field(alias="accessToken", repr=False)
    refresh_token: str = Field(alias="refreshToken", repr=False)
    access_token_expires_at: str = Field(alias="accessTokenExpiresAt")
    refresh_token_expires_at: str = Field(alias="refreshTokenExpiresAt")
    server_version: str = Field(alias="serverVersion")


class BrokerCapabilities(AndroidWireModel):
    domestic_stock: bool = False
    us_stock: bool = False
    foreign_stock: bool = False
    rest: bool = False
    web_socket: bool = False
    paper_trading: bool = False
    market_order: bool = False
    limit_order: bool = False
    scheduled_order: bool = False
    fractional_order: bool = False
    oco: bool = False
    oto: bool = False
    fixed_ip_required: bool = False
    read_only: bool = False


class Broker(AndroidWireModel):
    provider: str
    display_name: str
    connected: bool
    credential_id: str | None = None
    app_key_masked: str | None = None
    account_no_masked: str | None = None
    last_verified_at: str | None = None
    supported_modes: list[str]
    requires_credential: bool
    implemented: bool
    mode: str
    capabilities: BrokerCapabilities


class BrokersResponse(AndroidWireModel):
    brokers: list[Broker]


class BrokerVerifyResponse(AndroidWireModel):
    connected: bool
    checked_at: str
    message: str


class DatabaseStatus(AndroidWireModel):
    status: str
    migration_revision: str | None = None


class SystemBrokerStatus(AndroidWireModel):
    provider: str
    connected: bool
    last_verified_at: str | None = None


class AiRelayStatus(AndroidWireModel):
    configured: bool
    reachable: bool
    last_checked_at: str | None = None
    message: str


class SystemStatus(AndroidWireModel):
    server_version: str
    server_time: str
    database: DatabaseStatus
    trading_mode: str
    trading_enabled: bool
    live_trading_enabled: bool
    kill_switch_enabled: bool
    brokers: list[SystemBrokerStatus]
    ai_relay: AiRelayStatus
    last_quote_at: str | None = None


class HealthResponse(AndroidWireModel):
    status: str = "ok"


class AiBriefingSection(AndroidWireModel):
    status: str
    refreshed_at: str | None
    items: list[dict[str, object]]


class AiBriefingSummary(AndroidWireModel):
    status: str
    id: str | None
    title: str | None
    summary: str | None
    provider: str | None
    market: str | None
    as_of: str | None
    valid_until: str | None
    data_status: str | None
    unavailable_reason: str | None


class AiBriefingResponse(AndroidWireModel):
    status: str
    as_of: str | None
    news: AiBriefingSection
    research: AiBriefingSection
    briefing: AiBriefingSummary
