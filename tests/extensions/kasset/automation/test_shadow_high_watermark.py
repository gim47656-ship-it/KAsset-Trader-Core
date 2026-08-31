from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions.kasset.automation.shadow_high_watermark import (
    SHADOW_HIGH_WATERMARK_SCHEMA_VERSION,
    ShadowBuyState,
    ShadowEquityValuation,
    ShadowEvidenceStatus,
    ShadowHighWatermarkState,
    ShadowHighWatermarkThresholds,
    ShadowReasonCode,
    ShadowReductionStage,
    evaluate_and_persist_shadow_high_watermark,
    evaluate_shadow_high_watermark,
    persist_shadow_high_watermark,
)

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
def thresholds() -> ShadowHighWatermarkThresholds:
    return ShadowHighWatermarkThresholds(
        profit_target_stages=(
            ShadowReductionStage("profit-1", Decimal("0.05"), Decimal("0.75")),
            ShadowReductionStage("profit-2", Decimal("0.10"), Decimal("0.50")),
        ),
        peak_drawdown_stages=(
            ShadowReductionStage("drawdown-1", Decimal("0.03"), Decimal("0.80")),
            ShadowReductionStage("drawdown-2", Decimal("0.05"), Decimal("0.40")),
        ),
        maximum_loss_ratio=Decimal("0.12"),
        max_valuation_age=timedelta(minutes=5),
    )


def _valuation(
    equity: str,
    *,
    at: datetime = _NOW,
    evaluated_at: datetime | None = None,
    reference_equity: str | None = None,
    source: str = "paper-account-snapshot",
) -> ShadowEquityValuation:
    return ShadowEquityValuation(
        owner_user_id=17,
        account_key="paper:alpha",
        market="KRX",
        equity=Decimal(equity),
        valuation_at=at,
        evaluated_at=evaluated_at or at,
        valuation_source=source,
        reference_equity=(
            Decimal(reference_equity) if reference_equity is not None else None
        ),
    )


def _apply(
    equity: str,
    *,
    thresholds: ShadowHighWatermarkThresholds,
    previous: ShadowHighWatermarkState | None = None,
    at: datetime = _NOW,
    reference_equity: str | None = None,
):
    return evaluate_shadow_high_watermark(
        _valuation(equity, at=at, reference_equity=reference_equity),
        thresholds=thresholds,
        previous=previous,
    )


def test_thresholds_are_immutable_serializable_and_separately_fingerprinted(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    serialized = thresholds.as_serializable()

    assert json.loads(json.dumps(serialized)) == serialized
    assert serialized["configSchemaVersion"] == (
        "kasset.shadow-high-watermark-config.v1"
    )
    assert len(thresholds.fingerprint) == 64
    with pytest.raises(FrozenInstanceError):
        thresholds.maximum_loss_ratio = Decimal("0.20")  # type: ignore[misc]


def test_peak_is_monotonic_and_state_version_advances(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    opened = _apply("100", thresholds=thresholds)
    raised = _apply(
        "112",
        thresholds=thresholds,
        previous=opened.state,
        at=_NOW + timedelta(minutes=1),
    )
    lowered = _apply(
        "108",
        thresholds=thresholds,
        previous=raised.state,
        at=_NOW + timedelta(minutes=2),
    )

    assert opened.state is not None
    assert raised.state is not None
    assert lowered.state is not None
    assert raised.state.peak_equity == Decimal("112")
    assert lowered.state.peak_equity == Decimal("112")
    assert lowered.state.current_equity == Decimal("108")
    assert lowered.state.state_version == 3


def test_profit_target_equality_and_later_stage_reduce_hypothetical_buy(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    opened = _apply("100", thresholds=thresholds)
    first_target = _apply(
        "105",
        thresholds=thresholds,
        previous=opened.state,
        at=_NOW + timedelta(minutes=1),
    )
    second_target = _apply(
        "110",
        thresholds=thresholds,
        previous=first_target.state,
        at=_NOW + timedelta(minutes=2),
    )

    assert first_target.buy_state == ShadowBuyState.STAGED_REDUCTION
    assert first_target.buy_multiplier == Decimal("0.75")
    assert [stage.name for stage in first_target.triggered_stages] == ["profit-1"]
    assert second_target.buy_multiplier == Decimal("0.50")
    assert [stage.name for stage in second_target.triggered_stages] == [
        "profit-1",
        "profit-2",
    ]


def test_peak_drawdown_equality_and_breach_select_deepest_stage(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    opened = _apply("100", thresholds=thresholds, reference_equity="110")
    high = _apply(
        "110",
        thresholds=thresholds,
        previous=opened.state,
        at=_NOW + timedelta(minutes=1),
    )
    equality = _apply(
        "106.70",
        thresholds=thresholds,
        previous=high.state,
        at=_NOW + timedelta(minutes=2),
    )
    breach = _apply(
        "104.49",
        thresholds=thresholds,
        previous=high.state,
        at=_NOW + timedelta(minutes=3),
    )

    assert equality.peak_drawdown_ratio == Decimal("0.03")
    assert equality.buy_multiplier == Decimal("0.80")
    assert [stage.name for stage in equality.triggered_stages] == ["drawdown-1"]
    assert breach.peak_drawdown_ratio is not None
    assert breach.peak_drawdown_ratio > Decimal("0.05")
    assert breach.buy_multiplier == Decimal("0.40")
    assert [stage.name for stage in breach.triggered_stages] == [
        "drawdown-1",
        "drawdown-2",
    ]


def test_maximum_loss_equality_is_exit_only_but_sell_remains_allowed(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    opened = _apply("100", thresholds=thresholds)
    result = _apply(
        "88",
        thresholds=thresholds,
        previous=opened.state,
        at=_NOW + timedelta(minutes=1),
    )

    assert result.buy_state == ShadowBuyState.EXIT_ONLY
    assert result.buy_multiplier == Decimal("0")
    assert result.sell_risk_reduction_allowed is True
    assert ShadowReasonCode.MAXIMUM_LOSS_REACHED in {
        reason.code for reason in result.reasons
    }


def test_market_local_trading_date_rollover_resets_daily_state(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    before_midnight = datetime(2026, 8, 31, 14, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)
    previous = _apply("120", thresholds=thresholds, at=before_midnight)
    rolled = _apply(
        "90",
        thresholds=thresholds,
        previous=previous.state,
        at=after_midnight,
        reference_equity="95",
    )

    assert previous.state is not None
    assert rolled.state is not None
    assert previous.state.trading_date.isoformat() == "2026-08-31"
    assert rolled.state.trading_date.isoformat() == "2026-09-01"
    assert rolled.state.session_opening_equity == Decimal("90")
    assert rolled.state.reference_equity == Decimal("95")
    assert rolled.state.peak_equity == Decimal("90")
    assert rolled.state.state_version == 1
    assert rolled.reasons[0].code == ShadowReasonCode.TRADING_DAY_ROLLOVER


@pytest.mark.parametrize("equity", ["0", "NaN", "Infinity"])
def test_zero_or_nonfinite_valuation_fails_closed(
    thresholds: ShadowHighWatermarkThresholds,
    equity: str,
) -> None:
    result = _apply(equity, thresholds=thresholds)

    assert result.status == ShadowEvidenceStatus.FAIL_CLOSED
    assert result.buy_state == ShadowBuyState.EXIT_ONLY
    assert result.buy_multiplier == Decimal("0")
    assert result.sell_risk_reduction_allowed is True
    assert result.persistence_required is False
    assert result.reasons[0].code == ShadowReasonCode.INVALID_EQUITY


def test_stale_valuation_fails_closed_with_source_timestamps(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    result = evaluate_shadow_high_watermark(
        _valuation(
            "100",
            at=_NOW,
            evaluated_at=_NOW + timedelta(minutes=6),
        ),
        thresholds=thresholds,
    )
    evidence = result.as_evidence()

    assert result.status == ShadowEvidenceStatus.FAIL_CLOSED
    assert result.reasons[0].code == ShadowReasonCode.STALE_VALUATION
    assert evidence["sourceTimestamps"] == {
        "valuationAt": _NOW.isoformat(),
        "evaluatedAt": (_NOW + timedelta(minutes=6)).isoformat(),
        "stateValuationAt": None,
    }


def test_missing_source_is_insufficient_and_never_actionable(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    result = evaluate_shadow_high_watermark(
        _valuation("100", source=""),
        thresholds=thresholds,
    )

    assert result.status == ShadowEvidenceStatus.INSUFFICIENT
    assert result.buy_state == ShadowBuyState.EXIT_ONLY
    assert result.sell_risk_reduction_allowed is True
    hypothetical = result.as_evidence()["hypothetical"]
    assert hypothetical["buyActionable"] is False  # type: ignore[index]


def test_evidence_has_closed_shadow_schema_and_deterministic_stage_order(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    opened = _apply("100", thresholds=thresholds, reference_equity="100")
    high = _apply(
        "120",
        thresholds=thresholds,
        previous=opened.state,
        at=_NOW + timedelta(minutes=1),
    )
    result = _apply(
        "110",
        thresholds=thresholds,
        previous=high.state,
        at=_NOW + timedelta(minutes=2),
    )
    evidence = result.as_evidence()

    assert set(evidence) == {
        "schemaVersion",
        "mode",
        "status",
        "scope",
        "sourceTimestamps",
        "valuationSource",
        "thresholdConfig",
        "hypothetical",
        "metrics",
        "triggeredStages",
        "reasons",
        "state",
    }
    assert evidence["schemaVersion"] == SHADOW_HIGH_WATERMARK_SCHEMA_VERSION
    assert evidence["mode"] == "SHADOW"
    assert evidence["status"] == "valid"
    triggered_stages = evidence["triggeredStages"]
    assert [item["kind"] for item in triggered_stages] == [  # type: ignore[union-attr]
        "PROFIT_TARGET",
        "PROFIT_TARGET",
        "PEAK_DRAWDOWN",
        "PEAK_DRAWDOWN",
    ]
    fingerprint = evidence["thresholdConfig"]["fingerprint"]  # type: ignore[index]
    assert isinstance(fingerprint, str)
    assert len(fingerprint) == 64


class _ScalarSequenceDb:
    def __init__(self, *results: object | None) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.results.pop(0)


def _row(state: ShadowHighWatermarkState) -> SimpleNamespace:
    return SimpleNamespace(
        owner_user_id=state.owner_user_id,
        account_key=state.account_key,
        market=state.market,
        trading_date=state.trading_date,
        session_opening_equity=state.session_opening_equity,
        reference_equity=state.reference_equity,
        peak_equity=state.peak_equity,
        current_equity=state.current_equity,
        valuation_at=state.valuation_at,
        valuation_source=state.valuation_source,
        state_version=state.state_version,
    )


@pytest.mark.asyncio
async def test_db_update_uses_optimistic_successor_version(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    opened = _apply("100", thresholds=thresholds)
    advanced = _apply(
        "101",
        thresholds=thresholds,
        previous=opened.state,
        at=_NOW + timedelta(minutes=1),
    )
    assert advanced.state is not None
    db = _ScalarSequenceDb(_row(advanced.state))

    persisted = await persist_shadow_high_watermark(  # type: ignore[arg-type]
        db,
        advanced,
    )

    assert persisted.state_version == 2
    assert [statement.__class__.__name__ for statement in db.statements] == ["Update"]


@pytest.mark.asyncio
async def test_db_insert_conflict_is_idempotent_for_the_same_state(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    evaluation = _apply("100", thresholds=thresholds)
    assert evaluation.state is not None
    db = _ScalarSequenceDb(None, _row(evaluation.state))

    persisted = await persist_shadow_high_watermark(  # type: ignore[arg-type]
        db,
        evaluation,
    )

    assert persisted == evaluation.state
    assert [statement.__class__.__name__ for statement in db.statements] == [
        "Insert",
        "Select",
    ]


@pytest.mark.asyncio
async def test_restart_recovers_row_and_replay_does_not_write_again(
    thresholds: ShadowHighWatermarkThresholds,
) -> None:
    valuation = _valuation("100")
    expected = evaluate_shadow_high_watermark(valuation, thresholds=thresholds)
    assert expected.state is not None
    persisted_row = _row(expected.state)

    first_process = _ScalarSequenceDb(None, persisted_row)
    first = await evaluate_and_persist_shadow_high_watermark(  # type: ignore[arg-type]
        first_process,
        valuation,
        thresholds=thresholds,
    )
    restarted_process = _ScalarSequenceDb(persisted_row)
    recovered = await evaluate_and_persist_shadow_high_watermark(
        restarted_process,  # type: ignore[arg-type]
        valuation,
        thresholds=thresholds,
    )

    assert first.state == expected.state
    assert recovered.state == expected.state
    assert recovered.reasons[0].code == ShadowReasonCode.IDEMPOTENT_REPLAY
    assert recovered.persistence_required is False
    assert [statement.__class__.__name__ for statement in first_process.statements] == [
        "Select",
        "Insert",
    ]
    assert [
        statement.__class__.__name__ for statement in restarted_process.statements
    ] == ["Select"]
