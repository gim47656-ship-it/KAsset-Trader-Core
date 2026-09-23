"""`symbol_master` 밖 KRX 종목(우선주 등)의 종목명 fallback 회귀 테스트.

`symbol_master`의 CHECK는 KRX에 `COMMON_STOCK`/`ETF`만 허용하므로 우선주는 그 표에
행이 없다. 이름이 없으면 앱이 종목코드를 그대로 렌더하므로, 두 이름 해석 경로가
`kr_symbol_universe`로 2차 조회하는지 여기서 고정한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.paper import PaperAccountAdapter, paper_account_adapter
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.symbol_master import SymbolMaster
from app.services.paper_trading_service import PaperTradingService


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


def _requested_symbols(statement: object) -> list[str]:
    """`in_()` 바인드는 컴파일 시 심볼 리스트 하나로 접힌다."""
    symbols: list[str] = []
    for value in statement.compile().params.values():  # type: ignore[attr-defined]
        candidates = value if isinstance(value, list | tuple) else [value]
        symbols.extend(str(item) for item in candidates if isinstance(item, str))
    return sorted(symbols)


class _NameSession:
    """`symbol_master`와 `kr_symbol_universe` 조회를 구분해 응답하는 fake 세션."""

    def __init__(
        self,
        *,
        master: list[tuple[Any, ...]] | None = None,
        universe: list[tuple[str, str]] | None = None,
        universe_error: Exception | None = None,
    ) -> None:
        self.master = list(master or [])
        self.universe = list(universe or [])
        self.universe_error = universe_error
        self.master_reads = 0
        self.universe_reads = 0
        self.universe_symbols: list[str] = []

    async def execute(self, statement: object) -> _Rows:
        entities = tuple(
            item.get("entity") for item in getattr(statement, "column_descriptions", ())
        )
        if SymbolMaster in entities:
            self.master_reads += 1
            return _Rows(self.master)
        if KRSymbolUniverse in entities:
            self.universe_reads += 1
            self.universe_symbols = _requested_symbols(statement)
            if self.universe_error is not None:
                raise self.universe_error
            # `kr_symbol_universe_service`는 `row.symbol`/`row.name`으로 읽는다.
            rows = [SimpleNamespace(symbol=s, name=n) for s, n in self.universe]
            return _Rows(rows)
        raise AssertionError(f"unexpected statement: {statement}")


def _krx(symbol: str) -> SimpleNamespace:
    return SimpleNamespace(market="KRX", symbol=symbol)


def _us(symbol: str) -> SimpleNamespace:
    return SimpleNamespace(market="US", symbol=symbol)


def _position_row(symbol: str) -> dict[str, object]:
    return {
        "instrument_type": "equity_kr",
        "symbol": symbol,
        "currency": "KRW",
        "quantity": Decimal("1"),
        "avg_price": Decimal("1"),
        "current_price": Decimal("1"),
        "evaluation_amount": Decimal("1"),
        "unrealized_pnl": Decimal("0"),
        "pnl_pct": Decimal("0"),
    }


def _closed_trade_row(symbol: str) -> dict[str, object]:
    return {
        "instrument_type": "equity_kr",
        "symbol": symbol,
        "currency": "KRW",
        "quantity": Decimal("1"),
        "cost_basis": Decimal("100"),
        "pnl_amount": Decimal("10"),
        "return_rate_pct": Decimal("10"),
        "holding_days": 3,
        "entry_date": datetime(2026, 9, 1, tzinfo=UTC),
        "exit_date": datetime(2026, 9, 4, tzinfo=UTC),
    }


@pytest.mark.asyncio
async def test_position_names_fill_preferred_shares_from_kr_symbol_universe() -> None:
    """`symbol_master`에 없는 KRX 우선주가 `kr_symbol_universe`에서 채워진다."""
    db = _NameSession(
        universe=[
            ("000155", "두산우"),
            ("000157", "두산2우B"),
            ("005935", "삼성전자우"),
        ]
    )

    names = await PaperAccountAdapter._position_names(
        db,  # type: ignore[arg-type]
        [_krx("000155"), _krx("000157"), _krx("005935")],
    )

    assert names == {
        ("KRX", "000155"): "두산우",
        ("KRX", "000157"): "두산2우B",
        ("KRX", "005935"): "삼성전자우",
    }
    # 포지션 수와 무관하게 이름 조회는 심볼 집합당 1회다.
    assert db.master_reads == 1
    assert db.universe_reads == 1
    assert db.universe_symbols == ["000155", "000157", "005935"]


@pytest.mark.asyncio
async def test_position_names_prefer_symbol_master_over_kr_universe() -> None:
    """`symbol_master`가 아는 심볼은 그 값이 이기고 2차 조회 대상이 아니다."""
    db = _NameSession(
        master=[("KRX", "005930", "삼성전자"), ("KRX", "005935", "삼성전자우")],
        universe=[("005935", "엉뚱한이름")],
    )

    names = await PaperAccountAdapter._position_names(
        db,  # type: ignore[arg-type]
        [_krx("005930"), _krx("005935")],
    )

    assert names == {
        ("KRX", "005930"): "삼성전자",
        ("KRX", "005935"): "삼성전자우",
    }
    assert db.universe_reads == 0


@pytest.mark.asyncio
async def test_position_names_leave_unresolved_and_self_named_symbols_unset() -> None:
    """어디에도 없는 심볼과 이름이 코드뿐인 행은 채우지 않는다(null 유지)."""
    db = _NameSession(
        universe=[
            ("000155", "두산우"),
            ("000158", "000158"),
            ("000159", "   "),
        ]
    )

    names = await PaperAccountAdapter._position_names(
        db,  # type: ignore[arg-type]
        [_krx("000155"), _krx("000158"), _krx("000159"), _krx("999999")],
    )

    assert names == {("KRX", "000155"): "두산우"}


@pytest.mark.asyncio
async def test_position_names_do_not_use_kr_universe_for_us_symbols() -> None:
    """US 심볼은 KRX universe로 메우지 않는다."""
    db = _NameSession(universe=[("AAPL", "애플")])

    names = await PaperAccountAdapter._position_names(
        db,  # type: ignore[arg-type]
        [_us("AAPL")],
    )

    assert names == {}
    assert db.universe_reads == 0


@pytest.mark.asyncio
async def test_position_names_survive_kr_symbol_universe_failure() -> None:
    """2차 조회가 실패해도 이미 해석한 이름은 남고 예외는 나가지 않는다."""
    db = _NameSession(
        master=[("KRX", "005930", "삼성전자")],
        universe_error=RuntimeError("universe unavailable"),
    )

    names = await PaperAccountAdapter._position_names(
        db,  # type: ignore[arg-type]
        [_krx("005930"), _krx("000155")],
    )

    assert names == {("KRX", "005930"): "삼성전자"}


@pytest.mark.asyncio
async def test_instrument_names_fill_preferred_shares_from_kr_symbol_universe() -> None:
    """시세 응답의 종목명도 같은 fallback을 쓴다(심볼 집합당 1회)."""
    db = _NameSession(
        universe=[("000155", "두산우"), ("005935", "삼성전자우")],
    )

    names = await krx_quotes._instrument_names(
        db,  # type: ignore[arg-type]
        "KRX",
        ["000155", "005935", "999999"],
    )

    assert names == {"000155": "두산우", "005935": "삼성전자우"}
    assert db.universe_reads == 1
    assert db.universe_symbols == ["000155", "005935", "999999"]


@pytest.mark.asyncio
async def test_instrument_names_prefer_symbol_master_and_skip_universe() -> None:
    """`symbol_master`가 전부 해석하면 2차 조회를 하지 않는다."""
    db = _NameSession(
        master=[("005930", "삼성전자")],
        universe=[("005930", "엉뚱한이름")],
    )

    names = await krx_quotes._instrument_names(
        db,  # type: ignore[arg-type]
        "KRX",
        ["005930"],
    )

    assert names == {"005930": "삼성전자"}
    assert db.universe_reads == 0


@pytest.mark.asyncio
async def test_instrument_names_skip_kr_universe_for_us_market() -> None:
    """US 시장 표기는 KRX universe를 조회하지 않는다."""
    db = _NameSession(universe=[("AAPL", "애플")])

    names = await krx_quotes._instrument_names(
        db,  # type: ignore[arg-type]
        "US",
        ["AAPL"],
    )

    assert names == {}
    assert db.universe_reads == 0


@pytest.mark.asyncio
async def test_instrument_names_survive_kr_symbol_universe_failure() -> None:
    """2차 조회 실패는 이름만 비우고 시세 응답을 죽이지 않는다."""
    db = _NameSession(
        master=[("005930", "삼성전자")],
        universe_error=RuntimeError("universe unavailable"),
    )

    names = await krx_quotes._instrument_names(
        db,  # type: ignore[arg-type]
        "KRX",
        ["005930", "000155"],
    )

    assert names == {"005930": "삼성전자"}


@pytest.mark.asyncio
async def test_positions_response_exposes_preferred_share_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """앱이 읽는 positions 응답 `name`까지 우선주 한글명이 채워진다."""
    db = _NameSession(
        master=[("KRX", "005930", "삼성전자")],
        universe=[
            ("000155", "두산우"),
            ("000157", "두산2우B"),
            ("005935", "삼성전자우"),
        ],
    )
    monkeypatch.setattr(
        paper_account_adapter,
        "default_account",
        AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    monkeypatch.setattr(
        PaperTradingService,
        "get_positions",
        AsyncMock(
            return_value=[
                _position_row("000155"),
                _position_row("000157"),
                _position_row("005935"),
                _position_row("005930"),
                _position_row("999999"),
            ]
        ),
    )

    response = await paper_account_adapter.positions(
        db,  # type: ignore[arg-type]
        owner_user_id=101,
    )

    assert [position.name for position in response.positions] == [
        "두산우",
        "두산2우B",
        "삼성전자우",
        "삼성전자",
        None,
    ]


@pytest.mark.asyncio
async def test_closed_trades_response_exposes_preferred_share_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """청산 내역 응답도 같은 fallback으로 우선주 한글명을 낸다."""
    db = _NameSession(
        master=[("KRX", "005930", "삼성전자")],
        universe=[("000155", "두산우")],
    )
    monkeypatch.setattr(
        paper_account_adapter,
        "default_account",
        AsyncMock(return_value=SimpleNamespace(id=7)),
    )
    monkeypatch.setattr(
        PaperTradingService,
        "list_closed_trades",
        AsyncMock(
            return_value=(
                [_closed_trade_row("000155"), _closed_trade_row("005930")],
                [],
            )
        ),
    )

    response = await paper_account_adapter.closed_trades(
        db,  # type: ignore[arg-type]
        101,
        limit=100,
    )

    assert [trade.name for trade in response.trades] == ["두산우", "삼성전자"]
