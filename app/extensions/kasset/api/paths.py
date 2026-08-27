"""Exact route boundary owned by the Android compatibility facade."""

_EXACT_PATHS = frozenset(
    {
        "/api/v1/auth/pair",
        "/api/v1/auth/refresh",
        "/api/v1/auth/revoke",
        "/api/v1/system/status",
        "/api/v1/system/kill-switch",
        "/api/v1/system/trading-mode",
        "/api/v1/system/audit",
        "/api/v1/brokers",
        "/api/v1/account/balance",
        "/api/v1/positions",
        "/api/v1/market/quote",
        "/api/v1/market/symbols",
        "/api/v1/orders",
        "/api/v1/orders/preview",
        "/api/v1/fills",
        "/api/v1/risk/policy",
        "/api/v1/ai/status",
        "/api/v1/ai/briefing",
    }
)
_DYNAMIC_PREFIXES = ("/api/v1/brokers/", "/api/v1/orders/")


def is_android_compat_path(path: str) -> bool:
    return path in _EXACT_PATHS or path.startswith(_DYNAMIC_PREFIXES)
