"""Canonical strategy artifact identity for KAsset PAPER promotion."""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.extensions.kasset.automation.candidate_ranker import (
    DEFAULT_CANDIDATE_RANKER_CONFIG,
)
from app.extensions.kasset.automation.portfolio_backtest import (
    PortfolioBacktestConfig,
    WalkForwardConfig,
)
from app.extensions.kasset.automation.position_manager import PositionManagerConfig
from app.extensions.kasset.automation.position_sizing import (
    DEFAULT_POSITION_SIZING_CONFIG,
)
from app.extensions.kasset.automation.regime import MarketRegime, weights_for_regime
from app.extensions.kasset.automation.strategies import STRATEGIES
from app.extensions.kasset.automation.strategy_promotion import (
    DEFAULT_PAPER_STRATEGY_KEY,
    DEFAULT_PAPER_STRATEGY_VERSION,
)
from app.services.research_canonical_hash import canonical_sha256

STRATEGY_ARTIFACT_SCHEMA_VERSION = "kasset.strategy-artifact.v1"
PROMOTION_EVIDENCE_SCHEMA_VERSION = "kasset.paper-promotion-evidence.v1"
BACKTEST_CANDIDATES_PER_MARKET = 6
BACKTEST_HISTORY_BARS = 400

# Only strategy-impacting runtime code belongs here. Operational surfaces and
# mutable evidence plumbing (docs, tests, UI, migrations, CLI) are excluded.
STRATEGY_CODE_PATHS: tuple[str, ...] = (
    "app/extensions/kasset/automation/candidate_ranker.py",
    "app/extensions/kasset/automation/contracts.py",
    "app/extensions/kasset/automation/portfolio_backtest.py",
    "app/extensions/kasset/automation/position_manager.py",
    "app/extensions/kasset/automation/position_sizing.py",
    "app/extensions/kasset/automation/producer.py",
    "app/extensions/kasset/automation/regime.py",
    "app/extensions/kasset/automation/strategies.py",
    "app/extensions/kasset/automation/strategy_promotion.py",
)

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass(frozen=True, slots=True)
class StrategyCodeFile:
    path: str
    sha256: str

    def as_evidence(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class StrategyArtifactManifest:
    schema_version: str
    strategy_key: str
    strategy_version: str
    fingerprint: str
    source_commit: str
    code_files: tuple[StrategyCodeFile, ...]
    effective_config: Mapping[str, object]

    def __post_init__(self) -> None:
        if not _FINGERPRINT_RE.fullmatch(self.fingerprint):
            raise ValueError("strategy artifact fingerprint must be lowercase 64-hex")
        if not _SOURCE_COMMIT_RE.fullmatch(self.source_commit):
            raise ValueError("source commit must be lowercase 40-hex or 64-hex")

    def as_evidence(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "strategyKey": self.strategy_key,
            "strategyVersion": self.strategy_version,
            "fingerprint": self.fingerprint,
            "sourceCommit": self.source_commit,
            "codeFiles": [item.as_evidence() for item in self.code_files],
            "effectiveConfig": dict(self.effective_config),
        }


def effective_strategy_config() -> dict[str, object]:
    """Return the concrete defaults used by the deterministic portfolio path."""

    portfolio = PortfolioBacktestConfig()
    walk_forward = WalkForwardConfig()
    regime_weights = {
        regime.value: {
            strategy.value: weight
            for strategy, weight in sorted(
                weights_for_regime(regime).items(), key=lambda item: item[0].value
            )
        }
        for regime in MarketRegime
    }
    strategy_registry = tuple(
        {
            "name": strategy.name.value,
            "version": strategy.version,
            "class": type(strategy).__name__,
            "minimumBars": strategy.minimum_bars,
            "validitySeconds": int(strategy.validity.total_seconds()),
        }
        for strategy in STRATEGIES
    )
    return _canonical_config_value(
        {
            "candidateRanker": DEFAULT_CANDIDATE_RANKER_CONFIG,
            "portfolioBacktest": portfolio,
            "evidenceUniverseSelection": {
                "candidatesPerMarket": BACKTEST_CANDIDATES_PER_MARKET,
                "historyBarsPerSymbol": BACKTEST_HISTORY_BARS,
                "selection": "active_eligible_then_one_delisted",
            },
            "walkForward": walk_forward,
            "positionSizer": DEFAULT_POSITION_SIZING_CONFIG,
            "positionManager": PositionManagerConfig(),
            "strategyRegistry": strategy_registry,
            "regimeWeights": regime_weights,
            "promotionEvidenceSchemaVersion": PROMOTION_EVIDENCE_SCHEMA_VERSION,
        }
    )


def fingerprint_strategy_artifact(
    *,
    code_files: Sequence[StrategyCodeFile],
    effective_config: Mapping[str, object],
    schema_version: str = STRATEGY_ARTIFACT_SCHEMA_VERSION,
) -> str:
    """Hash canonical strategy code/config while deliberately excluding Git SHA."""

    normalized_files = tuple(sorted(code_files, key=lambda item: item.path))
    if not normalized_files or len({item.path for item in normalized_files}) != len(
        normalized_files
    ):
        raise ValueError("strategy code files must be a non-empty unique set")
    for item in normalized_files:
        if not item.path or not _FINGERPRINT_RE.fullmatch(item.sha256):
            raise ValueError("strategy code file hashes must be lowercase 64-hex")
    payload = {
        "schemaVersion": schema_version,
        "codeFiles": tuple(item.as_evidence() for item in normalized_files),
        "effectiveConfig": _canonical_config_value(effective_config),
    }
    return canonical_sha256(payload)


def load_current_strategy_artifact(
    *,
    repo_root: Path | None = None,
) -> StrategyArtifactManifest:
    """Read the checked-out strategy code and stamp its actual Git commit."""

    root = (repo_root or Path(__file__).resolve().parents[4]).resolve()
    code_files = tuple(_code_file(root, relative) for relative in STRATEGY_CODE_PATHS)
    config = effective_strategy_config()
    return StrategyArtifactManifest(
        schema_version=STRATEGY_ARTIFACT_SCHEMA_VERSION,
        strategy_key=DEFAULT_PAPER_STRATEGY_KEY,
        strategy_version=DEFAULT_PAPER_STRATEGY_VERSION,
        fingerprint=fingerprint_strategy_artifact(
            code_files=code_files,
            effective_config=config,
        ),
        source_commit=_source_commit(root),
        code_files=code_files,
        effective_config=config,
    )


@lru_cache(maxsize=1)
def current_strategy_artifact() -> StrategyArtifactManifest:
    """Process-local runtime identity; deployed source is immutable per process."""

    return load_current_strategy_artifact()


def _code_file(root: Path, relative: str) -> StrategyCodeFile:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("strategy code path escapes repository root") from exc
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"strategy code is not UTF-8: {relative}") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return StrategyCodeFile(
        path=relative, sha256=hashlib.sha256(normalized).hexdigest()
    )


def _source_commit(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("current source commit is unavailable") from exc
    commit = completed.stdout.strip().lower()
    if not _SOURCE_COMMIT_RE.fullmatch(commit):
        raise ValueError("current source commit is not a full Git object id")
    return commit


def _canonical_config_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_config_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, timedelta):
        return int(value.total_seconds() * 1_000_000)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, Mapping):
        return {
            str(_canonical_config_value(key)): _canonical_config_value(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list)):
        return tuple(_canonical_config_value(child) for child in value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"unsupported effective config value: {type(value).__name__}")


__all__ = [
    "BACKTEST_CANDIDATES_PER_MARKET",
    "BACKTEST_HISTORY_BARS",
    "PROMOTION_EVIDENCE_SCHEMA_VERSION",
    "STRATEGY_ARTIFACT_SCHEMA_VERSION",
    "STRATEGY_CODE_PATHS",
    "StrategyArtifactManifest",
    "StrategyCodeFile",
    "current_strategy_artifact",
    "effective_strategy_config",
    "fingerprint_strategy_artifact",
    "load_current_strategy_artifact",
]
