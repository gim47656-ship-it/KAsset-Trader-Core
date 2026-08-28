from __future__ import annotations

from contextlib import contextmanager

import pytest

import app.mcp_server.tooling.fundamentals_sources_indices as sources

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Session:
    closed = False


class _LazyFastInfo:
    def __init__(self, session: _Session) -> None:
        self._session = session

    def _read(self, value: float | int) -> float | int:
        if self._session.closed:
            raise RuntimeError("session already closed")
        return value

    @property
    def last_price(self) -> float:
        return float(self._read(7730.99))

    @property
    def regular_market_previous_close(self) -> float:
        return float(self._read(7675.70))

    @property
    def open(self) -> float:
        return float(self._read(7700.00))

    @property
    def day_high(self) -> float:
        return float(self._read(7740.00))

    @property
    def day_low(self) -> float:
        return float(self._read(7680.00))

    @property
    def last_volume(self) -> int:
        return int(self._read(3_500_000_000))


async def test_us_index_reads_lazy_fast_info_before_session_close(monkeypatch):
    session = _Session()

    @contextmanager
    def fake_session():
        try:
            yield session
        finally:
            session.closed = True

    class FakeTicker:
        def __init__(self, actual_session: _Session) -> None:
            self.fast_info = _LazyFastInfo(actual_session)

    monkeypatch.setattr(sources, "yfinance_tracing_session", fake_session)
    monkeypatch.setattr(
        sources.yf,
        "Ticker",
        lambda _symbol, session=None: FakeTicker(session),
    )

    result = await sources._fetch_index_us_current("^GSPC", "S&P 500", "SPX")

    assert session.closed is True
    assert result["source"] == "yfinance"
    assert result["current"] == pytest.approx(7730.99)
    assert result["change_pct"] == pytest.approx(0.72)
