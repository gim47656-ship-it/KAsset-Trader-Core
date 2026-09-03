from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions.kasset.automation import promotion_evidence, trade_bootstrap
from app.extensions.kasset.automation.promotion_evidence import (
    PromotionEvidenceBuildError,
    _trade_bootstrap_payload,
)
from app.extensions.kasset.automation.trade_bootstrap import calculate_trade_bootstrap


def _payload_bytes(value: object) -> bytes:
    assert value is not None
    return json.dumps(
        value.as_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def test_block_bootstrap_is_byte_deterministic_for_fixed_seed() -> None:
    pnls = tuple(
        Decimal(value)
        for value in (
            "5",
            "4",
            "3",
            "-8",
            "-7",
            "-6",
        )
        * 6
    )

    first = calculate_trade_bootstrap(
        pnls,
        Decimal("1000"),
        simulations=200,
        seed=20260903,
    )
    second = calculate_trade_bootstrap(
        pnls,
        Decimal("1000"),
        simulations=200,
        seed=20260903,
    )

    assert first is not None
    assert first.sampling == "block"
    assert first.block_size == 6
    assert _payload_bytes(first) == _payload_bytes(second)


def test_block_bootstrap_wraps_consecutive_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts = iter((28, 3, 8, 13, 18, 23))

    class _FixedRandom:
        def __init__(self, _seed: int) -> None:
            pass

        def randrange(self, stop: int) -> int:
            assert stop == 30
            return next(starts)

    monkeypatch.setattr(trade_bootstrap, "Random", _FixedRandom)
    pnls = tuple(Decimal(index - 15) for index in range(30))

    result = calculate_trade_bootstrap(
        pnls,
        Decimal("100"),
        simulations=1,
    )

    assert result is not None
    expected_indices = (28, 29, 0, 1, 2, *range(3, 28))
    expected_pnl = sum((pnls[index] for index in expected_indices), Decimal("0"))
    assert result.pnl_p50 == expected_pnl


def test_minimum_trade_count_boundary_reuses_promotion_threshold() -> None:
    assert (
        calculate_trade_bootstrap(
            (Decimal("1"),) * 29,
            Decimal("100"),
            simulations=10,
        )
        is None
    )

    result = calculate_trade_bootstrap(
        (Decimal("1"),) * 30,
        Decimal("100"),
        simulations=10,
    )

    assert result is not None
    assert result.sample_count == 30
    assert result.block_size == 5


def test_bootstrap_distribution_matches_profit_and_loss_sanity_checks() -> None:
    profits = calculate_trade_bootstrap(
        (Decimal("1"),) * 30,
        Decimal("100"),
        simulations=50,
    )
    losses = calculate_trade_bootstrap(
        (Decimal("-1"),) * 30,
        Decimal("100"),
        simulations=50,
    )

    assert profits is not None
    assert profits.max_drawdown_p95 == Decimal("0")
    assert losses is not None
    assert losses.pnl_p5 < Decimal("0")


def test_nonpositive_running_max_segments_count_as_zero_drawdown() -> None:
    result = calculate_trade_bootstrap(
        (Decimal("-1"),) * 30,
        Decimal("-10"),
        simulations=10,
    )

    assert result is not None
    assert result.max_drawdown_p95 == Decimal("0")


def test_promotion_evidence_serializes_baseline_trade_bootstrap_as_advisory() -> None:
    baseline = SimpleNamespace(
        initial_cash=Decimal("1000"),
        trades=tuple(SimpleNamespace(net_pnl=Decimal("1")) for _ in range(30)),
    )

    payload = _trade_bootstrap_payload(baseline)

    assert payload is not None
    assert payload["sampleCount"] == 30
    assert payload["sampling"] == "block"
    assert payload["pnlP5"] == "30"
    assert payload["maxDrawdownP95"] == "0"


def test_promotion_evidence_wraps_bootstrap_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = SimpleNamespace(
        initial_cash=Decimal("1000"),
        trades=tuple(SimpleNamespace(net_pnl=Decimal("1")) for _ in range(30)),
    )

    def reject_bootstrap(*_args, **_kwargs):
        raise ValueError("invalid bootstrap input")

    monkeypatch.setattr(
        promotion_evidence,
        "calculate_trade_bootstrap",
        reject_bootstrap,
    )

    with pytest.raises(
        PromotionEvidenceBuildError,
        match="backtest_evidence_unavailable:invalid bootstrap input",
    ):
        _trade_bootstrap_payload(baseline)
