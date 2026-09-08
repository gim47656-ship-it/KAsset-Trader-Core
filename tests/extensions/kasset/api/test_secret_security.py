from app.extensions.kasset.api.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SessionTokens,
)


def test_secret_models_hide_sensitive_values_from_repr() -> None:
    models = [
        RegisterRequest(
            username="trader",
            email="trader@example.com",
            password="Register-secret-1!",
            deviceId="device",
            deviceName="phone",
        ),
        LoginRequest(
            username="trader",
            password="Login-secret-1!",
            deviceId="device",
            deviceName="phone",
        ),
        RefreshRequest(refreshToken="refresh-only-token"),
        SessionTokens(
            accessToken="access-only-token",
            refreshToken="refresh-session-token",
            accessTokenExpiresAt="2027-08-26T12:00:00Z",
            refreshTokenExpiresAt="2027-08-26T12:00:00Z",
            serverVersion="2.3-r1",
        ),
    ]
    rendered = "\n".join(repr(model) for model in models)

    for secret in (
        "Register-secret-1!",
        "Login-secret-1!",
        "refresh-only-token",
        "access-only-token",
        "refresh-session-token",
    ):
        assert secret not in rendered
