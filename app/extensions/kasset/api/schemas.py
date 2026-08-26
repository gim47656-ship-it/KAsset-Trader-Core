"""Wire schemas matching Android TraderApi JSON names."""

from pydantic import BaseModel, ConfigDict, Field


class AndroidWireModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class PairRequest(AndroidWireModel):
    pairing_code: str = Field(alias="pairingCode", min_length=1, max_length=256)
    device_id: str = Field(alias="deviceId", min_length=1, max_length=200)
    device_name: str = Field(alias="deviceName", min_length=1, max_length=200)


class RefreshRequest(AndroidWireModel):
    refresh_token: str = Field(alias="refreshToken", min_length=1)


class SessionTokens(AndroidWireModel):
    access_token: str = Field(alias="accessToken")
    refresh_token: str = Field(alias="refreshToken")
    access_token_expires_at: str = Field(alias="accessTokenExpiresAt")
    refresh_token_expires_at: str = Field(alias="refreshTokenExpiresAt")
    server_version: str = Field(alias="serverVersion")
