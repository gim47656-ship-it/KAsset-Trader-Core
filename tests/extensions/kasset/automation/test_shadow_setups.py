from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation.shadow_setups import (
    DEFAULT_SHADOW_SETUP_CONFIG,
    SHADOW_SETUPS_SCHEMA_VERSION,
    ShadowStatus,
    evaluate_ranked_shadow_setups,
    evaluate_shadow_setups,
    shadow_setups_evidence,
)

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(
    index: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "1000",
) -> PriceBar:
    return PriceBar(
        timestamp=_START + timedelta(days=index),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def _rising(count: int) -> list[PriceBar]:
    result: list[PriceBar] = []
    for index in range(count):
        close = Decimal("100") + index
        result.append(
            PriceBar(
                timestamp=_START + timedelta(days=index),
                open=close - Decimal("0.05"),
                high=close + Decimal("0.10"),
                low=close - Decimal("0.10"),
                close=close,
                volume=Decimal("1000"),
            )
        )
    return result


def _single_pullback(
    *, recovery_close: str = "127", recovery_volume: str = "1200"
) -> list[PriceBar]:
    bars = _rising(28)
    bars.extend(
        [
            _bar(28, open_="126", high="126.2", low="121", close="124"),
            _bar(29, open_="124", high="125.5", low="121.5", close="125"),
            _bar(
                30,
                open_="126",
                high=str(max(Decimal(recovery_close), Decimal("127.2"))),
                low=(
                    str(Decimal(recovery_close) - Decimal("0.5"))
                    if Decimal(recovery_close) < Decimal("126")
                    else "126"
                ),
                close=recovery_close,
                volume=recovery_volume,
            ),
        ]
    )
    return bars


def _two_pullbacks(*, untouched_between: int = 2) -> list[PriceBar]:
    bars = _rising(23)
    next_index = 23
    bars.append(_bar(next_index, open_="120", high="120.5", low="116", close="118"))
    next_index += 1
    for offset in range(untouched_between):
        close = Decimal("122") + offset
        bars.append(
            PriceBar(
                timestamp=_START + timedelta(days=next_index),
                open=close,
                high=close + Decimal("0.2"),
                low=close - Decimal("0.2"),
                close=close,
                volume=Decimal("1000"),
            )
        )
        next_index += 1
    bars.append(_bar(next_index, open_="122", high="123", low="117", close="120"))
    next_index += 1
    bars.append(
        _bar(
            next_index,
            open_="123.5",
            high="124.5",
            low="123.2",
            close="124",
            volume="1200",
        )
    )
    return bars


def _noisy_pullback() -> list[PriceBar]:
    bars = _rising(30)
    path = (
        ("128.8", "129.2", "128.5", "129"),
        ("129.2", "129.6", "129", "129.4"),
        ("127.8", "128.8", "127", "128"),
        ("128.5", "129", "128", "128.7"),
        ("124", "127", "122", "125"),
        ("127", "128", "126.5", "127.5"),
    )
    for offset, (open_, high, low, close) in enumerate(path, start=30):
        bars.append(
            _bar(
                offset,
                open_=open_,
                high=high,
                low=low,
                close=close,
                volume="1200" if offset == 35 else "1000",
            )
        )
    return bars


def _compression_bars(kind: str, *, current_volume: str = "500") -> list[PriceBar]:
    bars = [
        _bar(
            index,
            open_="100",
            high="102",
            low="98",
            close="100",
            volume="100",
        )
        for index in range(21)
    ]
    if kind == "nr7":
        bars[-2] = _bar(
            19, open_="105", high="110", low="100", close="105", volume="100"
        )
        bars[-1] = _bar(
            20,
            open_="111.2",
            high="112",
            low="111",
            close="111.5",
            volume=current_volume,
        )
    elif kind == "inside_day":
        bars[-6] = _bar(
            15, open_="101", high="102", low="100", close="101", volume="100"
        )
        bars[-2] = _bar(
            19, open_="105", high="110", low="100", close="105", volume="100"
        )
        bars[-1] = _bar(
            20, open_="106", high="108", low="105", close="107", volume=current_volume
        )
    elif kind == "combined":
        bars[-2] = _bar(
            19, open_="105", high="110", low="100", close="105", volume="100"
        )
        bars[-1] = _bar(
            20,
            open_="105.2",
            high="106",
            low="105",
            close="105.5",
            volume=current_volume,
        )
    else:
        raise AssertionError(f"unknown fixture kind: {kind}")
    return bars


def _evaluate(bars: list[PriceBar], **kwargs: object):
    as_of = kwargs.pop("as_of", bars[-1].timestamp + timedelta(hours=1))
    return evaluate_shadow_setups(
        bars,
        symbol="aapl",
        market="US",
        as_of=as_of,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _evidence(result: object, code: str):
    return next(item for item in result.evidence if item.code == code)  # type: ignore[attr-defined]


def test_contracts_are_frozen_versioned_shadow_only_and_config_has_separate_fingerprint() -> (
    None
):
    bars = _single_pullback()
    result = _evaluate(bars)

    assert result.schema_version == SHADOW_SETUPS_SCHEMA_VERSION
    assert result.mode == "SHADOW"
    assert result.status == ShadowStatus.VALID
    assert result.config_fingerprint == DEFAULT_SHADOW_SETUP_CONFIG.fingerprint
    assert len(result.config_fingerprint) == 64
    assert DEFAULT_SHADOW_SETUP_CONFIG.fingerprint_payload()["schemaVersion"].endswith(
        "config.v1"
    )
    with pytest.raises(FrozenInstanceError):
        result.mode = "PAPER"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        DEFAULT_SHADOW_SETUP_CONFIG.feature_enabled = False  # type: ignore[misc]
    for child in (result.first_pullback, result.nr7_inside_day, *result.observations):
        assert child.schema_version == SHADOW_SETUPS_SCHEMA_VERSION
        assert child.mode == "SHADOW"
        assert child.status in set(ShadowStatus)
        assert child.source_timestamps
        assert all(item.mode == "SHADOW" for item in child.evidence)


def test_clustered_contacts_collapse_through_configured_untouched_bar_boundary() -> (
    None
):
    collapsed = _evaluate(_two_pullbacks(untouched_between=1))
    distinct = _evaluate(_two_pullbacks(untouched_between=2))

    assert collapsed.first_pullback.distinct_contact_count == 1
    assert collapsed.first_pullback.contact_label == "first"
    assert distinct.first_pullback.distinct_contact_count == 2
    assert distinct.first_pullback.contact_label == "second"


def test_first_and_second_distinct_contact_labels_are_explicit() -> None:
    first = _evaluate(_single_pullback()).first_pullback
    second = _evaluate(_two_pullbacks()).first_pullback

    assert (first.contact_label, first.distinct_contact_count) == ("first", 1)
    assert (second.contact_label, second.distinct_contact_count) == ("second", 2)
    assert _evidence(first, "contact_label").value == "first"
    assert _evidence(second, "contact_label").value == "second"


def test_later_label_and_pullback_depth_are_deterministic_components() -> None:
    bars = _single_pullback()
    bars[18] = replace(bars[18], low=Decimal("110"))
    bars[23] = replace(bars[23], low=Decimal("115"))
    later = _evaluate(bars).first_pullback

    deep_bars = _single_pullback()
    deep_bars[28] = replace(deep_bars[28], low=Decimal("90"))
    deep = _evaluate(deep_bars).first_pullback

    assert (later.contact_label, later.distinct_contact_count) == ("later", 3)
    assert later.pullback_depth is not None
    assert _evidence(later, "pullback_depth").value == str(later.pullback_depth)
    assert _evidence(later, "pullback_depth").passed is True
    assert deep.pullback_depth is not None
    assert deep.pullback_depth > DEFAULT_SHADOW_SETUP_CONFIG.maximum_pullback_depth
    assert deep.confirmed is False
    assert _evidence(deep, "pullback_depth").passed is False


def test_orderly_pullback_confirms_but_noisy_path_does_not() -> None:
    strict = replace(DEFAULT_SHADOW_SETUP_CONFIG, minimum_orderliness=Decimal("0.80"))
    orderly = _evaluate(_single_pullback(), config=strict).first_pullback
    noisy = _evaluate(_noisy_pullback(), config=strict).first_pullback

    assert orderly.orderliness == Decimal("1.000000")
    assert orderly.confirmed is True
    assert noisy.orderliness is not None and noisy.orderliness < Decimal("0.80")
    assert noisy.confirmed is False
    assert _evidence(noisy, "orderliness").passed is False


def test_first_pullback_requires_ema10_recovery_and_trigger_clearance() -> None:
    recovered = _evaluate(_single_pullback()).first_pullback
    not_recovered = _evaluate(
        _single_pullback(recovery_close="123", recovery_volume="1200")
    ).first_pullback

    assert recovered.ema_recovered is True
    assert recovered.confirmed is True
    assert not_recovered.ema_recovered is False
    assert not_recovered.confirmed is False
    assert _evidence(not_recovered, "ema10_recovery").passed is False


def test_first_pullback_supportive_volume_uses_prior_window() -> None:
    supported = _evaluate(_single_pullback(recovery_volume="1200")).first_pullback
    weak = _evaluate(_single_pullback(recovery_volume="500")).first_pullback

    assert supported.supportive_volume is True
    assert supported.confirmed is True
    assert weak.supportive_volume is False
    assert weak.confirmed is False
    assert _evidence(weak, "prior_volume_average").value == "1000.000000"
    assert _evidence(weak, "supportive_volume_ratio").value == "0.500000"


def test_appending_future_bars_cannot_change_historical_setup_output() -> None:
    bars = _single_pullback()
    as_of = bars[-1].timestamp + timedelta(hours=1)
    historical = _evaluate(bars, as_of=as_of)
    future = replace(
        bars[-1],
        timestamp=as_of + timedelta(days=3),
        close=Decimal("999"),
        high=Decimal("1000"),
        low=Decimal("998"),
    )

    assert _evaluate(bars + [future], as_of=as_of) == historical


@pytest.mark.parametrize(
    ("kind", "subtype", "nr7", "inside_day"),
    [
        ("nr7", "nr7", True, False),
        ("inside_day", "inside_day", False, True),
        ("combined", "nr7_inside_day", True, True),
    ],
)
def test_nr7_inside_day_individual_and_combined_subtypes_are_explicit(
    kind: str, subtype: str, nr7: bool, inside_day: bool
) -> None:
    result = _evaluate(_compression_bars(kind)).nr7_inside_day

    assert result.subtype == subtype
    assert result.nr7 is nr7
    assert result.inside_day is inside_day
    assert result.pivot is not None
    assert result.pivot_at == result.source_timestamps[-1]
    assert result.valid_until == result.pivot_at + timedelta(days=1)


def test_inside_day_containment_is_strict_at_both_boundaries() -> None:
    bars = _compression_bars("combined")
    equal_high = replace(bars[-1], high=bars[-2].high)
    equal_low = replace(bars[-1], low=bars[-2].low)

    assert _evaluate(bars[:-1] + [equal_high]).nr7_inside_day.inside_day is False
    assert _evaluate(bars[:-1] + [equal_low]).nr7_inside_day.inside_day is False


def test_nr7_accepts_a_tie_for_the_minimum_range() -> None:
    bars = _compression_bars("combined")
    bars[-3] = _bar(
        18, open_="100.2", high="101", low="100", close="100.5", volume="100"
    )

    result = _evaluate(bars).nr7_inside_day

    assert result.nr7 is True
    assert result.inside_day is True
    assert result.subtype == "nr7_inside_day"
    assert "ties accepted" in (_evidence(result, "nr7_range").threshold or "")


def test_current_volume_is_excluded_from_contraction_baseline() -> None:
    contracted = _evaluate(
        _compression_bars("combined", current_volume="50")
    ).nr7_inside_day
    expanded = _evaluate(
        _compression_bars("combined", current_volume="10000")
    ).nr7_inside_day

    assert _evidence(contracted, "prior_volume_average").value == "100.000000"
    assert _evidence(expanded, "prior_volume_average").value == "100.000000"
    assert contracted.volume_contracted is True
    assert expanded.volume_contracted is False


def test_incomplete_bar_is_excluded_and_matches_the_same_completed_prefix() -> None:
    bars = _compression_bars("combined")
    cutoff = bars[-2].timestamp
    as_of = bars[-1].timestamp + timedelta(hours=1)

    config = replace(DEFAULT_SHADOW_SETUP_CONFIG, validity=timedelta(days=2))
    with_incomplete = _evaluate(
        bars,
        as_of=as_of,
        completed_through=cutoff,
        config=config,
    )
    prefix_only = _evaluate(bars[:-1], as_of=as_of, config=config)

    assert with_incomplete == prefix_only
    assert bars[-1].timestamp not in with_incomplete.source_timestamps


def test_invalid_completed_data_fails_closed_but_invalid_future_data_is_ignored() -> (
    None
):
    bars = _compression_bars("combined")
    as_of = bars[-1].timestamp + timedelta(hours=1)
    invalid_completed = replace(bars[-1], low=bars[-1].high + Decimal("1"))
    invalid_future = replace(
        invalid_completed,
        timestamp=as_of + timedelta(days=1),
    )

    failed = _evaluate(bars[:-1] + [invalid_completed], as_of=as_of)
    unchanged = _evaluate(bars + [invalid_future], as_of=as_of)

    assert failed.status == ShadowStatus.FAIL_CLOSED
    assert failed.observations == ()
    assert failed.evidence[0].code == "invalid_ohlcv"
    assert unchanged == _evaluate(bars, as_of=as_of)


def test_trigger_math_uses_deterministic_resumption_and_setup_high_pivots() -> None:
    pullback = _evaluate(_single_pullback()).first_pullback
    compression = _evaluate(_compression_bars("combined")).nr7_inside_day

    assert pullback.resumption_pivot == Decimal("126.200000")
    assert pullback.trigger_price == pullback.resumption_pivot
    assert pullback.pullback_pivot == Decimal("121.000000")
    assert _evidence(pullback, "trigger_price").threshold == "close > trigger_price"
    assert compression.pivot == Decimal("106.000000")
    assert compression.trigger_price == compression.pivot


def test_stale_data_and_future_completion_cutoff_are_rejected() -> None:
    bars = _compression_bars("combined")
    stale = _evaluate(bars, as_of=bars[-1].timestamp + timedelta(days=1))
    future_cutoff = _evaluate(
        bars,
        as_of=bars[-1].timestamp + timedelta(hours=1),
        completed_through=bars[-1].timestamp + timedelta(days=1),
    )

    assert stale.status == ShadowStatus.FAIL_CLOSED
    assert stale.evidence[0].code == "stale_completed_bar"
    assert stale.observations == ()
    assert future_cutoff.status == ShadowStatus.FAIL_CLOSED
    assert future_cutoff.evidence[0].code == "future_completion_cutoff"


def test_feature_flag_changes_only_observation_emission() -> None:
    bars = _single_pullback()
    enabled = _evaluate(
        bars, config=replace(DEFAULT_SHADOW_SETUP_CONFIG, feature_enabled=True)
    )
    disabled = _evaluate(
        bars, config=replace(DEFAULT_SHADOW_SETUP_CONFIG, feature_enabled=False)
    )

    assert enabled.observations
    assert disabled.observations == ()
    assert disabled.first_pullback == enabled.first_pullback
    assert disabled.nr7_inside_day == enabled.nr7_inside_day
    assert disabled.status == enabled.status == ShadowStatus.VALID


def test_short_history_is_non_actionable_with_explicit_insufficient_status() -> None:
    result = _evaluate(_rising(7))

    assert result.status == ShadowStatus.INSUFFICIENT
    assert result.first_pullback.status == ShadowStatus.INSUFFICIENT
    assert result.nr7_inside_day.status == ShadowStatus.INSUFFICIENT
    assert result.observations == ()
    assert result.evidence[0].code == "history_bars"


def test_ranked_runtime_and_backtest_adapter_is_inactive_by_default() -> None:
    bars = tuple(_single_pullback())

    assert DEFAULT_SHADOW_SETUP_CONFIG.feature_enabled is False
    assert (
        evaluate_ranked_shadow_setups(
            (("KR", "005930"),),
            {("KR", "005930"): bars},
            as_of=bars[-1].timestamp + timedelta(hours=1),
        )
        == ()
    )


def test_ranked_runtime_and_backtest_adapter_uses_shared_detector() -> None:
    bars = tuple(_single_pullback())
    as_of = bars[-1].timestamp + timedelta(hours=1)
    config = replace(DEFAULT_SHADOW_SETUP_CONFIG, feature_enabled=True)

    observed = evaluate_ranked_shadow_setups(
        (("KR", "005930"), ("KR", "005930")),
        {("KR", "005930"): bars},
        as_of=as_of,
        config=config,
    )
    direct = evaluate_shadow_setups(
        bars,
        symbol="005930",
        market="KRX",
        as_of=as_of,
        config=config,
    )

    assert observed == (direct,)
    assert json.loads(json.dumps(shadow_setups_evidence(observed[0]))) == (
        shadow_setups_evidence(observed[0])
    )
