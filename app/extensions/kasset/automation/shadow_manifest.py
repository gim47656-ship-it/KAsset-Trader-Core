"""Versioned fingerprints for inactive automation SHADOW observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.extensions.kasset.automation.shadow_high_watermark import (
    ShadowHighWatermarkThresholds,
)
from app.extensions.kasset.automation.shadow_loss_streak import ShadowLossStreakConfig
from app.extensions.kasset.automation.shadow_selection import ShadowSelectionConfig
from app.extensions.kasset.automation.shadow_setups import ShadowSetupConfig

SHADOW_AUTOMATION_CONFIG_SCHEMA_VERSION = "kasset.shadow-automation-config.v1"
SHADOW_AUTOMATION_EVIDENCE_SCHEMA_VERSION = "kasset.shadow-automation-evidence.v1"
SHADOW_ACTIVATION_SCHEMA_VERSION = "kasset.shadow-automation-activation.v1"
SHADOW_MODE = "SHADOW"


@dataclass(frozen=True, slots=True)
class ShadowActivation:
    """Deployment switches kept separate from calculation fingerprints."""

    setups: bool = False
    high_watermark: bool = False
    loss_streak: bool = False
    soft_top_k: bool = False

    def as_serializable(self) -> dict[str, object]:
        return {
            "schemaVersion": SHADOW_ACTIVATION_SCHEMA_VERSION,
            "setups": self.setups,
            "highWatermark": self.high_watermark,
            "lossStreak": self.loss_streak,
            "softTopK": self.soft_top_k,
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.as_serializable())

    @property
    def any_active(self) -> bool:
        return any(
            (
                self.setups,
                self.high_watermark,
                self.loss_streak,
                self.soft_top_k,
            )
        )


@dataclass(frozen=True, slots=True)
class ShadowAutomationManifest:
    """Canonical SHADOW identity that never participates in promotion."""

    setups: ShadowSetupConfig
    high_watermark: ShadowHighWatermarkThresholds
    loss_streak: ShadowLossStreakConfig
    soft_top_k: ShadowSelectionConfig
    activation: ShadowActivation = ShadowActivation()

    def config_payload(self) -> dict[str, object]:
        return {
            "schemaVersion": SHADOW_AUTOMATION_CONFIG_SCHEMA_VERSION,
            "evidenceSchemaVersion": SHADOW_AUTOMATION_EVIDENCE_SCHEMA_VERSION,
            "mode": SHADOW_MODE,
            "components": {
                "setups": {
                    "fingerprint": self.setups.fingerprint,
                    "config": self.setups.fingerprint_payload(),
                },
                "highWatermark": {
                    "fingerprint": self.high_watermark.fingerprint,
                    "config": self.high_watermark.as_serializable(),
                },
                "lossStreak": {
                    "fingerprint": self.loss_streak.fingerprint,
                    "config": self.loss_streak.as_serializable(),
                },
                "softTopK": {
                    "fingerprint": self.soft_top_k.fingerprint,
                    "config": self.soft_top_k.fingerprint_payload(),
                },
            },
        }

    @property
    def config_fingerprint(self) -> str:
        return _fingerprint(self.config_payload())

    def as_evidence(self) -> dict[str, object]:
        return {
            **self.config_payload(),
            "configFingerprint": self.config_fingerprint,
            "activation": {
                "fingerprint": self.activation.fingerprint,
                "config": self.activation.as_serializable(),
                "anyActive": self.activation.any_active,
            },
            "promotionEligible": False,
        }


def _fingerprint(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "SHADOW_ACTIVATION_SCHEMA_VERSION",
    "SHADOW_AUTOMATION_CONFIG_SCHEMA_VERSION",
    "SHADOW_AUTOMATION_EVIDENCE_SCHEMA_VERSION",
    "ShadowActivation",
    "ShadowAutomationManifest",
]
