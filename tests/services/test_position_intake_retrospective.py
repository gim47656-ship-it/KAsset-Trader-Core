"""ROB-1285 — guarded position-intake retrospective path."""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast, TradeRetrospective
from app.services import decision_history
from app.services.trade_journal import trade_retrospective_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()


def _historical_intake() -> TradeRetrospective:
    """A persisted pre-cutover intake row; the live writer no longer exists."""
    return TradeRetrospective(
        symbol="012030",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="unfilled",
        correlation_id="position-intake:kis_live:012030:historical",
        fill_evidence_available=False,
        evidence_snapshot={
            "retrospective_type": "intake",
            "position_intake": {
                "account_mode": "kis_live",
                "account_ref": "kis-live-primary",
                "market": "equity_kr",
            },
        },
    )


@pytest.mark.asyncio
async def test_generic_retrospective_cannot_spoof_intake_type(
    db_session: AsyncSession,
):
    with pytest.raises(
        svc.RetrospectiveValidationError,
        match="retrospective_type is reserved",
    ):
        await svc.save_retrospective(
            db_session,
            symbol="012030",
            instrument_type="equity_kr",
            account_mode="kis_live",
            outcome="unfilled",
            evidence_snapshot={"retrospective_type": "intake"},
        )


@pytest.mark.asyncio
async def test_generic_upsert_cannot_retype_or_mutate_existing_intake(
    db_session: AsyncSession,
):
    intake = _historical_intake()
    db_session.add(intake)
    await db_session.commit()

    with pytest.raises(
        svc.RetrospectiveValidationError,
        match="historical intake rows are read-only",
    ):
        await svc.save_retrospective(
            db_session,
            symbol="012030",
            instrument_type="equity_kr",
            account_mode="kis_live",
            outcome="filled",
            correlation_id=intake.correlation_id,
            evidence_snapshot={},
        )

    await db_session.refresh(intake)
    assert svc.serialize_retrospective(intake)["retrospective_type"] == "intake"
    assert intake.outcome == "unfilled"


@pytest.mark.asyncio
async def test_intake_is_excluded_by_actual_learning_aggregate_consumer(
    db_session: AsyncSession,
):
    db_session.add(_historical_intake())
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        strategy_key="execution-strategy",
        realized_pnl=1000,
        realized_pnl_currency="KRW",
        pnl_pct=1.5,
    )
    await db_session.commit()

    result = await svc.build_retrospective_aggregate(
        db_session,
        group_by="trigger_type",
    )

    assert result["excluded_intake"] == 1
    assert sum(group["sample_size"] for group in result["groups"]) == 1
    assert result["groups"][0]["by_outcome"] == {"filled": 1}
    forecasts = (
        await db_session.execute(select(func.count()).select_from(TradeForecast))
    ).scalar_one()
    assert forecasts == 0


@pytest.mark.asyncio
async def test_intake_is_excluded_from_decision_history_learning_context(
    db_session: AsyncSession,
):
    db_session.add(_historical_intake())
    await svc.save_retrospective(
        db_session,
        symbol="012030",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        pnl_pct=Decimal("1.25"),
        lesson="execution lesson remains eligible",
    )
    await db_session.commit()

    lessons, outcomes = await decision_history._retrospectives(
        db_session,
        "012030",
        "kis_live",
    )

    assert lessons == ["execution lesson remains eligible"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "filled"
    assert outcomes[0]["pnl_pct"] == 1.25


@pytest.mark.asyncio
async def test_existing_execution_retrospective_path_regresses_zero(
    db_session: AsyncSession,
):
    action, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        evidence_snapshot={"broker_evidence": "fixture"},
    )
    await db_session.commit()

    assert action == "created"
    assert row.fill_evidence_available is True
    assert svc.serialize_retrospective(row)["retrospective_type"] == "execution"
