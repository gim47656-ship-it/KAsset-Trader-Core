"""Exact route boundary owned by the Android compatibility facade."""

_EXACT_PATHS = frozenset(
    {
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/google",
        "/api/v1/auth/me",
        "/api/v1/auth/refresh",
        "/api/v1/auth/revoke",
        "/api/v1/system/status",
        "/api/v1/system/kill-switch",
        "/api/v1/system/trading-mode",
        "/api/v1/system/audit",
        "/api/v1/admin/system/kill-switch",
        "/api/v1/brokers",
        "/api/v1/account/balance",
        "/api/v1/positions",
        "/api/v1/market/quote",
        "/api/v1/market/overview",
        "/api/v1/market/candles",
        "/api/v1/market/symbols",
        "/api/v1/instruments/search",
        "/api/v1/orders",
        "/api/v1/orders/preview",
        "/api/v1/fills",
        "/api/v1/risk/policy",
        "/api/v1/ai/status",
        "/api/v1/ai/briefing",
        "/api/v1/watchlist",
    }
)
_DYNAMIC_PREFIXES = (
    "/api/v1/brokers/",
    "/api/v1/market/indices/",
    "/api/v1/orders/",
    "/api/v1/watchlist/",
)


def is_android_compat_path(path: str) -> bool:
    return path in _EXACT_PATHS or path.startswith(_DYNAMIC_PREFIXES)


# Generic Core API paths a KAsset Android token may also call. The mobile
# client consumes the recommendation review API, which lives outside the
# compatibility facade; every other generic Core surface (trader-gated web
# APIs included) must reject KAsset-issued tokens.
_KASSET_GENERIC_API_ROOTS = ("/api/v1/ai/recommendations",)


def is_kasset_token_allowed_path(path: str) -> bool:
    if is_android_compat_path(path):
        return True
    return any(
        path == root or path.startswith(root + "/")
        for root in _KASSET_GENERIC_API_ROOTS
    )
