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
    candles: list[DailyCandle]


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
    role: UserRole


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
