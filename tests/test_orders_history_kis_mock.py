import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us"])
async def test_get_order_history_pending_mock_surfaces_unsupported(
    market: str,
):
    """Removed KIS mock intents fail closed without restoring a KIS client seam."""
    from app.mcp_server.tooling import orders_history

    result = await orders_history.get_order_history_impl(
        status="pending", market=market, is_mock=True
    )

    assert result == {
        "success": False,
        "account_mode": "kis_mock",
        "error": "provider kis is not operational",
        "detail": "KIS mock order history is provider_unsupported",
        "source": "unsupported",
        "orders": [],
        "errors": [
            {
                "market": market,
                "error": "provider kis is not operational",
            }
        ],
    }
