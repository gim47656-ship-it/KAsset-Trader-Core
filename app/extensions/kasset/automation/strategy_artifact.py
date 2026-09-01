"""Canonical strategy artifact identity for KAsset PAPER promotion."""

from __future__ import annotations

import hashlib
import os
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
from app.extensions.kasset.automation.daily_setup import DEFAULT_DAILY_SETUP_CONFIG
from app.extensions.kasset.automation.intraday_data import (
    INTRADAY_BAR_COUNT,
    INTRADAY_BAR_INTERVAL,
    INTRADAY_BAR_PERIOD,
    INTRADAY_MAX_BAR_AGE,
)
from app.extensions.kasset.automation.intraday_triggers import (
    DEFAULT_INTRADAY_TRIGGER_POLICY,
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
PROMOTION_EVIDENCE_SCHEMA_VERSION = "kasset.paper-promotion-evidence.v4"
BACKTEST_CANDIDATES_PER_MARKET = 6
BACKTEST_HISTORY_BARS = 400

# Only strategy-impacting runtime code belongs here. Operational surfaces and
# mutable evidence plumbing (docs, tests, UI, migrations, CLI) are excluded.
STRATEGY_CODE_PATHS: tuple[str, ...] = (
    "app/extensions/kasset/automation/benchmark_relative_strength.py",
    "app/extensions/kasset/automation/candidate_ranker.py",
    "app/extensions/kasset/automation/contracts.py",
    "app/extensions/kasset/automation/daily_setup.py",
    "app/extensions/kasset/automation/intraday_data.py",
    "app/extensions/kasset/automation/intraday_triggers.py",
    "app/extensions/kasset/automation/market_session.py",
    "app/extensions/kasset/automation/portfolio_backtest.py",
    "app/extensions/kasset/automation/position_manager.py",
    "app/extensions/kasset/automation/position_sizing.py",
    "app/extensions/kasset/automation/producer.py",
    "app/extensions/kasset/automation/regime.py",
    "app/extensions/kasset/automation/strategies.py",
    "app/extensions/kasset/automation/strategy_promotion.py",
    "app/extensions/kasset/automation/vertical_slice.py",
)

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BUILD_VCS_REF_PATH = Path("/app/.build-vcs-ref")

#: 배포가 소스 커밋을 주입하는 환경변수. 앞쪽이 우선한다.
#:
#: ``KASSET_SOURCE_COMMIT``은 이미지를 다시 빌드하지 않고도 운영 배포가 즉시
#: 채울 수 있는 경로다. ``GITHUB_SHA``는 CI 빌드가 넣어 주는 기존 키다.
_SOURCE_COMMIT_ENV_VARS: tuple[str, ...] = ("KASSET_SOURCE_COMMIT", "GITHUB_SHA")

#: fail-closed 오류가 그대로 달고 나가는 조치 안내. 로그를 보는 사람이 "무엇을
#: 채워야 하는지"를 스택만 보고 알 수 있어야 한다.
_SOURCE_COMMIT_REMEDY = (
    "deployed image must embed its source commit: rebuild with "
    "--build-arg VCS_REF=<full git sha> or set KASSET_SOURCE_COMMIT"
)


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
            "dailySetup": DEFAULT_DAILY_SETUP_CONFIG,
            "intradayData": {
                "period": INTRADAY_BAR_PERIOD,
                "barInterval": INTRADAY_BAR_INTERVAL,
                "maximumBarAge": INTRADAY_MAX_BAR_AGE,
                "barCount": INTRADAY_BAR_COUNT,
            },
            "intradayTriggerPolicy": DEFAULT_INTRADAY_TRIGGER_POLICY,
            "portfolioBacktest": portfolio,
            "evidenceUniverseSelection": {
                "candidatesPerMarket": BACKTEST_CANDIDATES_PER_MARKET,
                "historyBarsPerSymbol": BACKTEST_HISTORY_BARS,
                "selection": "cohort_member_rank",
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
    """Read strategy code and stamp its deployed source commit lineage."""

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
    """배포된 이미지에 박힌 불변 소스 커밋. 없으면 fail closed다.

    해석 순서는 (1) ``KASSET_SOURCE_COMMIT``/``GITHUB_SHA`` 환경변수, (2) 빌드가
    써 넣은 ``/app/.build-vcs-ref``, (3) **로컬 개발 체크아웃에서만**
    ``git rev-parse``다.

    런타임 이미지에는 git이 없고 있어야 할 이유도 없다. 그래서 ``.git``이
    보이지 않으면 subprocess를 시도조차 하지 않고, ``FileNotFoundError``를
    "커밋을 알 수 없다"로 뭉개는 대신 무엇을 채워야 하는지 말하는 오류로 끝낸다.
    trust fingerprint 의미는 그대로다 — 커밋은 여전히 40/64자 hex object id다.
    """

    for env_name in _SOURCE_COMMIT_ENV_VARS:
        env_commit = _normalize_source_commit(os.getenv(env_name))
        if env_commit is not None:
            return env_commit

    try:
        build_ref = _BUILD_VCS_REF_PATH.read_text(encoding="utf-8")
    except OSError:
        build_ref = None
    build_commit = _normalize_source_commit(build_ref)
    if build_commit is not None:
        return build_commit

    if not (root / ".git").exists():
        raise ValueError(
            f"current source commit is unavailable; {_SOURCE_COMMIT_REMEDY}"
        )

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
        raise ValueError(
            f"current source commit is unavailable; {_SOURCE_COMMIT_REMEDY}"
        ) from exc
    commit = _normalize_source_commit(completed.stdout)
    if commit is None:
        raise ValueError("current source commit is not a full Git object id")
    return commit


def _normalize_source_commit(value: str | None) -> str | None:
    if value is None:
        return None
    commit = value.strip().lower()
    return commit if _SOURCE_COMMIT_RE.fullmatch(commit) else None


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
