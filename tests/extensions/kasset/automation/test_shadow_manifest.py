from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from app.extensions.kasset.automation.shadow_high_watermark import (
    ShadowHighWatermarkThresholds,
    ShadowReductionStage,
)
from app.extensions.kasset.automation.shadow_loss_streak import ShadowLossStreakConfig
from app.extensions.kasset.automation.shadow_manifest import (
    SHADOW_AUTOMATION_EVIDENCE_SCHEMA_VERSION,
    ShadowActivation,
    ShadowAutomationManifest,
)
from app.extensions.kasset.automation.shadow_selection import (
    DEFAULT_SHADOW_SELECTION_CONFIG,
)
from app.extensions.kasset.automation.shadow_setups import (
    DEFAULT_SHADOW_SETUP_CONFIG,
)
from app.extensions.kasset.automation.strategy_artifact import (
    load_current_strategy_artifact,
)


def _manifest(*, activation: ShadowActivation = ShadowActivation()):
    return ShadowAutomationManifest(
        setups=DEFAULT_SHADOW_SETUP_CONFIG,
        high_watermark=ShadowHighWatermarkThresholds(
            profit_target_stages=(
                ShadowReductionStage("profit-1", Decimal("0.05"), Decimal("0.75")),
            ),
            peak_drawdown_stages=(
                ShadowReductionStage("drawdown-1", Decimal("0.03"), Decimal("0.80")),
            ),
            maximum_loss_ratio=Decimal("0.12"),
            max_valuation_age=timedelta(minutes=5),
        ),
        loss_streak=ShadowLossStreakConfig(
            stop_loss_reasons=("STOP_LOSS", "TRAILING_STOP"),
            loss_limit=3,
            lookback=timedelta(days=5),
            lock_duration=timedelta(hours=4),
        ),
        soft_top_k=DEFAULT_SHADOW_SELECTION_CONFIG,
        activation=activation,
    )


def test_manifest_fingerprints_every_shadow_component_and_sector_cap() -> None:
    manifest = _manifest()
    evidence = manifest.as_evidence()
    components = evidence["components"]

    assert evidence["evidenceSchemaVersion"] == (
        SHADOW_AUTOMATION_EVIDENCE_SCHEMA_VERSION
    )
    assert len(evidence["configFingerprint"]) == 64
    assert components["setups"]["fingerprint"] == manifest.setups.fingerprint
    assert components["highWatermark"]["fingerprint"] == (
        manifest.high_watermark.fingerprint
    )
    assert components["lossStreak"]["fingerprint"] == (manifest.loss_streak.fingerprint)
    assert components["softTopK"]["fingerprint"] == manifest.soft_top_k.fingerprint
    assert components["softTopK"]["config"]["sectorWeightCap"] == "0.3"
    assert evidence["promotionEligible"] is False


def test_activation_is_separate_and_every_shadow_skill_defaults_inactive() -> None:
    inactive = _manifest()
    active = _manifest(activation=ShadowActivation(setups=True))

    assert inactive.activation.any_active is False
    assert active.activation.any_active is True
    assert inactive.config_fingerprint == active.config_fingerprint
    assert inactive.activation.fingerprint != active.activation.fingerprint


def test_shadow_config_changes_never_change_active_strategy_fingerprint() -> None:
    before = load_current_strategy_artifact().fingerprint
    original = _manifest()
    changed = replace(
        original,
        setups=replace(original.setups, maximum_pullback_depth=Decimal("0.14")),
    )
    after = load_current_strategy_artifact().fingerprint

    assert original.config_fingerprint != changed.config_fingerprint
    assert before == after
