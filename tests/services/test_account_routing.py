from __future__ import annotations

import pytest

from app.services.account_routing import (
    DEFAULT_ACCOUNT_COSTS,
    AccountRoutingInput,
    build_cost_profiles,
    suggest_account_from_snapshot,
)


def _cash(
    *, kis_domestic=2_000_000, kis_overseas=2_000, toss_krw=1_000_000, toss_usd=500
):
    return {
        "accounts": [
            {
                "account": "kis_domestic",
                "broker": "kis",
                "currency": "KRW",
                "orderable": float(kis_domestic),
            },
            {
                "account": "kis_overseas",
                "broker": "kis",
                "currency": "USD",
                "orderable": float(kis_overseas),
            },
            {
                "account": "toss",
                "broker": "toss",
                "currency": "KRW",
                "orderable": float(toss_krw),
            },
            {
                "account": "toss",
                "broker": "toss",
                "currency": "USD",
                "orderable": float(toss_usd),
            },
        ],
        "summary": {"exchange_rate_usd_krw": 1500.0},
        "errors": [],
    }


def _holdings(accounts: list[str]):
    return {
        "accounts": [
            {
                "account": account,
                "positions": [
                    {
                        "symbol": "005930" if account != "kis_overseas" else "AAPL",
                        "quantity": 1,
                        "evaluation_amount": 100_000,
                    }
                ],
            }
            for account in accounts
        ],
        "errors": [],
    }


def test_default_cost_profiles_are_review_required_until_operator_override():
    profiles = build_cost_profiles(None)

    assert profiles.source == "default_seed"
    assert profiles.review_required is True
    assert profiles.threshold_bps("kr") == pytest.approx(25)
    assert profiles.threshold_bps("us") == pytest.approx(40)
    assert profiles.market_profile(
        "kis_domestic", "kr"
    ).commission_bps == pytest.approx(14.7)
    assert profiles.market_profile("toss", "us").commission_bps == pytest.approx(10)


def test_invalid_cost_profile_values_fall_back_to_review_required_defaults():
    profiles = build_cost_profiles(
        {
            "version": 1,
            "routing": {"position_consolidation_threshold_bps": {"kr": "bad"}},
            "accounts": {
                "kis_domestic": {
                    "markets": {
                        "kr": {
                            "commission_bps": "bad",
                            "fx_spread_bps": "bad",
                        }
                    }
                },
                "toss": {"limits": {"max_order_notional_krw": "bad"}},
            },
        }
    )

    assert profiles.review_required is True
    assert profiles.threshold_bps("kr") == pytest.approx(25)
    assert profiles.market_profile(
        "kis_domestic", "kr"
    ).commission_bps == pytest.approx(14.7)
    assert profiles.market_profile("kis_domestic", "kr").fx_spread_bps == pytest.approx(
        0
    )
    assert profiles.max_order_notional_krw("toss") == pytest.approx(1_000_000)


def test_invalid_cost_profile_version_uses_default_seed():
    profiles = build_cost_profiles({"version": "bad"})

    assert profiles.source == "default_seed"
    assert profiles.review_required is True
    assert profiles.threshold_bps("kr") == pytest.approx(25)


def test_no_existing_holding_recommends_cheapest_eligible_account():
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=10,
            price=75_000,
            usd_krw=None,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(),
            holdings_snapshot=_holdings([]),
        )
    )

    assert result["success"] is True
    assert result["recommended_account"] == "toss"
    assert set(result["cost_comparison"]) == {"toss"}
    assert result["cost_comparison"]["toss"]["total_cost_krw"] == pytest.approx(0)
    assert result["position_consolidation"]["decision"] == "no_existing_position"


@pytest.mark.parametrize(
    ("side", "market", "quantity", "price", "message"),
    [
        ("sell", "kr", 1, 75_000, "buy side only"),
        ("buy", "crypto", 1, 75_000, "kr/us markets only"),
        ("buy", "kr", 0, 75_000, "quantity must be positive"),
        ("buy", "kr", 1, 0, "price must be positive"),
    ],
)
def test_invalid_routing_inputs_raise_before_recommendation(
    side, market, quantity, price, message
):
    with pytest.raises(ValueError, match=message):
        suggest_account_from_snapshot(
            AccountRoutingInput(
                symbol="005930",
                market=market,
                side=side,
                quantity=quantity,
                price=price,
                usd_krw=None,
                account_costs=DEFAULT_ACCOUNT_COSTS,
                capital_snapshot=_cash(),
                holdings_snapshot=_holdings([]),
            )
        )


def test_us_routing_requires_usd_krw_rate():
    with pytest.raises(ValueError, match="usd_krw is required"):
        suggest_account_from_snapshot(
            AccountRoutingInput(
                symbol="AAPL",
                market="us",
                side="buy",
                quantity=1,
                price=100,
                usd_krw=None,
                account_costs=DEFAULT_ACCOUNT_COSTS,
                capital_snapshot=_cash(),
                holdings_snapshot=_holdings([]),
            )
        )


def test_existing_toss_holding_keeps_operational_account() -> None:
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=10,
            price=75_000,
            usd_krw=None,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(),
            holdings_snapshot=_holdings(["toss"]),
        )
    )

    assert result["recommended_account"] == "toss"
    assert result["position_consolidation"]["existing_accounts"] == ["toss"]
    assert result["position_consolidation"]["decision"] == "keep_existing"


def test_historical_kis_holding_is_not_an_operational_routing_candidate() -> None:
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=10,
            price=75_000,
            usd_krw=None,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(),
            holdings_snapshot={
                "accounts": [
                    {
                        "account": "kis",
                        "broker": "kis",
                        "positions": [{"symbol": "005930", "quantity": 1}],
                    }
                ],
                "errors": [],
            },
        )
    )

    assert result["recommended_account"] == "toss"
    assert result["position_consolidation"]["existing_accounts"] == []
    assert result["position_consolidation"]["decision"] == "no_existing_position"


def test_us_routing_uses_toss_as_the_only_operational_candidate() -> None:
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="AAPL",
            market="us",
            side="buy",
            quantity=2,
            price=100,
            usd_krw=1500,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(),
            holdings_snapshot=_holdings(["kis_overseas"]),
        )
    )

    assert result["recommended_account"] == "toss"
    assert set(result["cost_comparison"]) == {"toss"}
    assert result["position_consolidation"]["existing_accounts"] == []
    assert result["position_consolidation"]["decision"] == "no_existing_position"


def test_toss_notional_cap_fails_closed_without_kis_fallback() -> None:
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=20,
            price=75_000,
            usd_krw=None,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(kis_domestic=2_000_000, toss_krw=2_000_000),
            holdings_snapshot=_holdings([]),
        )
    )

    assert result["success"] is False
    assert result["recommended_account"] is None
    assert set(result["cost_comparison"]) == {"toss"}
    assert result["cost_comparison"]["toss"]["eligible"] is False
    assert (
        result["cost_comparison"]["toss"]["ineligible_reason"]
        == "notional_limit_exceeded"
    )


def test_no_eligible_account_returns_only_toss_evidence() -> None:
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=10,
            price=75_000,
            usd_krw=None,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(kis_domestic=2_000_000, toss_krw=0),
            holdings_snapshot=_holdings([]),
        )
    )

    assert result["success"] is False
    assert result["recommended_account"] is None
    assert set(result["cost_comparison"]) == {"toss"}
    assert (
        result["cost_comparison"]["toss"]["ineligible_reason"]
        == "insufficient_orderable_cash"
    )
