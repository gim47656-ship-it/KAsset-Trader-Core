from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_operational_kis_cancel_does_not_touch_historical_ledger():
    from app.mcp_server.tooling import orders_modify_cancel as mc

    with patch(
        "app.mcp_server.tooling.kis_live_ledger._mark_ledger_cancelled",
        new=AsyncMock(return_value=1),
    ) as mock_mark:
        out = await mc.cancel_order_impl(
            "OID-1", symbol="035420", market="kr", is_mock=False
        )

    assert out["success"] is False
    assert out["error"] == "provider kis is not operational"
    assert out["mutation_sent"] is False
    mock_mark.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_operational_kis_modify_does_not_repoint_historical_ledger():
    from app.mcp_server.tooling import orders_modify_cancel as mc

    with patch(
        "app.mcp_server.tooling.kis_live_ledger._repoint_ledger_after_modify",
        new=AsyncMock(return_value=1),
    ) as mock_repoint:
        out = await mc.modify_order_impl(
            "OLD",
            "035420",
            market="kr",
            new_price=250000.0,
            dry_run=False,
            is_mock=False,
        )

    assert out["success"] is False
    assert out["error"] == "provider kis is not operational"
    assert out["mutation_sent"] is False
    mock_repoint.assert_not_awaited()
