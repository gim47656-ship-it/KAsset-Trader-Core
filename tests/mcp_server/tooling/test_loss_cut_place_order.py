from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import order_validation as ov


@pytest.mark.unit
@pytest.mark.asyncio
async def test_defensive_trim_path_ignores_loss_cut_plumbing():
    # A defensive_trim-style call with exit_intent=None resolves no loss_cut context,
    # and the validator short-circuits without touching any loss_cut-only helpers.
    with patch.object(
        ov,
        "_get_retrospective_by_id_for_loss_cut",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        ctx, errors = await ov._validate_loss_cut_preconditions(
            exit_intent=None,
            retrospective_id=None,
            exit_reason=None,
            approval_issue_id=None,
            side="sell",
            order_type="limit",
            is_mock=False,
            symbol="KRW-DOT",
        )
    assert ctx is None and errors == []
