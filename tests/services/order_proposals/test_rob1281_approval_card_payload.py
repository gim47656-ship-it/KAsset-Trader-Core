"""KIS 제안 경로 제거 회귀 테스트."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.revalidation import _default_place_order_fn
from app.services.order_proposals.service import RungInput


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_mode", "market", "symbol"),
    [
        ("kis_live", "equity_kr", "005930"),
        ("kis_live", "equity_us", "AAPL"),
        ("kis_mock", "equity_kr", "005930"),
    ],
)
async def test_kis_proposal_creation_is_rejected(
    db_session,
    account_mode: str,
    market: str,
    symbol: str,
):
    with pytest.raises(OrderProposalError, match="unsupported account_mode/market"):
        await OrderProposalsService(db_session).create_proposal(
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            side="sell",
            order_type="limit",
            proposer="cutover-regression",
            rungs=[RungInput(0, "sell", Decimal("1"), Decimal("100"), None)],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
async def test_legacy_kis_intent_is_not_rerouted_to_toss(account_mode: str):
    result = await _default_place_order_fn(
        account_mode=account_mode,
        proposal_client_order_id="legacy-kis-intent",
        symbol="005930",
        market="equity_kr",
        side="sell",
        order_type="limit",
        quantity=Decimal("1"),
        price=Decimal("100"),
        dry_run=True,
    )

    assert result == {
        "success": False,
        "mutation_sent": False,
        "account_mode": account_mode,
        "error": "provider kis is not operational",
    }
