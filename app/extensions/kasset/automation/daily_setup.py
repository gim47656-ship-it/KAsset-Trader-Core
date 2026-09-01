"""완료 일봉만 쓰는 breakout 계열 Daily Setup.

이 단계는 "오늘 장중에 무엇을 노려볼 수 있는가"만 정한다. 진입 방아쇠는
당기지 않는다. 방아쇠는 :mod:`app.extensions.kasset.automation.intraday_triggers`
가 완료된 장중 bar로만 판정한다.

Setup은 명시적으로 이름 붙은 조건으로 적합/부적합을 가린다. "N개 전략 중 2개가
동의" 같은 일반 정족수는 쓰지 않는다. Mean Reversion은 되돌림을 전제하는 다른
전략군이므로 breakout 합의에 표를 넣지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any, Final, Literal, cast

from app.extensions.kasset.automation.candidate_ranker import (
    CandidateRankResult,
    MarketKey,
)
from app.extensions.kasset.automation.contracts import (
    Action,
    DeterministicStrategy,
    PriceBar,
    StrategyFamily,
    StrategyResult,
    strategies_in_family,
)
from app.extensions.kasset.automation.market_session import completed_daily_bars
from app.extensions.kasset.automation.producer import (
    WeightedEnsembleDecision,
    compose_weighted_ensemble,
)
from app.extensions.kasset.automation.regime import RegimeAssessment

DAILY_SETUP_SCHEMA_VERSION: Final = "kasset.daily-setup.v1"

#: Setup 후보 수의 허용 범위. 설정으로 바꿀 수 있으나 이 범위 밖은 거부한다.
#: 너무 적으면 장중 방아쇠가 걸릴 후보가 남지 않고, 너무 많으면 하루 안에
#: 사람이 검증할 수 없는 폭이 된다.
DAILY_SETUP_LIMIT_RANGE: Final[tuple[int, int]] = (10, 20)

#: 일봉 상대강도 factor를 벤치마크 초과수익으로 계산했다는 ranker 근거 값.
#: 이 값이 아니면 벤치마크가 입증되지 않았다는 뜻이므로 fail-closed한다.
_BENCHMARK_STRENGTH_SOURCE: Final = "benchmark_excess_60_session_return"
_STRENGTH_SOURCE_CODE: Final = "relative_strength_source"
_STRENGTH_FACTOR_CODE: Final = "relative_strength"

_QUANTUM = Decimal("0.000001")


class SetupFeatureStatus(StrEnum):
    """Setup feature 하나의 닫힌 상태 집합."""

    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


class DailySetupStatus(StrEnum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DailySetupConfig:
    """Daily Setup의 유일한 불변 정책 객체."""

    #: 장중 Trigger 단계로 넘길 setup 후보 상한.
    setup_limit: int = 15
    #: 방향 판정에 쓰는 breakout 계열 최소 완료 일봉 수.
    minimum_completed_bars: int = 30
    #: 일봉 상대강도가 통과하기 위한 최소 벤치마크 초과 60세션 수익률.
    minimum_relative_strength: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        low, high = DAILY_SETUP_LIMIT_RANGE
        if type(self.setup_limit) is not int or not low <= self.setup_limit <= high:
            raise ValueError(f"setup_limit must be an int within [{low}, {high}]")
        if type(self.minimum_completed_bars) is not int or (
            self.minimum_completed_bars < 21
        ):
            raise ValueError("minimum_completed_bars must cover the 20-bar channel")
        value = self.minimum_relative_strength
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
            object.__setattr__(self, "minimum_relative_strength", value)
        if not value.is_finite():
            raise ValueError("minimum_relative_strength must be finite")


DEFAULT_DAILY_SETUP_CONFIG = DailySetupConfig()


@dataclass(frozen=True, slots=True)
class SetupFeature:
    """이름 붙은 setup 조건 하나와 그 관측값."""

    code: str
    status: SetupFeatureStatus
    value: str | None
    threshold: str | None
    detail: str

    def as_evidence(self) -> dict[str, object]:
        return {
            "code": self.code,
            "status": self.status.value,
            "value": self.value,
            "threshold": self.threshold,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class DailySetup:
    """완료 일봉으로 판정한 한 후보의 breakout 계열 setup."""

    schema_version: str
    symbol: str
    market: Literal["KRX", "US"]
    family: StrategyFamily
    status: DailySetupStatus
    direction: Action
    features: tuple[SetupFeature, ...]
    strategy_results: tuple[StrategyResult, ...]
    ensemble: WeightedEnsembleDecision | None
    completed_bar_count: int
    completed_through: datetime | None
    evaluated_at: datetime
    rejection_reason: str | None
    rank_position: int | None = None
    setup_position: int | None = None

    @property
    def qualified(self) -> bool:
        return self.status is DailySetupStatus.QUALIFIED

    def as_evidence(self) -> dict[str, object]:
        return {
            "title": "Completed daily breakout setup",
            "source": "kasset_daily_setup",
            "kind": "daily_setup",
            "schemaVersion": self.schema_version,
            "symbol": self.symbol,
            "market": self.market,
            "family": self.family.value,
            "status": self.status.value,
            "direction": self.direction.value,
            "completedBarCount": self.completed_bar_count,
            "completedThrough": _timestamp_text(self.completed_through),
            "evaluatedAt": _timestamp_text(self.evaluated_at),
            "rejectionReason": self.rejection_reason,
            "rankPosition": self.rank_position,
            "setupPosition": self.setup_position,
            "ensembleScore": (
                _decimal_text(self.ensemble.score)
                if self.ensemble is not None
                else None
            ),
            "features": [item.as_evidence() for item in self.features],
        }


def evaluate_daily_setup(
    ranking: CandidateRankResult,
    bars: Sequence[PriceBar],
    *,
    market: Literal["KRX", "US"],
    regime: RegimeAssessment,
    strategies: Sequence[DeterministicStrategy],
    as_of: datetime,
    completed_cutoff: datetime | None = None,
    config: DailySetupConfig = DEFAULT_DAILY_SETUP_CONFIG,
) -> DailySetup:
    """한 후보의 setup을 완료 일봉만으로 판정한다.

    진행 중인 세션의 부분 일봉은 계산 전에 잘라낸다. 잘라낸 뒤 데이터가 모자라면
    점수를 깎지 않고 ``UNAVAILABLE``로 fail-closed한다.
    """

    evaluated_at = _aware_utc(as_of, "as_of")
    completed = completed_daily_bars(
        bars,
        market=market,
        as_of=evaluated_at,
        cutoff=completed_cutoff,
    )
    if not ranking.included:
        return _unavailable(
            ranking,
            market=market,
            evaluated_at=evaluated_at,
            completed=completed,
            reason=f"ranking_excluded:{ranking.exclusion_reason}",
        )
    if len(completed) < config.minimum_completed_bars:
        return _unavailable(
            ranking,
            market=market,
            evaluated_at=evaluated_at,
            completed=completed,
            reason="insufficient_completed_daily_bars",
        )

    results = tuple(
        strategy.evaluate(
            completed,
            symbol=ranking.symbol,
            market=cast(Any, market),
            as_of=evaluated_at,
        )
        for strategy in strategies
    )
    ensemble = compose_weighted_ensemble(
        results,
        regime.weights,
        family=StrategyFamily.BREAKOUT,
    )
    direction = ensemble.action
    if direction is Action.HOLD:
        return DailySetup(
            schema_version=DAILY_SETUP_SCHEMA_VERSION,
            symbol=ranking.symbol,
            market=market,
            family=StrategyFamily.BREAKOUT,
            status=DailySetupStatus.REJECTED,
            direction=Action.HOLD,
            features=(),
            strategy_results=results,
            ensemble=ensemble,
            completed_bar_count=len(completed),
            completed_through=completed[-1].timestamp,
            evaluated_at=evaluated_at,
            rejection_reason="no_breakout_family_direction",
            rank_position=ranking.rank_position,
        )

    features = (
        _breakout_structure_feature(results, direction=direction),
        _momentum_alignment_feature(results, direction=direction),
        _daily_relative_strength_feature(
            ranking,
            direction=direction,
            minimum=config.minimum_relative_strength,
        ),
        _mean_reversion_isolation_feature(results),
    )
    blocking = tuple(
        item for item in features if item.status is not SetupFeatureStatus.PASS
    )
    if blocking:
        status = (
            DailySetupStatus.UNAVAILABLE
            if any(item.status is SetupFeatureStatus.UNAVAILABLE for item in blocking)
            else DailySetupStatus.REJECTED
        )
        reason = ",".join(f"{item.code}={item.status.value}" for item in blocking)
    else:
        status = DailySetupStatus.QUALIFIED
        reason = None
    return DailySetup(
        schema_version=DAILY_SETUP_SCHEMA_VERSION,
        symbol=ranking.symbol,
        market=market,
        family=StrategyFamily.BREAKOUT,
        status=status,
        direction=direction,
        features=features,
        strategy_results=results,
        ensemble=ensemble,
        completed_bar_count=len(completed),
        completed_through=completed[-1].timestamp,
        evaluated_at=evaluated_at,
        rejection_reason=reason,
        rank_position=ranking.rank_position,
    )


def select_daily_setups(
    setups: Sequence[DailySetup],
    *,
    config: DailySetupConfig = DEFAULT_DAILY_SETUP_CONFIG,
) -> tuple[DailySetup, ...]:
    """적합 setup만 ranker 순위대로 상한까지 고른다.

    같은 입력이면 같은 순서를 내도록 rank 없는 행은 symbol로 정렬한다.
    """

    qualified = sorted(
        (item for item in setups if item.qualified),
        key=lambda item: (
            item.rank_position if item.rank_position is not None else 1_000_000,
            item.symbol,
            item.market,
        ),
    )
    return tuple(
        _with_setup_position(item, position)
        for position, item in enumerate(qualified[: config.setup_limit], start=1)
    )


def daily_setup_policy_evidence(
    config: DailySetupConfig,
) -> dict[str, object]:
    """운영자가 읽는 setup 정책 요약."""

    low, high = DAILY_SETUP_LIMIT_RANGE
    return {
        "schemaVersion": DAILY_SETUP_SCHEMA_VERSION,
        "family": StrategyFamily.BREAKOUT.value,
        "setupLimit": config.setup_limit,
        "setupLimitRange": [low, high],
        "minimumCompletedBars": config.minimum_completed_bars,
        "minimumRelativeStrength": _decimal_text(config.minimum_relative_strength),
        "votingStrategies": [
            item.value for item in strategies_in_family(StrategyFamily.BREAKOUT)
        ],
        "isolatedStrategies": [StrategyFamily.MEAN_REVERSION.value],
        "barScope": "completed_regular_session_daily_bars_only",
    }


def _with_setup_position(setup: DailySetup, position: int) -> DailySetup:
    return DailySetup(
        schema_version=setup.schema_version,
        symbol=setup.symbol,
        market=setup.market,
        family=setup.family,
        status=setup.status,
        direction=setup.direction,
        features=setup.features,
        strategy_results=setup.strategy_results,
        ensemble=setup.ensemble,
        completed_bar_count=setup.completed_bar_count,
        completed_through=setup.completed_through,
        evaluated_at=setup.evaluated_at,
        rejection_reason=setup.rejection_reason,
        rank_position=setup.rank_position,
        setup_position=position,
    )


def _unavailable(
    ranking: CandidateRankResult,
    *,
    market: Literal["KRX", "US"],
    evaluated_at: datetime,
    completed: Sequence[PriceBar],
    reason: str,
) -> DailySetup:
    return DailySetup(
        schema_version=DAILY_SETUP_SCHEMA_VERSION,
        symbol=ranking.symbol,
        market=market,
        family=StrategyFamily.BREAKOUT,
        status=DailySetupStatus.UNAVAILABLE,
        direction=Action.HOLD,
        features=(),
        strategy_results=(),
        ensemble=None,
        completed_bar_count=len(completed),
        completed_through=completed[-1].timestamp if completed else None,
        evaluated_at=evaluated_at,
        rejection_reason=reason,
        rank_position=ranking.rank_position,
    )


def _result_for(
    results: Sequence[StrategyResult],
    code: str,
) -> StrategyResult | None:
    return next((item for item in results if item.strategy.value == code), None)


def _breakout_structure_feature(
    results: Sequence[StrategyResult],
    *,
    direction: Action,
) -> SetupFeature:
    """전일 20봉 채널을 방향대로 돌파했는가."""

    result = _result_for(results, "breakout")
    if result is None:
        return SetupFeature(
            code="breakout_structure",
            status=SetupFeatureStatus.UNAVAILABLE,
            value=None,
            threshold=None,
            detail="breakout strategy result is missing",
        )
    distance = next(
        (item.value for item in result.evidence if item.code == "BREAKOUT_DISTANCE"),
        None,
    )
    passed = result.action == direction
    return SetupFeature(
        code="breakout_structure",
        status=SetupFeatureStatus.PASS if passed else SetupFeatureStatus.FAIL,
        value=result.action.value,
        threshold=direction.value,
        detail=(
            "prior 20-session channel breakout distance="
            f"{distance if distance is not None else 'unknown'}"
        ),
    )


def _momentum_alignment_feature(
    results: Sequence[StrategyResult],
    *,
    direction: Action,
) -> SetupFeature:
    """단·중기 수익률이 같은 방향인가."""

    result = _result_for(results, "momentum")
    if result is None:
        return SetupFeature(
            code="momentum_alignment",
            status=SetupFeatureStatus.UNAVAILABLE,
            value=None,
            threshold=None,
            detail="momentum strategy result is missing",
        )
    passed = result.action == direction
    values = {item.code: item.value for item in result.evidence}
    return SetupFeature(
        code="momentum_alignment",
        status=SetupFeatureStatus.PASS if passed else SetupFeatureStatus.FAIL,
        value=result.action.value,
        threshold=direction.value,
        detail=(
            f"return5={values.get('RETURN_5', 'unknown')} "
            f"return20={values.get('RETURN_20', 'unknown')}"
        ),
    )


def _daily_relative_strength_feature(
    ranking: CandidateRankResult,
    *,
    direction: Action,
    minimum: Decimal,
) -> SetupFeature:
    """완료 일봉 60세션 벤치마크 초과수익이 방향을 지지하는가.

    ranker가 벤치마크를 입증하지 못하고 cross-sectional 대체값을 썼으면 값을
    추정하지 않고 ``UNAVAILABLE``로 fail-closed한다.
    """

    source = next(
        (item.value for item in ranking.evidence if item.code == _STRENGTH_SOURCE_CODE),
        None,
    )
    if source != _BENCHMARK_STRENGTH_SOURCE:
        return SetupFeature(
            code="daily_relative_strength",
            status=SetupFeatureStatus.UNAVAILABLE,
            value=None,
            threshold=_decimal_text(minimum),
            detail=(
                "benchmark 60-session excess return is unproven; "
                f"ranker source={source or 'missing'}"
            ),
        )
    factor = next(
        (item for item in ranking.factor_scores if item.code == _STRENGTH_FACTOR_CODE),
        None,
    )
    if factor is None:
        return SetupFeature(
            code="daily_relative_strength",
            status=SetupFeatureStatus.UNAVAILABLE,
            value=None,
            threshold=_decimal_text(minimum),
            detail="ranker relative_strength factor is missing",
        )
    excess = factor.raw_value
    if not isinstance(excess, Decimal):
        excess = Decimal(str(excess))
    if not excess.is_finite():
        return SetupFeature(
            code="daily_relative_strength",
            status=SetupFeatureStatus.UNAVAILABLE,
            value=None,
            threshold=_decimal_text(minimum),
            detail="benchmark excess return is not finite",
        )
    passed = excess >= minimum if direction is Action.BUY else excess <= -minimum
    return SetupFeature(
        code="daily_relative_strength",
        status=SetupFeatureStatus.PASS if passed else SetupFeatureStatus.FAIL,
        value=_decimal_text(excess),
        threshold=_decimal_text(minimum if direction is Action.BUY else -minimum),
        detail=f"benchmark excess 60-session return for {direction.value}",
    )


def _mean_reversion_isolation_feature(
    results: Sequence[StrategyResult],
) -> SetupFeature:
    """Mean Reversion이 breakout 합의에 표를 넣지 않았음을 근거로 남긴다."""

    result = _result_for(results, "mean_reversion")
    return SetupFeature(
        code="mean_reversion_isolated",
        status=SetupFeatureStatus.PASS,
        value=result.action.value if result is not None else None,
        threshold="not_counted",
        detail=(
            "mean reversion belongs to a separate family and never votes in the "
            "breakout consensus"
        ),
    )


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN), "f")


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def daily_setups_by_key(
    setups: Sequence[DailySetup],
) -> Mapping[tuple[MarketKey, str], DailySetup]:
    """``(ranker market, symbol)`` 키로 setup을 찾을 수 있게 한다."""

    return {
        (cast(MarketKey, "KR" if item.market == "KRX" else "US"), item.symbol): item
        for item in setups
    }


__all__ = [
    "DAILY_SETUP_LIMIT_RANGE",
    "DAILY_SETUP_SCHEMA_VERSION",
    "DEFAULT_DAILY_SETUP_CONFIG",
    "DailySetup",
    "DailySetupConfig",
    "DailySetupStatus",
    "SetupFeature",
    "SetupFeatureStatus",
    "daily_setup_policy_evidence",
    "daily_setups_by_key",
    "evaluate_daily_setup",
    "select_daily_setups",
]
