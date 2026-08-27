"""Deterministic recommendation production and PAPER execution automation."""

from app.extensions.kasset.automation.backtest import (
    BacktestConfig,
    run_all_backtests,
    run_backtest,
)
from app.extensions.kasset.automation.consumer import PaperAutomationConsumer
from app.extensions.kasset.automation.contracts import (
    Action,
    BacktestResult,
    ClaimedRecommendation,
    DeterministicStrategy,
    ExecutionSafetyGate,
    ExternalEvidence,
    OwnerExecutionPolicy,
    PaperExecutionClaim,
    PaperOrderFacade,
    PriceBar,
    RationaleEvidence,
    RecommendationDraft,
    RecommendationPersistence,
    RecommendationService,
    StrategyName,
    StrategyResult,
)
from app.extensions.kasset.automation.producer import (
    RecommendationProducer,
    external_evidence_from_mapping,
)
from app.extensions.kasset.automation.strategies import (
    STRATEGIES,
    BreakoutStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    VolatilityTrendStrategy,
)

__all__ = [
    "Action",
    "BacktestConfig",
    "BacktestResult",
    "BreakoutStrategy",
    "ClaimedRecommendation",
    "DeterministicStrategy",
    "ExecutionSafetyGate",
    "ExternalEvidence",
    "MeanReversionStrategy",
    "MomentumStrategy",
    "OwnerExecutionPolicy",
    "PaperAutomationConsumer",
    "PaperExecutionClaim",
    "PaperOrderFacade",
    "PriceBar",
    "RecommendationDraft",
    "RecommendationProducer",
    "RecommendationPersistence",
    "RecommendationService",
    "RationaleEvidence",
    "STRATEGIES",
    "StrategyName",
    "StrategyResult",
    "VolatilityTrendStrategy",
    "external_evidence_from_mapping",
    "run_all_backtests",
    "run_backtest",
]
