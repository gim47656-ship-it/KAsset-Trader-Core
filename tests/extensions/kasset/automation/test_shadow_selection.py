from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.extensions.kasset.automation.candidate_ranker import CandidateRankResult
from app.extensions.kasset.automation.shadow_selection import (
    SHADOW_SELECTION_EVIDENCE_VERSION,
    SHADOW_SELECTION_SCHEMA_VERSION,
    UNKNOWN_SECTOR,
    ShadowAdjustmentKind,
    ShadowAtrCeiling,
    ShadowSectorExposure,
    ShadowSelectionConfig,
    ShadowSelectionPosition,
    ShadowSelectionStatus,
    allocate_shadow_targets,
)

D = Decimal
NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


def _rank(
    symbol: str,
    score: str,
    *,
    market: str = "KR",
    data_as_of: datetime = NOW,
) -> CandidateRankResult:
    return CandidateRankResult(
        symbol=symbol,
        market=market,  # type: ignore[arg-type]
        total_score=D(score),
        factor_scores=(),
        penalties=(),
        data_as_of=data_as_of,
        valid_until=NOW,
        exclusion_reason=None,
        atr_14=D("1"),
        average_volume_20=D("100"),
        average_turnover_20=D("1000"),
        evidence=(),
        sources=("repository",),
        is_held=False,
        is_watchlisted=False,
        eligible_for_new_buy=True,
    )


def _position(
    symbol: str,
    weight: str,
    sector: str | None,
    *,
    market: str = "KR",
) -> ShadowSelectionPosition:
    return ShadowSelectionPosition(
        market=market,  # type: ignore[arg-type]
        symbol=symbol,
        current_weight=D(weight),
        sector_key=sector,
        source_timestamp=NOW,
    )


def _sector(key: str, weight: str) -> ShadowSectorExposure:
    return ShadowSectorExposure(
        sector_key=key,
        projected_weight=D(weight),
        source_timestamp=NOW,
    )


def _config(**changes: object) -> ShadowSelectionConfig:
    values: dict[str, object] = {
        "top_k": 2,
        "target_investment_weight": D("0.8"),
        "maximum_rebalance_delta": D("1"),
        "no_trade_band": D("0"),
        "sector_weight_cap": D("1"),
    }
    values.update(changes)
    return ShadowSelectionConfig(**values)  # type: ignore[arg-type]


def _targets(result: object) -> dict[str, Decimal]:
    return {
        item.symbol: item.target_weight
        for item in result.allocations  # type: ignore[attr-defined]
    }


def test_cold_start_assigns_equal_top_k_targets_and_closed_shadow_evidence() -> None:
    result = allocate_shadow_targets(
        (_rank("B", "0.8"), _rank("A", "0.9")),
        (_position("A", "0", "TECH"), _position("B", "0", "HEALTH")),
        (_sector("TECH", "0"), _sector("HEALTH", "0")),
        evaluated_at=NOW,
        config=_config(),
    )

    assert result.status == ShadowSelectionStatus.VALID
    assert result.selected_keys == (("KR", "A"), ("KR", "B"))
    assert _targets(result) == {"A": D("0.40000000"), "B": D("0.40000000")}
    assert result.mode == "SHADOW"
    assert result.schema_version == SHADOW_SELECTION_SCHEMA_VERSION
    assert result.evidence_version == SHADOW_SELECTION_EVIDENCE_VERSION
    assert result.source_timestamps == (NOW,)
    assert len(result.config_fingerprint) == 64
    payload = result.as_evidence()
    assert "orders" not in payload
    assert payload["mode"] == "SHADOW"


def test_replacement_reduces_non_top_k_before_funding_new_top_name() -> None:
    result = allocate_shadow_targets(
        (_rank("NEW", "0.9"), _rank("OLD", "0.2")),
        (_position("OLD", "0.6", "OLD"), _position("NEW", "0", "NEW")),
        (_sector("OLD", "0.6"), _sector("NEW", "0")),
        evaluated_at=NOW,
        config=_config(
            top_k=1,
            target_investment_weight=D("0.6"),
            maximum_rebalance_delta=D("0.2"),
        ),
    )

    assert _targets(result) == {"NEW": D("0.20000000"), "OLD": D("0.40000000")}
    assert result.released_weight == D("0.20000000")
    assert result.buy_budget == D("0.20000000")
    assert [item.kind for item in result.evidence] == [
        ShadowAdjustmentKind.REDUCE_NON_TOP_K,
        ShadowAdjustmentKind.INCREASE_UNDERWEIGHT,
    ]


def test_selected_overweight_reduction_funds_underweight_increase() -> None:
    result = allocate_shadow_targets(
        (_rank("HEAVY", "0.9"), _rank("LIGHT", "0.8")),
        (
            _position("HEAVY", "0.6", "A"),
            _position("LIGHT", "0", "B"),
        ),
        (_sector("A", "0.6"), _sector("B", "0")),
        evaluated_at=NOW,
        config=_config(target_investment_weight=D("0.6")),
    )

    assert _targets(result) == {
        "HEAVY": D("0.30000000"),
        "LIGHT": D("0.30000000"),
    }
    assert result.preexisting_investment_headroom == D("0E-8")
    assert result.released_weight == D("0.30000000")
    assert result.buy_budget == D("0.30000000")
    assert result.evidence[0].kind == ShadowAdjustmentKind.REDUCE_OVERWEIGHT


def test_buy_budget_is_allocated_in_proportion_to_underweight_shortfall() -> None:
    result = allocate_shadow_targets(
        (_rank("A", "0.9"), _rank("B", "0.8"), _rank("OLD", "0.1")),
        (
            _position("A", "0", "A"),
            _position("B", "0.1", "B"),
            _position("OLD", "0.5", "OLD"),
        ),
        (_sector("A", "0"), _sector("B", "0.1"), _sector("OLD", "0.5")),
        evaluated_at=NOW,
        config=_config(
            target_investment_weight=D("0.6"),
            maximum_rebalance_delta=D("0.15"),
        ),
    )

    assert result.buy_budget == D("0.15000000")
    assert _targets(result) == {
        "A": D("0.09000000"),
        "B": D("0.16000000"),
        "OLD": D("0.35000000"),
    }


def test_each_name_change_is_capped_from_its_original_weight() -> None:
    result = allocate_shadow_targets(
        (_rank("A", "1"),),
        (_position("A", "0", "A"),),
        (_sector("A", "0"),),
        evaluated_at=NOW,
        config=_config(
            top_k=1,
            target_investment_weight=D("0.8"),
            maximum_rebalance_delta=D("0.2"),
        ),
    )

    assert _targets(result)["A"] == D("0.20000000")
    assert result.allocations[0].comparison_delta == D("0.20000000")


def test_no_trade_band_keeps_a_small_shortfall_unchanged() -> None:
    result = allocate_shadow_targets(
        (_rank("A", "1"),),
        (_position("A", "0.49", "A"),),
        (_sector("A", "0.49"),),
        evaluated_at=NOW,
        config=_config(
            top_k=1,
            target_investment_weight=D("0.5"),
            no_trade_band=D("0.02"),
        ),
    )

    assert _targets(result)["A"] == D("0.49000000")
    assert result.evidence[0].kind == ShadowAdjustmentKind.NO_TRADE_BAND
    assert result.evidence[0].comparison_delta == D("0E-8")


def test_sector_projection_is_updated_per_adjustment_and_hard_caps_target() -> None:
    result = allocate_shadow_targets(
        (_rank("A", "1"), _rank("B", "0.9")),
        (_position("A", "0", "TECH"), _position("B", "0", "TECH")),
        (_sector("TECH", "0"),),
        evaluated_at=NOW,
        config=_config(sector_weight_cap=D("0.5")),
    )

    assert _targets(result) == {"A": D("0.25000000"), "B": D("0.25000000")}
    assert result.projected_sector_exposures == (("TECH", D("0.50000000")),)
    assert result.evidence[-1].kind == ShadowAdjustmentKind.SECTOR_CAP
    assert result.evidence[-1].sector_before_weight == D("0.25000000")
    assert result.evidence[-1].sector_after_weight == D("0.50000000")


def test_invalid_calculation_fails_closed_without_increasing_any_name() -> None:
    result = allocate_shadow_targets(
        (replace(_rank("A", "1"), total_score=D("NaN")),),
        (_position("A", "0.2", "TECH"),),
        (_sector("TECH", "0.2"),),
        evaluated_at=NOW,
        config=_config(top_k=1),
    )

    assert result.status == ShadowSelectionStatus.FAIL_CLOSED
    assert _targets(result)["A"] == D("0.20000000")
    assert result.buy_budget == D("0")
    assert result.sell_risk_reduction_allowed is True
    assert result.evidence[0].kind == ShadowAdjustmentKind.FAIL_CLOSED


def test_failed_atr_input_fails_closed_but_keeps_risk_reduction_allowed() -> None:
    result = allocate_shadow_targets(
        (_rank("A", "1"),),
        (_position("A", "0.2", "TECH"),),
        (_sector("TECH", "0.2"),),
        evaluated_at=NOW,
        atr_ceilings=(
            ShadowAtrCeiling(
                market="KR",
                symbol="A",
                status=ShadowSelectionStatus.FAIL_CLOSED,
                maximum_allocation_weight=None,
                maximum_quantity=None,
                source_timestamp=NOW,
            ),
        ),
        config=_config(top_k=1),
    )

    assert result.status == ShadowSelectionStatus.FAIL_CLOSED
    assert _targets(result)["A"] == D("0.20000000")
    assert result.allocations[0].sell_risk_reduction_allowed is True


def test_hypothetical_target_never_exceeds_existing_atr_allocation_ceiling() -> None:
    result = allocate_shadow_targets(
        (_rank("A", "1"),),
        (_position("A", "0", "TECH"),),
        (_sector("TECH", "0"),),
        evaluated_at=NOW,
        atr_ceilings=(
            ShadowAtrCeiling(
                market="KR",
                symbol="A",
                status=ShadowSelectionStatus.VALID,
                maximum_allocation_weight=D("0.15"),
                maximum_quantity=D("7"),
                source_timestamp=NOW,
            ),
        ),
        config=_config(top_k=1),
    )

    allocation = result.allocations[0]
    assert allocation.atr_allocation_ceiling is not None
    assert allocation.target_weight == D("0.15000000")
    assert allocation.target_weight <= allocation.atr_allocation_ceiling
    assert allocation.atr_quantity_ceiling == D("7")
    assert result.evidence[-1].kind == ShadowAdjustmentKind.ATR_CAP


def test_existing_weight_above_atr_ceiling_is_hard_capped_hypothetically() -> None:
    result = allocate_shadow_targets(
        (_rank("A", "1"),),
        (_position("A", "0.4", "TECH"),),
        (_sector("TECH", "0.4"),),
        evaluated_at=NOW,
        atr_ceilings=(
            ShadowAtrCeiling(
                market="KR",
                symbol="A",
                status=ShadowSelectionStatus.VALID,
                maximum_allocation_weight=D("0.15"),
                maximum_quantity=None,
                source_timestamp=NOW,
            ),
        ),
        config=_config(
            top_k=1,
            maximum_rebalance_delta=D("0.1"),
        ),
    )

    assert result.allocations[0].target_weight == D("0.15000000")
    assert result.allocations[0].unconstrained_target_weight == D("0.50000000")
    assert result.allocations[0].sell_risk_reduction_allowed is True


def test_equal_scores_use_symbol_then_market_tie_order_independent_of_input() -> None:
    rankings = (
        _rank("B", "0.8", market="KR"),
        _rank("A", "0.8", market="US"),
        _rank("A", "0.8", market="KR"),
    )
    positions = (
        _position("B", "0", "B", market="KR"),
        _position("A", "0", "A-US", market="US"),
        _position("A", "0", "A-KR", market="KR"),
    )
    exposures = (
        _sector("B", "0"),
        _sector("A-US", "0"),
        _sector("A-KR", "0"),
    )

    result = allocate_shadow_targets(
        rankings,
        positions,
        exposures,
        evaluated_at=NOW,
        config=_config(top_k=3),
    )

    assert result.selected_keys == (("KR", "A"), ("US", "A"), ("KR", "B"))


def test_missing_sector_is_unknown_and_emits_coverage_evidence() -> None:
    result = allocate_shadow_targets(
        (_rank("A", "1"),),
        (_position("A", "0", None),),
        (),
        evaluated_at=NOW,
        config=_config(top_k=1),
    )

    assert result.allocations[0].sector_key == UNKNOWN_SECTOR
    assert result.sector_coverage.unknown_count == 1
    assert result.sector_coverage.unknown_keys == (("KR", "A"),)
    assert result.projected_sector_exposures == ((UNKNOWN_SECTOR, D("0.80000000")),)


def test_evidence_flag_changes_only_emission_not_hypothetical_targets() -> None:
    inputs = (
        (_rank("A", "1"),),
        (_position("A", "0", "TECH"),),
        (_sector("TECH", "0"),),
    )
    enabled = allocate_shadow_targets(
        *inputs,
        evaluated_at=NOW,
        config=_config(top_k=1, emit_evidence=True),
    )
    disabled = allocate_shadow_targets(
        *inputs,
        evaluated_at=NOW,
        config=_config(top_k=1, emit_evidence=False),
    )

    assert _targets(enabled) == _targets(disabled)
    assert enabled.projected_sector_exposures == disabled.projected_sector_exposures
    assert enabled.evidence
    assert disabled.evidence == ()
    assert enabled.config_fingerprint != disabled.config_fingerprint


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("top_k", 0),
        ("target_investment_weight", D("1.01")),
        ("maximum_rebalance_delta", D("0")),
        ("no_trade_band", D("-0.01")),
        ("sector_weight_cap", D("NaN")),
        ("target_investment_weight", 1.0),
    ],
)
def test_invalid_config_is_rejected(field: str, value: object) -> None:
    values: dict[str, object] = {
        "top_k": 2,
        "target_investment_weight": D("0.8"),
        "maximum_rebalance_delta": D("0.2"),
        "no_trade_band": D("0.01"),
        "sector_weight_cap": D("0.5"),
    }
    values[field] = value

    with pytest.raises(ValueError):
        ShadowSelectionConfig(**values)  # type: ignore[arg-type]
