"""Screener-to-recommendation vertical slice for APPROVAL and AUTO_PAPER."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal, cast

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.model_router import AnalysisKind, OpenAiModelRouter
from app.extensions.kasset.ai.runtime_config import (
    AiLane,
    build_ai_availability,
    build_ai_route_catalog,
)
from app.extensions.kasset.api.watchlist import watchlist_service
from app.extensions.kasset.automation.ai_shadow import (
    AiShadowObservation,
    build_ai_shadow_observation,
)
from app.extensions.kasset.automation.benchmark_relative_strength import (
    load_candidate_benchmark_returns,
)
from app.extensions.kasset.automation.candidate_ranker import (
    DEFAULT_CANDIDATE_RANKER_CONFIG,
    CandidateKey,
    CandidateMetadata,
    CandidateRanker,
    CandidateRankerConfig,
    CandidateRankResult,
    cap_candidate_universe,
    normalize_allowed_markets,
)
from app.extensions.kasset.automation.contracts import (
    Action,
    ExternalEvidence,
    PriceBar,
    StrategyResult,
)
from app.extensions.kasset.automation.policy import AITradingPolicyService
from app.extensions.kasset.automation.position_manager_service import (
    PaperPositionManagerService,
)
from app.extensions.kasset.automation.producer import (
    RecommendationProducer,
    WeightedEnsembleDecision,
    compose_weighted_ensemble,
)
from app.extensions.kasset.automation.regime import (
    RegimeAssessment,
    assess_market_regime,
)
from app.extensions.kasset.automation.shadow_setups import (
    DEFAULT_SHADOW_SETUP_CONFIG,
    SHADOW_SETUPS_SCHEMA_VERSION,
    ShadowSetupConfig,
    evaluate_ranked_shadow_setups,
    shadow_setups_evidence,
)
from app.extensions.kasset.automation.strategies import STRATEGIES
from app.extensions.kasset.automation.strategy_artifact import (
    current_strategy_artifact,
)
from app.extensions.kasset.automation.strategy_promotion import (
    DEFAULT_PAPER_STRATEGY_KEY,
    DEFAULT_PAPER_STRATEGY_VERSION,
)
from app.extensions.kasset.daily_routine_service import daily_routine_service
from app.extensions.kasset.models import AndroidPaperAccount
from app.jobs.watch_market_data import is_market_open
from app.models.ai_recommendations import AIRecommendation
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.news import NewsArticle
from app.models.paper_trading import PaperPosition
from app.models.symbol_master import SymbolMaster
from app.models.trading import InstrumentType, User, UserRole
from app.services.ai_recommendations.service import AIRecommendationService
from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey
from app.services.disclosures.feed_sources import DISCLOSURE_FEED_SOURCES
from app.services.kasset_automation_audit import (
    new_cycle_trace_id,
    record_automation_cycle_event,
)
from app.services.symbol_news_store import load_symbol_news

logger = logging.getLogger(__name__)
_RECOMMENDATION_LIMIT = 5
_OWNER_COOLDOWN = timedelta(hours=1)

#: 검토 lane에 쓸 수 있는 route가 없어 cycle이 AI 없이 도는 상태의 사유.
#: 정책 자체는 정상이므로 ``AiAvailability``의 사유 코드와 층을 구분한다.
_AI_REVIEW_UNAVAILABLE = "review_routes_unavailable"
_NO_REGULAR_MARKET_OPEN = "no_regular_market_open"
_NO_CONFIGURED_REGULAR_MARKET_OPEN = "no_configured_regular_market_open"


def _open_regular_markets(*, now: datetime) -> frozenset[str]:
    """정규장 calendar가 현재 열려 있다고 입증한 시장만 반환한다."""

    open_markets: set[str] = set()
    for market, market_key in (("kr", "KR"), ("us", "US")):
        try:
            if is_market_open(market, now=now):
                open_markets.add(market_key)
        except Exception:
            # calendar 조회가 깨진 시장은 후보/AI 호출로 넘어가지 않는다.
            logger.exception(
                "kasset regular market gate failed closed: market=%s",
                market,
            )
    return frozenset(open_markets)


def _regular_market_skip_result(
    *,
    owner_user_id: int,
    cycle_trace_id: str,
    reason: str = _NO_REGULAR_MARKET_OPEN,
) -> dict[str, object]:
    return {
        "ownerUserId": owner_user_id,
        "cycleTraceId": cycle_trace_id,
        "skipped": reason,
        "candidateCount": 0,
        "rankedCount": 0,
        "strategyEvaluatedCount": 0,
        "strategyActionableCount": 0,
        "aiReviewedCount": 0,
        "aiFailureCount": 0,
        "recommendationIds": [],
        "positionExitRecommendationIds": [],
    }


def _collection_policy_payload(
    config: CandidateRankerConfig,
) -> dict[str, object]:
    return {
        "candidateLimit": config.candidate_limit,
        "minimumCandidateTarget": config.minimum_candidate_target,
        "strategyReviewLimit": config.strategy_review_limit,
        "recommendationLimit": _RECOMMENDATION_LIMIT,
        "aiReviewActions": [Action.BUY.value, Action.SELL.value],
    }


@dataclass(frozen=True, slots=True)
class TradingCandidate:
    symbol: str
    market: Literal["KRX", "US"]
    name: str | None
    source: str
    turnover: Decimal | None = None
    volume: Decimal | None = None
    is_held: bool = False
    is_watchlisted: bool = False
    eligible_for_new_buy: bool = True

    @property
    def ranker_market(self) -> Literal["KR", "US"]:
        return "KR" if self.market == "KRX" else "US"

    @property
    def ranker_key(self) -> CandidateKey:
        return self.ranker_market, self.symbol


@dataclass(frozen=True, slots=True)
class EvaluatedCandidate:
    candidate: TradingCandidate
    strategy_results: tuple[StrategyResult, ...]
    ensemble: WeightedEnsembleDecision
    factor_ranking: CandidateRankResult | None = None
    regime: RegimeAssessment | None = None


@dataclass(frozen=True, slots=True)
class ReviewedCandidate:
    evaluated: EvaluatedCandidate
    external: ExternalEvidence
    events: tuple[Mapping[str, object], ...]
    event_score: Decimal
    score: Decimal
    ai_shadow: AiShadowObservation


@dataclass(frozen=True, slots=True)
class AIReviewOutcome:
    symbol: str
    market: str
    strategy_action: str
    ai_action: str | None
    confidence: str | None
    reason: str
    observed_at: str
    provider: str | None = None
    tier: str | None = None
    model_id: str | None = None
    rationale_tags: tuple[str, ...] = ()
    #: 이 후보가 실제로 추천 행으로 저장된 경우의 추천 id. 채택되지 않았거나
    #: 상위 N개에 들지 못해 저장되지 않았으면 None으로 남는다.
    recommendation_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "strategyAction": self.strategy_action,
            "aiAction": self.ai_action,
            "confidence": self.confidence,
            "reason": self.reason,
            "observedAt": self.observed_at,
            "provider": self.provider,
            "tier": self.tier,
            "modelId": self.model_id,
            "rationaleTags": list(self.rationale_tags),
            "recommendationId": self.recommendation_id,
        }


async def _load_live_kr_candidates(
    *,
    limit: int = DEFAULT_CANDIDATE_RANKER_CONFIG.candidate_limit,
) -> tuple[TradingCandidate, ...]:
    from app.services.invest_kr_fundamentals_snapshots.provider import (
        TvScreenerKrFundamentalsProvider,
    )

    rows = await TvScreenerKrFundamentalsProvider(timeout=30).fetch_rows(limit=limit)
    ordered = sorted(
        rows,
        key=lambda row: (
            -((row.price or Decimal("0")) * (row.volume or Decimal("0"))),
            str(row.symbol).strip().upper(),
        ),
    )
    candidates: list[TradingCandidate] = []
    seen: set[str] = set()
    for row in ordered:
        symbol = str(row.symbol).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        name = str(row.name or "").strip()
        candidates.append(
            TradingCandidate(
                symbol=symbol,
                market="KRX",
                name=name if name and name != symbol else None,
                source="tvscreener_kr",
                turnover=(row.price or Decimal("0")) * (row.volume or Decimal("0")),
                volume=row.volume,
            )
        )
    return tuple(candidates)


class AIRecommendationVerticalSlice:
    """Run one owner through existing candles, strategies, AI, and persistence."""

    def __init__(
        self,
        db: AsyncSession,
        ai_router: OpenAiModelRouter | None,
        *,
        now: datetime,
        live_candidates_cache: dict[str, tuple[TradingCandidate, ...]] | None = None,
        allowed_markets: frozenset[str] | None = None,
        ranker_config: CandidateRankerConfig = DEFAULT_CANDIDATE_RANKER_CONFIG,
        shadow_setup_config: ShadowSetupConfig = DEFAULT_SHADOW_SETUP_CONFIG,
        cycle_trace_id: str | None = None,
    ) -> None:
        self._db = db
        self._ai_router = ai_router
        self._now = _aware_utc(now)
        # 후보를 한 건도 만지기 전에 이 cycle의 추적 id를 확정한다. 호출자가
        # 넘겨주면 그 값을 쓰고, 그래야 owner cycle이 예외로 끝나도 원장과
        # 추천이 같은 추적 id를 공유한다.
        self._cycle_trace_id = cycle_trace_id or new_cycle_trace_id()
        self._policy = AITradingPolicyService()
        self._live_candidates_cache = (
            live_candidates_cache if live_candidates_cache is not None else {}
        )
        self._allowed_markets = (
            normalize_allowed_markets(allowed_markets)
            if allowed_markets is not None
            else None
        )
        self._ranker_config = ranker_config
        self._ranker = CandidateRanker(ranker_config)
        self._shadow_setup_config = shadow_setup_config
        self._strategy_artifact_fingerprint = current_strategy_artifact().fingerprint
        self._position_manager = PaperPositionManagerService(
            db,
            now=self._now,
            strategy_version=DEFAULT_PAPER_STRATEGY_VERSION,
            strategy_fingerprint=self._strategy_artifact_fingerprint,
        )

    async def run_owner(self, owner_user_id: int) -> dict[str, object]:
        """Produce one owner's recommendations under this cycle's trace id."""

        cooldown_active = await self._cooldown_active(owner_user_id)
        position_exit_ids = await self._position_manager.run_owner(owner_user_id)
        if cooldown_active or position_exit_ids:
            return {
                "ownerUserId": owner_user_id,
                "cycleTraceId": self._cycle_trace_id,
                "skipped": (
                    "position_exit_recommendation_created"
                    if position_exit_ids
                    else "recommendation_cooldown_active"
                ),
                "candidateCount": 0,
                "positionExitRecommendationIds": list(position_exit_ids),
                "recommendationIds": list(position_exit_ids),
            }
        allowed_markets = self._allowed_markets
        if allowed_markets is not None:
            configured_markets = await daily_routine_service.recommendation_markets(
                self._db,
                owner_user_id,
                now=self._now,
            )
            configured_market_keys = frozenset(
                market.upper() for market in configured_markets
            )
            configured_allowed_markets = (
                normalize_allowed_markets(configured_market_keys)
                if configured_market_keys
                else frozenset()
            )
            allowed_markets = allowed_markets & configured_allowed_markets
            if not allowed_markets:
                return _regular_market_skip_result(
                    owner_user_id=owner_user_id,
                    cycle_trace_id=self._cycle_trace_id,
                    reason=_NO_CONFIGURED_REGULAR_MARKET_OPEN,
                )
        if self._ai_router is None:
            return {
                "ownerUserId": owner_user_id,
                "cycleTraceId": self._cycle_trace_id,
                "skipped": "ai_unavailable",
                "candidateCount": 0,
                "positionExitRecommendationIds": [],
                "recommendationIds": [],
            }

        snapshot = await self._policy.get_snapshot(
            self._db,
            owner_user_id,
            now=self._now,
            execution_limit=0,
        )
        if allowed_markets is None:
            configured_markets = await daily_routine_service.recommendation_markets(
                self._db,
                owner_user_id,
                now=self._now,
            )
            allowed_markets = normalize_allowed_markets(
                frozenset(market.upper() for market in configured_markets)
            )
        candidates = await self._load_candidates(
            owner_user_id,
            currency=snapshot.limits.currency,
            allowed_markets=allowed_markets,
        )
        recommendation_candidates = [
            candidate for candidate in candidates if candidate.eligible_for_new_buy
        ]
        management_only = [
            candidate
            for candidate in candidates
            if candidate.is_held and not candidate.eligible_for_new_buy
        ]
        if not recommendation_candidates:
            return {
                "ownerUserId": owner_user_id,
                "cycleTraceId": self._cycle_trace_id,
                "skipped": "screener_candidates_unavailable",
                "candidateCount": 0,
                "heldManagementOnly": [
                    {
                        "symbol": candidate.symbol,
                        "market": candidate.ranker_market,
                        "isHeld": True,
                    }
                    for candidate in management_only
                ],
                "recommendationIds": [],
                "positionExitRecommendationIds": [],
            }

        candle_sync = await self._sync_missing_kr_candles(
            [
                candidate
                for candidate in recommendation_candidates
                if candidate.market == "KRX"
            ]
        )
        bars_by_candidate = await self._load_candidate_bars(recommendation_candidates)
        benchmark_returns = await load_candidate_benchmark_returns(
            self._db,
            [candidate.ranker_key for candidate in candidates],
            as_of=self._now,
            maximum_age=self._ranker_config.maximum_bar_age,
        )
        metadata = tuple(_candidate_metadata(candidate) for candidate in candidates)
        ranking = self._ranker.rank(
            metadata,
            bars_by_candidate,
            as_of=self._now,
            allowed_markets=allowed_markets,
            benchmark_returns_60_by_candidate=benchmark_returns,
        )
        shadow_setups = evaluate_ranked_shadow_setups(
            tuple(result.key for result in ranking.ranked),
            bars_by_candidate,
            as_of=self._now,
            limit=_RECOMMENDATION_LIMIT,
            config=self._shadow_setup_config,
        )
        ranks_for_review = ranking.for_strategy_review(
            self._ranker_config.strategy_review_limit
        )
        candidate_by_key = {candidate.ranker_key: candidate for candidate in candidates}
        selected_candidates = [
            candidate_by_key[result.key]
            for result in ranks_for_review
            if result.key in candidate_by_key
        ]
        selected_rankings = {result.key: result for result in ranks_for_review}
        regimes = {
            market: assess_market_regime(
                {
                    symbol: bars
                    for (bar_market, symbol), bars in bars_by_candidate.items()
                    if bar_market == market
                }
            )
            for market in sorted(allowed_markets)
        }
        evaluated = self._evaluate_candidates(
            selected_candidates,
            bars_by_candidate,
            regimes,
            factor_rankings=selected_rankings,
        )
        actionable = [
            item
            for item in evaluated
            if item.ensemble.action in {Action.BUY, Action.SELL}
        ]
        reviewed: list[ReviewedCandidate] = []
        review_outcomes: list[AIReviewOutcome] = []
        ai_failures = 0
        review_rejections: Counter[str] = Counter()
        for item in actionable:
            try:
                candidate_regime = item.regime
                if candidate_regime is None:
                    continue
                (
                    reviewed_item,
                    rejection_reason,
                    review_outcome,
                ) = await self._review_candidate(
                    owner_user_id,
                    item,
                    candidate_regime,
                )
            except AiProviderUnavailable:
                ai_failures += 1
                rejection_reason = "provider_unavailable"
                review_rejections[rejection_reason] += 1
                review_outcomes.append(
                    self._review_outcome(item, reason=rejection_reason)
                )
                continue
            review_outcomes.append(review_outcome)
            if rejection_reason is not None:
                review_rejections[rejection_reason] += 1
            if reviewed_item is not None:
                reviewed.append(reviewed_item)

        reviewed.sort(
            key=lambda item: (
                -item.score,
                item.evaluated.candidate.symbol,
                item.evaluated.candidate.market,
            )
        )
        recommendation_ids: list[str] = []
        # 저장된 추천을 그 추천을 만든 AI 검토 결과로 되돌려 잇는다. 후보는
        # (symbol, market)로 이미 중복 제거돼 있으므로 이 키는 유일하다.
        recommendation_id_by_candidate: dict[tuple[str, str], str] = {}
        total = len(ranking.ranked)
        for position, item in enumerate(reviewed[:_RECOMMENDATION_LIMIT], start=1):
            candidate_regime = item.evaluated.regime
            if candidate_regime is None:
                continue
            row = await self._persist_recommendation(
                owner_user_id,
                item,
                candidate_regime,
                position=position,
                total=total,
                snapshot=snapshot,
            )
            persisted_candidate = item.evaluated.candidate
            recommendation_id_by_candidate[
                (persisted_candidate.symbol, persisted_candidate.ranker_market)
            ] = row.id
            if row.action in {"BUY", "SELL"}:
                recommendation_ids.append(row.id)

        review_outcomes = [
            replace(
                outcome,
                recommendation_id=recommendation_id_by_candidate.get(
                    (outcome.symbol, outcome.market)
                ),
            )
            for outcome in review_outcomes
        ]

        ranked_evidence = [
            result.as_evidence()
            for result in ranking.ranked[: self._ranker_config.strategy_review_limit]
        ]
        result: dict[str, object] = {
            "ownerUserId": owner_user_id,
            "cycleTraceId": self._cycle_trace_id,
            "candidateCount": len(recommendation_candidates),
            "rankedCount": len(ranking.ranked),
            "strategyActionableCount": len(actionable),
            "candidateMarkets": dict(
                sorted(
                    Counter(
                        candidate.ranker_market
                        for candidate in recommendation_candidates
                    ).items()
                )
            ),
            "candidateSources": dict(
                sorted(
                    Counter(
                        source
                        for candidate in recommendation_candidates
                        for source in candidate.source.split("|")
                        if source
                    ).items()
                )
            ),
            "candidateTargetMet": (
                len(ranking.ranked) >= self._ranker_config.minimum_candidate_target
            ),
            "strategyEvaluatedCount": len(evaluated),
            "aiReviewedCount": len(review_outcomes),
            "aiFailureCount": ai_failures,
            "aiReviewRejections": dict(sorted(review_rejections.items())),
            "aiReviewOutcomes": [outcome.as_dict() for outcome in review_outcomes],
            "collectionPolicy": self._collection_policy(),
            "candleSync": candle_sync,
            "regime": (
                next(iter(regimes.values())).regime.value
                if len(regimes) == 1
                else "MULTI_MARKET"
            ),
            "regimes": {
                market: assessment.regime.value
                for market, assessment in regimes.items()
            },
            "rankedCandidates": ranked_evidence,
            "shadowSetups": {
                "schemaVersion": SHADOW_SETUPS_SCHEMA_VERSION,
                "mode": "SHADOW",
                "active": self._shadow_setup_config.feature_enabled,
                "configFingerprint": self._shadow_setup_config.fingerprint,
                "candidates": [shadow_setups_evidence(item) for item in shadow_setups],
            },
            "candidateExclusions": [
                result.as_evidence() for result in ranking.excluded
            ],
            "heldManagementOnly": [
                {
                    "symbol": candidate.symbol,
                    "market": candidate.ranker_market,
                    "isHeld": True,
                }
                for candidate in management_only
            ],
            "recommendationIds": recommendation_ids,
            "positionExitRecommendationIds": [],
        }
        if len(ranking.ranked) < self._ranker_config.minimum_candidate_target:
            result["dataPrerequisite"] = (
                "fewer than 50 screener candidates have usable 52-week daily candles"
            )
        if not actionable:
            result["skipped"] = "no_dynamic_ensemble_signal"
        elif not reviewed:
            result["skipped"] = "no_ai_confirmed_signal"
        return result

    async def _load_candidate_bars(
        self,
        candidates: Sequence[TradingCandidate],
    ) -> dict[CandidateKey, tuple[PriceBar, ...]]:
        repository = DailyCandlesRepository(session=self._db)
        output: dict[CandidateKey, tuple[PriceBar, ...]] = {}
        for market in ("KR", "US"):
            symbols = [
                candidate.symbol
                for candidate in candidates
                if candidate.ranker_market == market
            ]
            if not symbols:
                continue
            rows_by_symbol = await repository.fetch_recent_batch(
                market=MarketKey.KR if market == "KR" else MarketKey.US,
                symbols=symbols,
                partition="KRX" if market == "KR" else None,
                count=self._ranker_config.history_bars,
            )
            for symbol, rows in rows_by_symbol.items():
                output[(market, symbol)] = _price_bars(rows)
        return output

    async def _load_candidates(
        self,
        owner_user_id: int,
        *,
        currency: str,
        allowed_markets: frozenset[str] | None = None,
    ) -> list[TradingCandidate]:
        requested_markets = normalize_allowed_markets(
            allowed_markets
            if allowed_markets is not None
            else frozenset({"KR" if currency == "KRW" else "US"})
        )
        ordered: dict[CandidateKey, TradingCandidate] = {}
        watchlist = await watchlist_service.list_items(self._db, owner_user_id)
        for item in watchlist.items:
            market = "KR" if item.market == "KRX" else item.market
            if market not in requested_markets:
                continue
            candidate = TradingCandidate(
                symbol=item.symbol.strip().upper(),
                market=cast(Any, "KRX" if market == "KR" else "US"),
                name=item.name,
                source="watchlist",
                is_watchlisted=True,
            )
            ordered[candidate.ranker_key] = _merge_trading_candidate(
                ordered.get(candidate.ranker_key),
                candidate,
            )

        holdings = (
            await self._db.scalars(
                select(PaperPosition)
                .join(
                    AndroidPaperAccount,
                    AndroidPaperAccount.paper_account_id == PaperPosition.account_id,
                )
                .where(
                    AndroidPaperAccount.owner_user_id == owner_user_id,
                    PaperPosition.quantity > 0,
                    PaperPosition.instrument_type.in_(
                        (InstrumentType.equity_kr, InstrumentType.equity_us)
                    ),
                )
                .order_by(PaperPosition.instrument_type, PaperPosition.symbol)
            )
        ).all()
        for position in holdings:
            market = (
                "KR" if position.instrument_type == InstrumentType.equity_kr else "US"
            )
            candidate = TradingCandidate(
                symbol=str(position.symbol).strip().upper(),
                market=cast(Any, "KRX" if market == "KR" else "US"),
                name=None,
                source="paper_holding",
                is_held=True,
                eligible_for_new_buy=market in requested_markets,
            )
            ordered[candidate.ranker_key] = _merge_trading_candidate(
                ordered.get(candidate.ranker_key),
                candidate,
            )

        snapshot_candidates: dict[str, list[TradingCandidate]] = {
            market: [] for market in sorted(requested_markets)
        }
        for market in sorted(requested_markets):
            snapshot_market = market.lower()
            recommendation_market = "KRX" if market == "KR" else "US"
            latest_date = await self._db.scalar(
                select(func.max(InvestScreenerSnapshot.snapshot_date)).where(
                    InvestScreenerSnapshot.market == snapshot_market
                )
            )
            if latest_date is None:
                continue
            rows = (
                await self._db.scalars(
                    select(InvestScreenerSnapshot)
                    .where(
                        InvestScreenerSnapshot.market == snapshot_market,
                        InvestScreenerSnapshot.snapshot_date == latest_date,
                    )
                    .order_by(
                        InvestScreenerSnapshot.daily_turnover.desc().nullslast(),
                        InvestScreenerSnapshot.daily_volume.desc().nullslast(),
                        InvestScreenerSnapshot.symbol,
                    )
                    .limit(self._ranker_config.candidate_limit)
                )
            ).all()
            symbols = tuple(
                dict.fromkeys(
                    str(row.symbol).strip().upper()
                    for row in rows
                    if str(row.symbol).strip()
                )
            )
            names = (
                {
                    symbol: name
                    for symbol, name in (
                        await self._db.execute(
                            select(SymbolMaster.symbol, SymbolMaster.name).where(
                                SymbolMaster.market == recommendation_market,
                                SymbolMaster.symbol.in_(symbols),
                            )
                        )
                    ).all()
                    if name and name.strip() and name.strip() != symbol
                }
                if symbols
                else {}
            )
            for row in rows:
                symbol = str(row.symbol).strip().upper()
                if not symbol:
                    continue
                snapshot_candidates[market].append(
                    TradingCandidate(
                        symbol=symbol,
                        market=cast(Any, recommendation_market),
                        name=names.get(symbol),
                        source=(f"invest_screener_snapshots:{latest_date.isoformat()}"),
                        turnover=(
                            Decimal(str(row.daily_turnover))
                            if row.daily_turnover is not None
                            else None
                        ),
                        volume=(
                            Decimal(str(row.daily_volume))
                            if row.daily_volume is not None
                            else None
                        ),
                    )
                )

        for index in range(self._ranker_config.candidate_limit):
            for market in sorted(requested_markets):
                rows = snapshot_candidates[market]
                if index >= len(rows):
                    continue
                candidate = rows[index]
                ordered[candidate.ranker_key] = _merge_trading_candidate(
                    ordered.get(candidate.ranker_key),
                    candidate,
                )

        kr_count = sum(
            candidate.ranker_market == "KR" and candidate.eligible_for_new_buy
            for candidate in ordered.values()
        )
        if (
            "KR" in requested_markets
            and kr_count < self._ranker_config.minimum_candidate_target
        ):
            live = self._live_candidates_cache.get("kr")
            if live is None:
                live = await _load_live_kr_candidates(
                    limit=self._ranker_config.candidate_limit
                )
                self._live_candidates_cache["kr"] = live
            for candidate in live:
                ordered[candidate.ranker_key] = _merge_trading_candidate(
                    ordered.get(candidate.ranker_key),
                    candidate,
                )

        metadata = tuple(
            _candidate_metadata(candidate) for candidate in ordered.values()
        )
        capped = cap_candidate_universe(
            metadata,
            limit=self._ranker_config.candidate_limit,
        )
        return [ordered[item.key] for item in capped]

    async def _sync_missing_kr_candles(
        self,
        candidates: Sequence[TradingCandidate],
    ) -> dict[str, int]:
        if not candidates:
            return {"requested": 0, "synced": 0, "failed": 0}
        repository = DailyCandlesRepository(session=self._db)
        existing = await repository.fetch_recent_batch(
            market=MarketKey.KR,
            symbols=[candidate.symbol for candidate in candidates],
            partition="KRX",
            count=self._ranker_config.history_bars,
        )
        missing = [
            candidate
            for candidate in candidates
            if len(existing.get(candidate.symbol, ()))
            < self._ranker_config.minimum_history_bars
        ]
        if not missing:
            return {"requested": 0, "synced": 0, "failed": 0}

        from app.services.daily_candles.converters import frame_to_rows
        from app.services.market_data.toss_ohlcv import fetch_daily_toss_frame

        semaphore = asyncio.Semaphore(6)

        async def fetch(candidate: TradingCandidate):
            async with semaphore:
                try:
                    frame = await fetch_daily_toss_frame(
                        symbol=candidate.symbol,
                        count=self._ranker_config.history_bars,
                    )
                    return candidate.symbol, frame, None
                except Exception as exc:  # noqa: BLE001 - bounded per-symbol failure
                    return candidate.symbol, None, type(exc).__name__

        fetched = await asyncio.gather(*(fetch(candidate) for candidate in missing))
        synced = 0
        failed = 0
        for symbol, frame, error in fetched:
            if error is not None or frame is None:
                failed += 1
                continue
            rows = frame_to_rows(frame, symbol=symbol, partition="KRX", source="toss")
            if len(rows) < self._ranker_config.minimum_history_bars:
                failed += 1
                continue
            await repository.upsert_rows(market=MarketKey.KR, rows=rows)
            synced += 1
        await self._db.commit()
        return {"requested": len(missing), "synced": synced, "failed": failed}

    def _evaluate_candidates(
        self,
        candidates: Sequence[TradingCandidate],
        bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
        regimes: Mapping[str, RegimeAssessment],
        *,
        factor_rankings: Mapping[CandidateKey, CandidateRankResult],
    ) -> list[EvaluatedCandidate]:
        evaluated: list[EvaluatedCandidate] = []
        for candidate in candidates:
            ranking = factor_rankings.get(candidate.ranker_key)
            if ranking is None or not ranking.included:
                continue
            regime = regimes.get(candidate.ranker_market)
            if regime is None:
                continue
            bars = bars_by_candidate.get(candidate.ranker_key, ())
            if len(bars) < 20:
                continue
            results = tuple(
                strategy.evaluate(
                    bars,
                    symbol=candidate.symbol,
                    market=cast(Any, candidate.market),
                    as_of=self._now,
                )
                for strategy in STRATEGIES
            )
            evaluated.append(
                EvaluatedCandidate(
                    candidate=candidate,
                    strategy_results=results,
                    ensemble=compose_weighted_ensemble(results, regime.weights),
                    factor_ranking=ranking,
                    regime=regime,
                )
            )
        return evaluated

    async def _review_candidate(
        self,
        owner_user_id: int,
        item: EvaluatedCandidate,
        regime: RegimeAssessment,
    ) -> tuple[ReviewedCandidate | None, str | None, AIReviewOutcome]:
        if self._ai_router is None:
            reason = "provider_unavailable"
            return None, reason, self._review_outcome(item, reason=reason)
        ranking = item.factor_ranking
        if ranking is None or not ranking.included or ranking.valid_until is None:
            reason = "ranking_unavailable"
            return None, reason, self._review_outcome(item, reason=reason)
        events = await self._event_evidence(item.candidate)
        payload = {
            "symbol": item.candidate.symbol,
            "market": item.candidate.market,
            "candidateSource": item.candidate.source,
            "candidateRanking": ranking.as_evidence(),
            "regime": regime.regime.value,
            "regimeDetail": regime.detail,
            "strategyVotes": list(item.ensemble.votes),
            "entry": _level_text(item.ensemble.agreeing, "entry"),
            "stop": _level_text(item.ensemble.agreeing, "stop"),
            "target": _level_text(item.ensemble.agreeing, "target"),
            "events": [dict(event) for event in events],
        }
        try:
            verdict = await self._ai_router.analyze_for_owner(
                self._db,
                owner_user_id,
                AnalysisKind.CANDIDATE_REVIEW,
                payload,
                correlation_id=(
                    f"ai-vertical:{owner_user_id}:{item.candidate.market}:"
                    f"{item.candidate.symbol}:{int(self._now.timestamp())}"
                ),
            )
        except ValidationError:
            reason = "invalid_ai_response"
            logger.warning(
                "KAsset AI candidate response rejected: market=%s symbol=%s",
                item.candidate.market,
                item.candidate.symbol,
            )
            return None, reason, self._review_outcome(item, reason=reason)
        shadow_observation = build_ai_shadow_observation(
            verdict,
            observed_at=self._now,
        )
        action_text = str(verdict.action).strip().upper()
        action = (
            Action(action_text)
            if action_text in {"BUY", "SELL", "HOLD"}
            else Action.HOLD
        )
        if action != item.ensemble.action:
            reason = "action_mismatch"
            return (
                None,
                reason,
                self._review_outcome(
                    item,
                    reason=reason,
                    observation=shadow_observation,
                ),
            )
        confidence = Decimal(str(verdict.confidence))
        if not confidence.is_finite() or confidence < Decimal("0.50"):
            reason = "low_confidence"
            return (
                None,
                reason,
                self._review_outcome(
                    item,
                    reason=reason,
                    observation=shadow_observation,
                ),
            )
        valid_until = min(
            (
                result.valid_until.astimezone(UTC)
                for result in item.strategy_results
                if result.valid_until.tzinfo is not None
                and result.valid_until.utcoffset() is not None
            ),
            default=self._now,
        )
        valid_until = min(
            valid_until,
            ranking.valid_until.astimezone(UTC),
            self._now + timedelta(hours=1),
        )
        if valid_until <= self._now:
            reason = "expired"
            return (
                None,
                reason,
                self._review_outcome(
                    item,
                    reason=reason,
                    observation=shadow_observation,
                ),
            )
        external = ExternalEvidence(
            source=f"model_router:{verdict.model_id}",
            symbol=item.candidate.symbol,
            market=cast(Any, item.candidate.market),
            action=action,
            confidence=confidence,
            as_of=self._now,
            valid_until=valid_until,
            rationale=tuple(str(value) for value in verdict.rationale_tags)
            + (f"AI risk={verdict.risk}",),
            evidence=(
                {
                    "title": "AI candidate review",
                    "source": f"model_router:{verdict.model_id}",
                    "kind": "ai_analysis",
                    "tier": verdict.tier_used,
                    "confidence": str(confidence),
                    "risk": str(verdict.risk),
                    "bullishScore": int(verdict.bullish_score),
                    "bearishScore": int(verdict.bearish_score),
                    "eventCount": len(events),
                },
                ranking.as_evidence(),
            ),
        )
        directional_score = Decimal(
            verdict.bullish_score if action == Action.BUY else verdict.bearish_score
        ) / Decimal("100")
        event_score = directional_score if events else Decimal("0")
        score = (
            ranking.total_score * Decimal("0.40")
            + abs(item.ensemble.score) * Decimal("0.35")
            + confidence * Decimal("0.15")
            + event_score * Decimal("0.10")
        ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
        return (
            ReviewedCandidate(
                evaluated=item,
                external=external,
                events=events,
                event_score=event_score,
                score=score,
                ai_shadow=shadow_observation,
            ),
            None,
            self._review_outcome(
                item,
                reason="accepted",
                observation=shadow_observation,
            ),
        )

    def _review_outcome(
        self,
        item: EvaluatedCandidate,
        *,
        reason: str,
        observation: AiShadowObservation | None = None,
    ) -> AIReviewOutcome:
        return AIReviewOutcome(
            symbol=item.candidate.symbol,
            market=item.candidate.ranker_market,
            strategy_action=item.ensemble.action.value,
            ai_action=observation.action if observation is not None else None,
            confidence=observation.confidence if observation is not None else None,
            reason=reason,
            observed_at=(
                observation.observed_at
                if observation is not None
                else _timestamp_text(self._now) or self._now.isoformat()
            ),
            provider=observation.provider if observation is not None else None,
            tier=observation.tier if observation is not None else None,
            model_id=observation.model_id if observation is not None else None,
            rationale_tags=(
                observation.rationale_tags if observation is not None else ()
            ),
        )

    def _collection_policy(self) -> dict[str, object]:
        return _collection_policy_payload(self._ranker_config)

    async def _persist_recommendation(
        self,
        owner_user_id: int,
        item: ReviewedCandidate,
        regime: RegimeAssessment,
        *,
        position: int,
        total: int,
        snapshot,
    ) -> AIRecommendation:
        candidate = item.evaluated.candidate
        reference_price_text = _level_text(item.evaluated.ensemble.agreeing, "entry")
        ranking = item.evaluated.factor_ranking
        strategy_stop_text = _level_text(
            item.evaluated.ensemble.agreeing,
            "stop",
        )
        if reference_price_text is None:
            raise ValueError("ensemble has no reference price")
        reference_price = Decimal(reference_price_text)
        plan = await self._policy.portfolio_plan(
            self._db,
            owner_user_id,
            action=item.external.action.value,
            market=candidate.market,
            symbol=candidate.symbol,
            reference_price=reference_price,
            limits=snapshot.limits,
            usage=snapshot.usage,
            strategy_stop=(
                Decimal(strategy_stop_text) if strategy_stop_text is not None else None
            ),
            strategy_atr=ranking.atr_14 if ranking is not None else None,
            price_as_of=ranking.data_as_of if ranking is not None else None,
            evaluated_at=self._now,
            regime=regime.regime,
            average_volume=_liquidity_cap(
                candidate.volume,
                ranking.average_volume_20 if ranking is not None else None,
            ),
            average_turnover=_liquidity_cap(
                candidate.turnover,
                ranking.average_turnover_20 if ranking is not None else None,
            ),
        )
        hard_risk = await self._policy.evaluate_hard_risk(
            self._db,
            owner_user_id,
            action=item.external.action.value,
            market=candidate.market,
            symbol=candidate.symbol,
            quantity=plan.target_quantity,
            reference_price=reference_price,
            ai_confidence=item.external.confidence,
            now=self._now,
        )
        persistence = AIRecommendationService(
            self._db,
            clock=lambda: self._now,
            cycle_trace_id=self._cycle_trace_id,
        )
        row = await RecommendationProducer(
            owner_user_id=str(owner_user_id),
            persistence=persistence,
        ).produce(
            symbol=candidate.symbol,
            market=candidate.market,
            name=candidate.name,
            strategy_results=item.evaluated.strategy_results,
            external_evidence=item.external,
            suggested_quantity=plan.target_quantity,
            now=self._now,
            regime=regime.regime.value,
            regime_detail=regime.detail,
            strategy_weights=regime.weights,
            event_evidence=item.events,
            ranking={
                "score": str(item.score),
                "position": position,
                "total": total,
                "note": (
                    f"{candidate.source} 후보 {total}개 중 dynamic ensemble, "
                    f"AI, news/DART event score {item.event_score}로 "
                    "순위화했습니다."
                ),
            },
            portfolio=plan.as_evidence(),
            hard_risk=hard_risk.as_evidence(),
            strategy_promotion={
                "strategyKey": DEFAULT_PAPER_STRATEGY_KEY,
                "version": DEFAULT_PAPER_STRATEGY_VERSION,
                "artifactFingerprint": self._strategy_artifact_fingerprint,
            },
            ai_shadow_evidence=item.ai_shadow.as_selected_evidence(),
        )
        return cast(AIRecommendation, row)

    async def _event_evidence(
        self,
        candidate: TradingCandidate,
    ) -> tuple[Mapping[str, object], ...]:
        market = "kr" if candidate.market == "KRX" else "us"
        news, _excluded = await load_symbol_news(
            self._db,
            candidate.symbol,
            market,
            3,
        )
        evidence: list[Mapping[str, object]] = [
            {
                "kind": "NEWS",
                "title": item.title,
                "source": item.source,
                "publishedAt": _timestamp_text(item.published_at),
                "summary": item.summary,
                "url": item.url,
            }
            for item in news
        ]
        disclosures = (
            await self._db.scalars(
                select(NewsArticle)
                .where(
                    NewsArticle.market == market,
                    NewsArticle.stock_symbol == candidate.symbol,
                    NewsArticle.feed_source.in_(DISCLOSURE_FEED_SOURCES),
                )
                .order_by(
                    NewsArticle.article_published_at.desc().nullslast(),
                    NewsArticle.id.desc(),
                )
                .limit(3)
            )
        ).all()
        evidence.extend(
            {
                "kind": "DISCLOSURE",
                "title": item.title,
                "source": item.source,
                "publishedAt": _timestamp_text(item.article_published_at),
                "summary": item.summary,
                "url": item.url,
            }
            for item in disclosures
        )
        return tuple(evidence)

    async def _cooldown_active(self, owner_user_id: int) -> bool:
        count = await self._db.scalar(
            select(func.count())
            .select_from(AIRecommendation)
            .where(
                AIRecommendation.owner_user_id == owner_user_id,
                AIRecommendation.source == "kasset-automation",
                AIRecommendation.created_at >= self._now - _OWNER_COOLDOWN,
            )
        )
        return bool(count)


async def run_ai_recommendation_cycle_once(
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Operator/task entrypoint; generates review rows but never calls a broker.

    TaskIQ는 반환값을 로그로 남기지 않는다. 그래서 이 함수가 직접 cycle의 시작·
    owner별 결과·종료를 남긴다. 그러지 않으면 "스케줄러가 태스크를 보냈다"는
    사실만 남고 왜 추천이 하나도 나오지 않았는지 운영 로그로 알 수 없다.
    """

    current = _aware_utc(now or datetime.now(UTC))
    if not settings.KASSET_MARKET_EVENTS_ENABLED:
        logger.info(
            "kasset AI recommendation cycle disabled: "
            "KASSET_MARKET_EVENTS_ENABLED=false"
        )
        return {"enabled": False, "owners": [], "candidateCount": 0}
    open_markets = _open_regular_markets(now=current)
    live_candidates_cache: dict[str, tuple[TradingCandidate, ...]] = {}
    snapshot = None
    async with _session() as db:
        owner_ids = list(
            (
                await db.scalars(
                    select(User.id)
                    .where(User.role == UserRole.trader, User.is_active.is_(True))
                    .order_by(User.id)
                )
            ).all()
        )
        if open_markets:
            # cycle 시작 시 정책을 한 번만 읽는다. 이 cycle 안에서는 route가
            # 섞이지 않고, 다음 cycle부터 새 정책이 재시작 없이 적용된다.
            from app.services.ai_runtime_config import get_ai_runtime_snapshot

            snapshot = await get_ai_runtime_snapshot(db)

    ai_router: OpenAiModelRouter | None = None
    if snapshot is not None:
        from app.extensions.kasset.ai.factory import build_model_router

        # 같은 snapshot에서 유효 가용성을 계산한다. catalog는 설정만 읽는 순수
        # 함수이므로 DB를 다시 건드리지 않는다(cycle당 정책 조회는 여전히 1회).
        availability = build_ai_availability(snapshot, build_ai_route_catalog())

        # 이 cycle의 첫 분석은 candidate_review -> terra다. 다른 review lane만
        # 살아 있으면 router 객체는 만들 수 있어도 실제 후보를 처리하지 못한다.
        if availability.lane_usable(AiLane.REVIEW_TERRA):
            try:
                ai_router = build_model_router(snapshot=snapshot)
            except AiProviderUnavailable:
                ai_router = None
        ai_unavailable_reason = (
            None
            if ai_router is not None
            else availability.unavailable_reason or _AI_REVIEW_UNAVAILABLE
        )
        ai_policy_source: str | None = availability.source
        ai_usable_lanes = sorted(lane.value for lane in availability.usable_lanes)
    else:
        # 장이 닫힌 cycle은 정책/provider/router를 건드리지 않는다.
        ai_unavailable_reason = _NO_REGULAR_MARKET_OPEN
        ai_policy_source = None
        ai_usable_lanes = []

    logger.info(
        "kasset AI recommendation cycle start: owners=%d open_markets=%s "
        "ai_available=%s ai_policy_source=%s ai_usable_lanes=%s "
        "ai_unavailable_reason=%s",
        len(owner_ids),
        sorted(open_markets),
        ai_router is not None,
        ai_policy_source,
        ai_usable_lanes,
        ai_unavailable_reason,
    )

    owners: list[dict[str, object]] = []
    total_candidates = 0
    total_recommendations = 0
    for raw_owner_id in owner_ids:
        owner_id = int(raw_owner_id)
        # 후보를 한 건도 읽기 전에 이 owner cycle의 추적 id를 확정한다. owner
        # cycle이 예외로 끝나도 원장 행이 같은 값을 갖는다.
        cycle_trace_id = new_cycle_trace_id()
        if not open_markets:
            result = _regular_market_skip_result(
                owner_user_id=owner_id,
                cycle_trace_id=cycle_trace_id,
            )
        else:
            try:
                async with _session() as db:
                    result = await AIRecommendationVerticalSlice(
                        db,
                        ai_router,
                        now=current,
                        live_candidates_cache=live_candidates_cache,
                        allowed_markets=open_markets,
                        cycle_trace_id=cycle_trace_id,
                    ).run_owner(owner_id)
            except Exception as exc:
                # 스택 없이 errorClass만 담아 돌려주면 TaskIQ가 그 dict를 버리는
                # 순간 원인이 사라진다. 원장에는 요약을, 로그에는 스택을 남긴다.
                logger.exception(
                    "kasset AI recommendation cycle owner failed: owner_user_id=%s",
                    owner_id,
                )
                result = {
                    "ownerUserId": owner_id,
                    "cycleTraceId": cycle_trace_id,
                    "candidateCount": 0,
                    "recommendationIds": [],
                    "skipped": "owner_cycle_failed",
                    "errorClass": type(exc).__name__,
                }
        result.setdefault(
            "collectionPolicy",
            _collection_policy_payload(DEFAULT_CANDIDATE_RANKER_CONFIG),
        )
        result.setdefault("cycleTraceId", cycle_trace_id)
        try:
            await record_automation_cycle_event(
                owner_user_id=owner_id,
                observed_at=current,
                finished_at=datetime.now(UTC),
                result=result,
            )
        except Exception:
            logger.exception(
                "kasset AI recommendation cycle audit write failed: owner_user_id=%s",
                owner_id,
            )
        recommendation_ids = result.get("recommendationIds")
        produced = (
            len(recommendation_ids) if isinstance(recommendation_ids, list) else 0
        )
        total_candidates += int(result.get("candidateCount", 0))
        total_recommendations += produced
        logger.info(
            "kasset AI recommendation cycle owner=%s trace=%s skipped=%s "
            "candidates=%s "
            "markets=%s sources=%s ranked=%s actionable=%s reviewed=%s "
            "review_rejections=%s recommendations=%d",
            owner_id,
            cycle_trace_id,
            result.get("skipped"),
            result.get("candidateCount", 0),
            result.get("candidateMarkets"),
            result.get("candidateSources"),
            result.get("rankedCount"),
            result.get("strategyActionableCount"),
            result.get("aiReviewedCount"),
            result.get("aiReviewRejections"),
            produced,
        )
        owners.append(result)

    logger.info(
        "kasset AI recommendation cycle done: owners=%d candidates=%d "
        "recommendations=%d",
        len(owners),
        total_candidates,
        total_recommendations,
    )
    return {
        "enabled": True,
        "owners": owners,
        "candidateCount": total_candidates,
        "recommendationCount": total_recommendations,
        "aiAvailable": ai_router is not None,
        "aiPolicySource": ai_policy_source,
        "aiUnavailableReason": ai_unavailable_reason,
        "openMarkets": sorted(open_markets),
    }


def _candidate_metadata(candidate: TradingCandidate) -> CandidateMetadata:
    return CandidateMetadata(
        symbol=candidate.symbol,
        market=candidate.ranker_market,
        sources=tuple(candidate.source.split("|")),
        name=candidate.name,
        screener_turnover=candidate.turnover,
        screener_volume=candidate.volume,
        is_held=candidate.is_held,
        is_watchlisted=candidate.is_watchlisted,
        eligible_for_new_buy=candidate.eligible_for_new_buy,
    )


def _merge_trading_candidate(
    current: TradingCandidate | None,
    incoming: TradingCandidate,
) -> TradingCandidate:
    if current is None:
        return incoming
    if current.ranker_key != incoming.ranker_key:
        raise ValueError("candidate merge requires identical market and symbol")
    sources = tuple(
        dict.fromkeys(
            (
                *current.source.split("|"),
                *incoming.source.split("|"),
            )
        )
    )
    return replace(
        current,
        name=current.name or incoming.name,
        source="|".join(sources),
        turnover=(
            incoming.turnover if incoming.turnover is not None else current.turnover
        ),
        volume=(incoming.volume if incoming.volume is not None else current.volume),
        is_held=current.is_held or incoming.is_held,
        is_watchlisted=current.is_watchlisted or incoming.is_watchlisted,
        eligible_for_new_buy=(
            current.eligible_for_new_buy or incoming.eligible_for_new_buy
        ),
    )


def _liquidity_cap(
    screener_value: Decimal | None,
    historical_average: Decimal | None,
) -> Decimal | None:
    values = tuple(
        value
        for value in (screener_value, historical_average)
        if value is not None and value.is_finite() and value > 0
    )
    return min(values) if values else None


def _price_bars(rows: Sequence[Any]) -> tuple[PriceBar, ...]:
    bars: list[PriceBar] = []
    for row in rows:
        timestamp = row.time_utc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        else:
            timestamp = timestamp.astimezone(UTC)
        bars.append(
            PriceBar(
                timestamp=timestamp,
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=Decimal(str(row.volume)),
            )
        )
    return tuple(bars)


def _level_text(results: Sequence[StrategyResult], field_name: str) -> str | None:
    values = sorted(
        value
        for result in results
        if (value := getattr(result, field_name, None)) is not None
        and value.is_finite()
        and value > 0
    )
    return str(values[len(values) // 2]) if values else None


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC).replace(microsecond=0)


def _session() -> AbstractAsyncContextManager[AsyncSession]:
    return cast(
        AbstractAsyncContextManager[AsyncSession],
        cast(object, AsyncSessionLocal()),
    )


__all__ = [
    "AIRecommendationVerticalSlice",
    "EvaluatedCandidate",
    "ReviewedCandidate",
    "TradingCandidate",
    "run_ai_recommendation_cycle_once",
]
