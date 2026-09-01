from __future__ import annotations

import pytest

from app.mcp_server.tooling.analysis_tool_handlers import _summarize_analysis_result


@pytest.mark.unit
def test_summary_copies_nxt_fields_from_quote():
    analysis = {
        "market_type": "equity_kr",
        "source": "toss",
        "quote": {
            "price": 70000,
            "nxt_tradable": True,
            "nxt_tradable_source": "kr_symbol_universe",
            "nxt_tradable_asof": "2026-07-03T06:00:00+09:00",
            "nxt_tradable_stale": False,
        },
    }
    summary = _summarize_analysis_result("005930", analysis)
    assert summary["nxt_tradable"] is True
    assert summary["nxt_tradable_source"] == "kr_symbol_universe"
    assert summary["nxt_tradable_asof"] == "2026-07-03T06:00:00+09:00"


@pytest.mark.unit
def test_summary_drops_legacy_nxt_quote_provenance():
    analysis = {
        "market_type": "equity_kr",
        "source": "toss",
        "quote": {
            "price": 2082500.0,
            "price_source": "nxt_mid",
            "session": "nxt_premarket",
            "session_state": "premarket",
            "krx_prev_close": 1913000.0,
            "change_pct": 8.86,
            "quote_asof": "2026-07-03T08:30:00+09:00",
        },
    }

    summary = _summarize_analysis_result("000660", analysis)

    assert summary["current_price"] is None
    assert "price_source" not in summary
    assert "session" not in summary
    assert "session_state" not in summary
    assert "krx_prev_close" not in summary
    assert "change_pct" not in summary
    assert "quote_asof" not in summary


@pytest.mark.unit
def test_summary_us_has_no_nxt_fields():
    analysis = {
        "market_type": "equity_us",
        "source": "toss",
        "quote": {"price": 100.0},
    }
    summary = _summarize_analysis_result("AAPL", analysis)
    assert "nxt_tradable" not in summary
