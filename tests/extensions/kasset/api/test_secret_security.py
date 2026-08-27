from datetime import UTC, datetime

import pytest

from app.extensions.kasset.api.credential_vault import (
    CredentialVault,
    RevealedBrokerCredential,
)
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.schemas import (
    Broker,
    BrokerCapabilities,
    CredentialRequest,
    PairRequest,
    RefreshRequest,
    SessionTokens,
)


def test_vault_ciphertext_round_trip_and_aad_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        CredentialVault,
        "_master_key",
        staticmethod(lambda: b"kasset-test-master-key-32-bytes!"),
    )
    vault = CredentialVault()
    plaintext = "nh-app-secret-do-not-leak"

    encrypted = vault._encrypt(
        plaintext,
        credential_id="credential-1",
        provider="NH",
        field_name="app_secret",
    )

    assert encrypted.startswith("v1.")
    assert plaintext not in encrypted
    assert (
        vault._decrypt(
            encrypted,
            credential_id="credential-1",
            provider="NH",
            field_name="app_secret",
        )
        == plaintext
    )
    with pytest.raises(
        MobileApiError, match="저장된 브로커 연결 정보를 읽지 못했습니다"
    ):
        vault._decrypt(
            encrypted,
            credential_id="credential-1",
            provider="NH",
            field_name="account_no",
        )


def test_secret_models_hide_sensitive_values_from_repr() -> None:
    now = datetime.now(UTC)
    models = [
        PairRequest(pairingCode="pairing-code", deviceId="device", deviceName="phone"),
        RefreshRequest(refreshToken="refresh-only-token"),
        CredentialRequest(
            appKey="nh-app-key",
            appSecret="nh-app-secret",
            accountNo="12345678901",
        ),
        SessionTokens(
            accessToken="access-only-token",
            refreshToken="refresh-session-token",
            accessTokenExpiresAt="2027-08-26T12:00:00Z",
            refreshTokenExpiresAt="2027-08-26T12:00:00Z",
            serverVersion="2.3-r1",
        ),
        RevealedBrokerCredential(
            credential_id="credential-1",
            provider="NH",
            app_key="revealed-key",
            app_secret="revealed-secret",
            account_no="98765432101",
            created_at=now,
            updated_at=now,
            last_verified_at=None,
        ),
    ]
    rendered = "\n".join(repr(model) for model in models)

    for secret in (
        "pairing-code",
        "refresh-only-token",
        "nh-app-key",
        "nh-app-secret",
        "12345678901",
        "access-only-token",
        "refresh-session-token",
        "revealed-key",
        "revealed-secret",
        "98765432101",
    ):
        assert secret not in rendered


def test_broker_response_has_only_masked_credential_fields() -> None:
    response = Broker(
        provider="NH",
        displayName="NH투자증권 PLUG",
        connected=True,
        credentialId="credential-1",
        appKeyMasked="••••-key",
        accountNoMasked="••••8901",
        lastVerifiedAt="2026-08-26T12:00:00Z",
        supportedModes=["MOCK_READ_ONLY"],
        requiresCredential=True,
        implemented=True,
        mode="MOCK_READ_ONLY",
        capabilities=BrokerCapabilities(
            domesticStock=True,
            rest=True,
            paperTrading=True,
            readOnly=True,
        ),
    ).model_dump(by_alias=True)

    serialized = str(response)
    assert response["appKeyMasked"] == "••••-key"
    assert response["accountNoMasked"] == "••••8901"
    assert "appSecret" not in response
    assert "accessToken" not in serialized
    assert "refreshToken" not in serialized
