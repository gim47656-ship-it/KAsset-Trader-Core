"""ROB-317 — read-only public futures stream allowlist.

fstream.binance.com is read-allowed (unsigned market data) and kept in its
own host set, disjoint from the public spot allowlist, so each public
transport keeps its own narrow host list.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import (
    PUBLIC_FUTURES_STREAM_HOSTS,
    PUBLIC_HOSTS,
    assert_allowed_host,
    assert_public_futures_stream_host,
)


def test_only_fstream() -> None:
    assert PUBLIC_FUTURES_STREAM_HOSTS == frozenset({"fstream.binance.com"})


def test_disjoint_from_public_spot_stream_allowlist() -> None:
    assert PUBLIC_FUTURES_STREAM_HOSTS.isdisjoint(PUBLIC_HOSTS)


def test_public_spot_transport_still_rejects_fstream() -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_allowed_host("fstream.binance.com")


def test_assert_accepts_fstream() -> None:
    assert_public_futures_stream_host("fstream.binance.com")  # no raise


@pytest.mark.parametrize(
    "host",
    [
        "fapi.binance.com",  # signed futures REST — never public
        "demo-fapi.binance.com",  # demo futures REST — never public
        "stream.binance.com",  # spot public stream
        "fstream.binance.com.evil.example",  # spoofed subdomain
    ],
)
def test_assert_rejects_non_fstream(host: str) -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_public_futures_stream_host(host)
