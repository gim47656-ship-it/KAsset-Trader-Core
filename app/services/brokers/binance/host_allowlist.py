"""ROB-285 — Public-adapter host allowlist (frozen).

Transport-layer host enforcement is the single source of truth for which
hosts the public adapter is allowed to talk to. There is no signed/demo
execution allowlist in this package: those adapters were removed, so every
host listed here is public read-only.
"""

from __future__ import annotations

from app.services.brokers.binance.errors import BinanceLiveHostBlocked

PUBLIC_HOSTS: frozenset[str] = frozenset(
    {
        "api.binance.com",
        "data-api.binance.vision",
        "stream.binance.com",
        "data-stream.binance.vision",
    }
)


def assert_allowed_host(host: str) -> None:
    """Raise BinanceLiveHostBlocked if ``host`` is not in PUBLIC_HOSTS.

    Strict equality match — no suffix/wildcard. Subdomain spoofs like
    ``stream.binance.com.evil.example`` are rejected because the full
    host string differs from any allowlist entry.
    """
    if host not in PUBLIC_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Host {host!r} is not in PUBLIC_HOSTS. "
            "Allowed: " + ", ".join(sorted(PUBLIC_HOSTS))
        )


# ROB-317 — read-only public USD-M futures WS stream host. Unsigned market
# data only, and deliberately kept out of PUBLIC_HOSTS so the REST/WS public
# transports keep their own narrow host sets. See ROB-317 design §2.
PUBLIC_FUTURES_STREAM_HOSTS: frozenset[str] = frozenset(
    {
        "fstream.binance.com",
    }
)


def assert_public_futures_stream_host(host: str) -> None:
    """Raise BinanceLiveHostBlocked if host is not the public futures stream host.

    Strict equality match — no suffix/wildcard, so subdomain spoofs like
    ``fstream.binance.com.evil.example`` are rejected.
    """
    if host not in PUBLIC_FUTURES_STREAM_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Public futures stream host blocked: {host!r} not in "
            f"{sorted(PUBLIC_FUTURES_STREAM_HOSTS)}"
        )
