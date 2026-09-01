from app.mcp_server.tooling.analysis_tool_handlers import _summarize_analysis_result


def test_summarize_analysis_result_passes_us_quote_session_freshness_fields():
    result = _summarize_analysis_result(
        "NVDA",
        {
            "market_type": "equity_us",
            "source": "toss",
            "quote": {
                "symbol": "NVDA",
                "instrument_type": "equity_us",
                "price": 195.29,
                "source": "toss",
                "session": "premarket",
                "data_state": "fresh",
                "price_source": "toss_price",
                "venue": "NASD",
                "price_as_of": "2026-07-06T08:45:12-04:00",
                "delayed": True,
            },
            "indicators": {"rsi": {"14": 61.2}},
            "support_resistance": {"supports": [], "resistances": []},
            "opinions": {"consensus": {"rating": "buy"}},
            "recommendation": {"action": "hold"},
        },
    )

    assert result["current_price"] == 195.29
    assert result["session"] == "premarket"
    assert result["data_state"] == "fresh"
    assert result["price_source"] == "toss_price"
    assert result["venue"] == "NASD"
    assert result["price_as_of"] == "2026-07-06T08:45:12-04:00"
    assert result["delayed"] is True
