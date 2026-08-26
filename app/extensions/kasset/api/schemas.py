"""Wire schemas matching Android TraderApi JSON names."""

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.capitalize() for part in rest)


class AndroidWireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class PairRequest(AndroidWireModel):
    pairing_code: str = Field(alias="pairingCode", min_length=1, max_length=256)
    device_id: str = Field(alias="deviceId", min_length=1, max_length=200)
    device_name: str = Field(alias="deviceName", min_length=1, max_length=200)


class RefreshRequest(AndroidWireModel):
    refresh_token: str = Field(alias="refreshToken", min_length=1)


class CredentialRequest(AndroidWireModel):
    app_key: str = Field(min_length=1, max_length=2048)
    app_secret: str = Field(min_length=1, max_length=2048)
    account_no: str = Field(min_length=1, max_length=2048)


class SessionTokens(AndroidWireModel):
    access_token: str = Field(alias="accessToken")
    refresh_token: str = Field(alias="refreshToken")
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
