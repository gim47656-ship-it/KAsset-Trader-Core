# tests/mcp_server/test_analyze_stock_pipeline_compat.py
"""ROB-396: pipeline 플래그와 무관하게 analyze_stock_impl의 Toss source가
결정적으로 유지되는지 확인한다."""

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_analyze import analyze_stock_impl


@pytest.fixture
def mock_ohlcv_df():
    return pd.DataFrame(
        {
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "volume": [1000],
            "value": [105000.0],
        },
        index=[pd.Timestamp.now()],
    )


@pytest.mark.asyncio
async def test_pipeline_flags_do_not_flip_source(mock_ohlcv_df):
    """RESEARCH_PIPELINE 플래그가 켜져도 Toss source를 유지한다."""

    with patch("app.core.config.settings") as mock_settings:
        mock_settings.RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED = True
        mock_settings.RESEARCH_PIPELINE_ENABLED = True

        with (
            patch(
                "app.mcp_server.tooling.analysis_analyze._fetch_ohlcv_for_indicators",
                new_callable=AsyncMock,
                return_value=mock_ohlcv_df,
            ),
            patch(
                "app.mcp_server.tooling.analysis_analyze._get_quote_impl",
                new_callable=AsyncMock,
                return_value={
                    "price": 105.0,
                    "symbol": "AAPL",
                    "instrument_type": "equity_us",
                    "source": "toss",
                },
            ),
            patch(
                "app.mcp_server.tooling.analysis_analyze._get_indicators_impl",
                new_callable=AsyncMock,
                return_value={"rsi": {"value": 50}},
            ),
            patch(
                "app.mcp_server.tooling.analysis_analyze._get_support_resistance_impl",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            first = await analyze_stock_impl("AAPL")
            second = await analyze_stock_impl("AAPL")

    assert first["source"] == "toss"
    assert first["source"] != "research_pipeline"
    # 결정적: 반복 호출에 source/판정 불변
    assert first["source"] == second["source"]
    assert first["recommendation"]["action"] == second["recommendation"]["action"]


@pytest.mark.asyncio
async def test_no_research_pipeline_symbols_in_module():
    """분기 제거 회귀: 모듈에서 pipeline 합성 헬퍼가 사라졌다."""

    import app.mcp_server.tooling.analysis_analyze as mod

    assert not hasattr(mod, "_get_pipeline_result")
    assert not hasattr(mod, "_map_pipeline_to_analysis")
