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
    StrategyFamily,
    StrategyResult,
)
from app.extensions.kasset.automation.daily_setup import (
    DEFAULT_DAILY_SETUP_CONFIG,
    DailySetup,
    DailySetupConfig,
    daily_setup_policy_evidence,
    evaluate_daily_setup,
    select_daily_setups,
)
from app.extensions.kasset.automation.decision_evidence import (
    AI_COHORT_CONFIDENCE_FLOOR,
    COHORT_TECHNICAL_AI,
    COHORT_TECHNICAL_AI_NEWS,
    COHORT_TECHNICAL_ONLY,
    LIVE_COHORT,
    AiReviewEvidence,
    AiReviewStatus,
    NewsShadowEvidence,
    ai_review_from_observation,
    build_decision_cohorts,
    build_news_shadow,
    unknown_news_shadow,
)
from app.extensions.kasset.automation.intraday_data import (
    CompletedIntradayBars,
    IntradayBarsUnavailable,
    load_completed_session_bars,
    load_index_session_bars,
)
from app.extensions.kasset.automation.intraday_triggers import (
    DEFAULT_INTRADAY_TRIGGER_POLICY,
    RELATIVE_VOLUME_5M,
    RELATIVE_VOLUME_20M,
    IntradayTriggerDecision,
    IntradayTriggerPolicy,
    TriggerResult,
    decide_intraday_triggers,
    intraday_relative_strength,
    opening_range_breakout,
    relative_volume,
    session_vwap_reclaim,
)
from app.extensions.kasset.automation.market_session import (
    RegularSession,
    completed_bar_cutoff,
    current_regular_session,
)
from app.extensions.kasset.automation.policy import (
    AITradingPolicyService,
    AITradingSnapshot,
    PortfolioPlan,
)
from app.extensions.kasset.automation.position_manager_service import (
    PaperPositionManagerService,
)
from app.extensions.kasset.automation.producer import (
    RecommendationProducer,
    WeightedEnsembleDecision,
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

#: 순위 상위 검토 창을 ``strategy_review_limit``의 몇 배까지 열어둘지. AI 앞단
#: 에서 결정론적으로 걸린 행을 다음 순위 행으로 메우려면 창이 상한보다 넓어야
#: 한다. AI로 보내는 최대 건수는 여전히 ``strategy_review_limit``이다.
_REVIEW_WINDOW_MULTIPLIER = 2

#: 앙상블 합의가 진입가를 내놓지 못해 사이징 자체가 불가능한 행.
_PRESIZING_NO_REFERENCE_PRICE = "presizing_reference_price_unavailable"
_PRESIZING_ZERO_QUANTITY = "presizing_zero_quantity"

#: 검토 lane에 쓸 수 있는 route가 없어 cycle이 AI 없이 도는 상태의 사유.
#: 정책 자체는 정상이므로 ``AiAvailability``의 사유 코드와 층을 구분한다.
_AI_REVIEW_UNAVAILABLE = "review_routes_unavailable"
_NO_REGULAR_MARKET_OPEN = "no_regular_market_open"
_NO_CONFIGURED_REGULAR_MARKET_OPEN = "no_configured_regular_market_open"

#: 장중 방아쇠가 걸리지 않아 주문 후보에서 빠진 행.
_NO_INTRADAY_TRIGGER = "intraday_trigger_not_satisfied"
#: Daily Setup 자체가 적합하지 않아 장중 단계로 가지 않은 행.
_NO_DAILY_SETUP = "daily_setup_not_qualified"
#: 장중 bar를 동시에 몇 심볼까지 읽을지. 후보 상한이 20이므로 이 폭으로
#: 정규장 한 tick 안에 적재가 끝난다.
_INTRADAY_FETCH_CONCURRENCY = 6
#: 뉴스 수집 경로가 살아 있다고 인정하는 최신성 창. 이 안에 기사가 하나도
#: 없으면 "이 종목에 뉴스가 없다"고 말할 근거가 없으므로 UNKNOWN이 된다.
_NEWS_HEALTH_WINDOW = timedelta(hours=24)
#: 상대거래량 임계값. 완료 bar 기준 평균의 몇 배부터 확장으로 볼지.
_RELATIVE_VOLUME_THRESHOLD = Decimal("1.5")
#: 5분·20분 창을 5분 bucket 개수로 표현한 값과 그 비교 기준선 길이.
_RELATIVE_VOLUME_WINDOWS: tuple[tuple[str, int, int], ...] = (
    (RELATIVE_VOLUME_5M, 1, 12),
    (RELATIVE_VOLUME_20M, 4, 12),
)
#: 개장 구간 돌파(ORB)의 개장 구간 길이.
_OPENING_RANGE = timedelta(minutes=15)
#: 장중 지수 대비 상대강도 임계값.
_INTRADAY_RELATIVE_STRENGTH_THRESHOLD = Decimal("0")
#: ranker가 실제로 쓴 벤치마크 식별자를 담은 근거 코드.
_RANKER_BENCHMARK_CODE = "relative_strength_benchmark"


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
    """완료 일봉 Daily Setup까지 통과한 한 후보."""

    candidate: TradingCandidate
    strategy_results: tuple[StrategyResult, ...]
    ensemble: WeightedEnsembleDecision
    setup: DailySetup
    factor_ranking: CandidateRankResult | None = None
    regime: RegimeAssessment | None = None


@dataclass(frozen=True, slots=True)
class _PreAiSizing:
    """One candidate's sizing, computed before AI and reused at persistence.

    같은 후보를 AI 앞뒤로 두 번 사이징하면 근거가 갈라질 수 있다. 앞단에서
    계산한 값을 그대로 들고 다녀 저장 시점 수량과 근거가 어긋나지 않게 한다.
    """

    reference_price: Decimal
    plan: PortfolioPlan


@dataclass(frozen=True, slots=True)
class _PreAiExclusion:
    """AI 슬롯을 쓰지 않고 결정론적으로 걸러낸 후보의 사유와 근거."""

    reason: str
    evidence: dict[str, object]


@dataclass(frozen=True, slots=True)
class AdmittedCandidate:
    """기술 판정과 장중 방아쇠를 모두 통과한 주문 후보.

    ``ai_review``와 ``news_shadow``는 기록용이다. 둘 중 무엇이 실패해도 이
    행이 후보에서 빠지지 않는다.
    """

    evaluated: EvaluatedCandidate
    trigger_decision: IntradayTriggerDecision
    decision: ExternalEvidence
    ai_review: AiReviewEvidence
    news_shadow: NewsShadowEvidence
    events: tuple[Mapping[str, object], ...]
    score: Decimal
    ai_shadow: AiShadowObservation | None = None


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


async def _load_live_us_candidates(
    db: AsyncSession,
    *,
    limit: int = DEFAULT_CANDIDATE_RANKER_CONFIG.candidate_limit,
) -> tuple[TradingCandidate, ...]:
    from app.services.market_valuation_snapshots.us_provider import (
        TvScreenerUsValuationProvider,
    )
    from app.services.us_symbol_universe_service import get_us_common_stock_flags

    fetch_limit = max(limit * 3, limit)
    rows = await TvScreenerUsValuationProvider(timeout=30).fetch_rows(
        limit=fetch_limit,
        sort_by_market_cap=True,
    )
    normalized: list[tuple[str, dict[str, object]]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").rsplit(":", 1)[-1].strip().upper()
        if symbol:
            normalized.append((symbol, row))
    flags = await get_us_common_stock_flags(
        [symbol for symbol, _row in normalized],
        db=db,
    )
    candidates: list[TradingCandidate] = []
    seen: set[str] = set()
    for symbol, _row in normalized:
        if symbol in seen or flags.get(symbol) is not True:
            continue
        seen.add(symbol)
        candidates.append(
            TradingCandidate(
                symbol=symbol,
                market="US",
                name=None,
                source="tvscreener_us",
            )
        )
        if len(candidates) >= limit:
            break
    return tuple(candidates)


class AIRecommendationVerticalSlice:
    """Run one owner through completed daily setups, intraday triggers, and PAPER.

    관문은 둘뿐이다: 기술 판정(완료 일봉 Daily Setup + 완료 장중 Trigger)과
    Hard Risk. AI 검토와 뉴스/공시는 근거로만 남고 후보를 늘리거나 줄이지 않는다.
    """

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
        daily_setup_config: DailySetupConfig = DEFAULT_DAILY_SETUP_CONFIG,
        trigger_policy: IntradayTriggerPolicy = DEFAULT_INTRADAY_TRIGGER_POLICY,
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
        self._review_window_limit = min(
            ranker_config.strategy_review_limit * _REVIEW_WINDOW_MULTIPLIER,
            ranker_config.candidate_limit,
        )
        self._shadow_setup_config = shadow_setup_config
        self._daily_setup_config = daily_setup_config
        self._trigger_policy = trigger_policy
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
        candidate_by_key = {candidate.ranker_key: candidate for candidate in candidates}
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
        # 1단계: 완료 일봉만으로 Daily Setup을 판정하고 상한까지 고른다.
        # 장중 partial bar는 setup 계산에 절대 섞이지 않는다.
        completed_cutoff_by_market = {
            market: completed_bar_cutoff(market, self._now)
            for market in sorted(allowed_markets)
        }
        setups: list[DailySetup] = []
        for result in ranking.ranked:
            candidate = candidate_by_key.get(result.key)
            if candidate is None:
                continue
            candidate_regime = regimes.get(candidate.ranker_market)
            if candidate_regime is None:
                continue
            setups.append(
                evaluate_daily_setup(
                    result,
                    bars_by_candidate.get(candidate.ranker_key, ()),
                    market=candidate.market,
                    regime=candidate_regime,
                    strategies=STRATEGIES,
                    as_of=self._now,
                    completed_cutoff=completed_cutoff_by_market.get(
                        candidate.ranker_market
                    ),
                    config=self._daily_setup_config,
                )
            )
        selected_setups = select_daily_setups(setups, config=self._daily_setup_config)
        setup_statuses: Counter[str] = Counter(item.status.value for item in setups)
        setup_rejections: Counter[str] = Counter(
            item.rejection_reason
            for item in setups
            if item.rejection_reason is not None and not item.qualified
        )

        evaluated: list[EvaluatedCandidate] = []
        actionable: list[EvaluatedCandidate] = []
        sizing_by_key: dict[CandidateKey, _PreAiSizing] = {}
        pre_ai_exclusions: Counter[str] = Counter()
        pre_ai_exclusion_evidence: list[dict[str, object]] = []
        admitted: list[AdmittedCandidate] = []
        review_outcomes: list[AIReviewOutcome] = []
        ai_failures = 0
        review_rejections: Counter[str] = Counter()
        trigger_evidence: list[dict[str, object]] = []
        trigger_statuses: Counter[str] = Counter()
        # 어느 관문이 몇 건을 죽였는지 운영 로그로 분리하기 위한 funnel 계측.
        trigger_failures: Counter[str] = Counter()

        # 2단계: 선택된 setup만 장중 완료 bar를 읽는다. 세션·신선도 검증은
        # intraday_data가 fail-closed로 하고, 여기서는 그 결과만 정책에 넣는다.
        session_by_market = {
            market: current_regular_session(market, self._now)
            for market in sorted(allowed_markets)
        }
        intraday_by_key = await self._load_intraday_bars(selected_setups)
        index_bars_by_symbol = await self._load_index_intraday_bars(
            selected_setups,
            ranking_by_key={item.key: item for item in ranking.ranked},
            session_by_market=session_by_market,
        )
        news_health_by_market = await self._news_source_health(allowed_markets)

        for setup in selected_setups:
            candidate = candidate_by_key.get(_setup_key(setup))
            if candidate is None:
                continue
            candidate_regime = regimes.get(candidate.ranker_market)
            ranked_result = next(
                (item for item in ranking.ranked if item.key == candidate.ranker_key),
                None,
            )
            if candidate_regime is None or ranked_result is None:
                continue
            item = EvaluatedCandidate(
                candidate=candidate,
                strategy_results=setup.strategy_results,
                ensemble=cast(WeightedEnsembleDecision, setup.ensemble),
                setup=setup,
                factor_ranking=ranked_result,
                regime=candidate_regime,
            )
            evaluated.append(item)
            if setup.direction not in {Action.BUY, Action.SELL}:
                continue
            actionable.append(item)
            trigger_decision = self._decide_triggers(
                item,
                intraday=intraday_by_key.get(candidate.ranker_key),
                index_bars=index_bars_by_symbol.get(_benchmark_symbol(ranked_result)),
            )
            trigger_statuses[trigger_decision.status.value] += 1
            trigger_evidence.append(trigger_decision.as_evidence())
            if not trigger_decision.triggered:
                for failure_code in (
                    trigger_decision.blocked_reason or "unspecified"
                ).split(","):
                    normalized_failure = failure_code.strip()
                    if normalized_failure:
                        trigger_failures[normalized_failure] += 1
                review_rejections[_NO_INTRADAY_TRIGGER] += 1
                pre_ai_exclusion_evidence.append(
                    _trigger_exclusion_evidence(item, trigger_decision)
                )
                continue
            sizing = await self._pre_ai_sizing(
                owner_user_id,
                item,
                candidate_regime,
                snapshot=snapshot,
            )
            if isinstance(sizing, _PreAiExclusion):
                pre_ai_exclusions[sizing.reason] += 1
                pre_ai_exclusion_evidence.append(sizing.evidence)
                continue
            # 3단계: 기술 판정이 이미 후보를 확정했다. AI 검토와 뉴스 수집은
            # 설명과 보조순위를 위한 관측이며 실패해도 이 행은 남는다.
            review = await self._review_candidate(owner_user_id, item)
            if review.ai_review.status is AiReviewStatus.UNAVAILABLE:
                ai_failures += 1
            review_rejections[f"ai_{review.ai_review.status.value}"] += 1
            review_outcomes.append(
                self._review_outcome(
                    item,
                    reason="admitted",
                    observation=review.ai_shadow,
                )
            )
            news_shadow = await self._news_shadow(
                candidate,
                health_proven=news_health_by_market.get(candidate.ranker_market, False),
            )
            sizing_by_key[candidate.ranker_key] = sizing
            admitted.append(
                _admitted_candidate(
                    item,
                    trigger_decision=trigger_decision,
                    review=review,
                    news_shadow=news_shadow,
                    now=self._now,
                )
            )

        reviewed = admitted

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
            sizing = sizing_by_key.get(item.evaluated.candidate.ranker_key)
            if candidate_regime is None or sizing is None:
                continue
            row = await self._persist_recommendation(
                owner_user_id,
                item,
                candidate_regime,
                position=position,
                total=total,
                sizing=sizing,
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
            for result in ranking.ranked[: self._review_window_limit]
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
            "strategyEvaluationWindow": self._review_window_limit,
            "strategyReviewCapReached": False,
            "preAiExclusions": dict(sorted(pre_ai_exclusions.items())),
            "dailySetupPolicy": daily_setup_policy_evidence(self._daily_setup_config),
            "dailySetupStatuses": dict(sorted(setup_statuses.items())),
            "dailySetupRejections": dict(sorted(setup_rejections.items())),
            "dailySetupSelectedCount": len(selected_setups),
            "dailySetups": [item.as_evidence() for item in selected_setups],
            "intradayTriggerPolicy": self._trigger_policy.as_evidence(),
            "intradayTriggerStatuses": dict(sorted(trigger_statuses.items())),
            "intradayTriggerFailures": dict(sorted(trigger_failures.items())),
            "intradayTriggers": trigger_evidence,
            "decisionCohortPolicy": {
                "liveCohort": LIVE_COHORT,
                "cohorts": [
                    COHORT_TECHNICAL_ONLY,
                    COHORT_TECHNICAL_AI,
                    COHORT_TECHNICAL_AI_NEWS,
                ],
                "gating": ["daily_setup", "intraday_triggers", "hard_risk"],
                "nonGating": ["ai_review", "news_shadow"],
            },
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
            # AI 앞단 제외 근거를 먼저 담는다. 감사 원장은 이 목록을 앞에서
            # 자르므로, 새로 진단해야 하는 행이 잘려 나가지 않게 한다.
            "candidateExclusions": [
                *pre_ai_exclusion_evidence,
                *(excluded.as_evidence() for excluded in ranking.excluded),
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
        if not selected_setups:
            result["skipped"] = _NO_DAILY_SETUP
        elif not actionable:
            result["skipped"] = "no_breakout_family_direction"
        elif not admitted and pre_ai_exclusions:
            result["skipped"] = "no_affordable_actionable_candidate"
        elif not admitted:
            result["skipped"] = _NO_INTRADAY_TRIGGER
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

        us_count = sum(
            candidate.ranker_market == "US" and candidate.eligible_for_new_buy
            for candidate in ordered.values()
        )
        if (
            "US" in requested_markets
            and us_count < self._ranker_config.minimum_candidate_target
        ):
            live = self._live_candidates_cache.get("us")
            if live is None:
                live = await _load_live_us_candidates(
                    self._db,
                    limit=self._ranker_config.candidate_limit,
                )
                self._live_candidates_cache["us"] = live
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

    async def _load_intraday_bars(
        self,
        setups: Sequence[DailySetup],
    ) -> dict[CandidateKey, CompletedIntradayBars | IntradayBarsUnavailable]:
        """선택된 setup만 공용 Toss-first 경로로 완료 장중 bar를 읽는다."""

        if not setups:
            return {}
        semaphore = asyncio.Semaphore(_INTRADAY_FETCH_CONCURRENCY)

        async def load(setup: DailySetup):
            async with semaphore:
                return _setup_key(setup), await load_completed_session_bars(
                    symbol=setup.symbol,
                    market=setup.market,
                    as_of=self._now,
                )

        return dict(await asyncio.gather(*(load(setup) for setup in setups)))

    async def _load_index_intraday_bars(
        self,
        setups: Sequence[DailySetup],
        *,
        ranking_by_key: Mapping[CandidateKey, CandidateRankResult],
        session_by_market: Mapping[str, RegularSession | None],
    ) -> dict[str, CompletedIntradayBars | IntradayBarsUnavailable]:
        """후보가 실제로 쓴 벤치마크 지수의 완료 분봉을 지수별로 한 번만 읽는다.

        지수 분봉이 없으면 그 사실만 남는다. 일봉으로 대체하지 않는다.
        """

        wanted: dict[str, Literal["KRX", "US"]] = {}
        for setup in setups:
            ranked = ranking_by_key.get(_setup_key(setup))
            if ranked is None:
                continue
            symbol = _benchmark_symbol(ranked)
            if symbol is not None:
                wanted[symbol] = setup.market
        if not wanted:
            return {}

        async def load(index_symbol: str, market: Literal["KRX", "US"]):
            return index_symbol, await load_index_session_bars(
                index_symbol=index_symbol,
                market=market,
                as_of=self._now,
                session=session_by_market.get("KR" if market == "KRX" else "US"),
            )

        return dict(
            await asyncio.gather(
                *(load(symbol, market) for symbol, market in sorted(wanted.items()))
            )
        )

    def _decide_triggers(
        self,
        item: EvaluatedCandidate,
        *,
        intraday: CompletedIntradayBars | IntradayBarsUnavailable | None,
        index_bars: CompletedIntradayBars | IntradayBarsUnavailable | None,
    ) -> IntradayTriggerDecision:
        """완료 장중 bar로 명시적 trigger 정책을 판정한다."""

        direction = item.setup.direction
        symbol = item.candidate.symbol
        market = item.candidate.market
        if intraday is None or isinstance(intraday, IntradayBarsUnavailable):
            # stale·부분 bar·세션 밖은 개별 trigger가 아니라 판정 전체를 막는다.
            return decide_intraday_triggers(
                (),
                symbol=symbol,
                market=market,
                direction=direction,
                evaluated_at=self._now,
                policy=self._trigger_policy,
                blocked_reason=(
                    "intraday_bars_not_loaded"
                    if intraday is None
                    else intraday.blocked_reason
                ),
            )

        triggers: list[TriggerResult] = [
            opening_range_breakout(
                intraday.bars,
                direction=direction,
                session_open=intraday.session.opens_at,
                opening_range=_OPENING_RANGE,
                bar_interval=intraday.bar_interval,
                source=intraday.source,
            ),
            session_vwap_reclaim(
                intraday.bars,
                direction=direction,
                bar_interval=intraday.bar_interval,
                source=intraday.source,
            ),
        ]
        triggers.extend(
            relative_volume(
                intraday.bars,
                code=code,
                window_bars=window_bars,
                baseline_bars=baseline_bars,
                threshold=_RELATIVE_VOLUME_THRESHOLD,
                bar_interval=intraday.bar_interval,
                source=intraday.source,
            )
            for code, window_bars, baseline_bars in _RELATIVE_VOLUME_WINDOWS
        )
        usable_index = (
            index_bars if isinstance(index_bars, CompletedIntradayBars) else None
        )
        triggers.append(
            intraday_relative_strength(
                intraday.bars,
                usable_index.bars if usable_index is not None else None,
                direction=direction,
                threshold=_INTRADAY_RELATIVE_STRENGTH_THRESHOLD,
                bar_interval=intraday.bar_interval,
                source=intraday.source,
                index_source=(
                    usable_index.source
                    if usable_index is not None
                    else (index_bars.symbol if index_bars is not None else None)
                ),
                unavailable_reason=(
                    index_bars.blocked_reason
                    if isinstance(index_bars, IntradayBarsUnavailable)
                    else None
                ),
            )
        )
        return decide_intraday_triggers(
            triggers,
            symbol=symbol,
            market=market,
            direction=direction,
            evaluated_at=self._now,
            policy=self._trigger_policy,
        )

    async def _pre_ai_sizing(
        self,
        owner_user_id: int,
        item: EvaluatedCandidate,
        regime: RegimeAssessment,
        *,
        snapshot: AITradingSnapshot,
    ) -> _PreAiSizing | _PreAiExclusion:
        """Size the candidate before AI so unaffordable rows cost no AI slot.

        Hard Risk와 AI 임계값은 그대로다. 여기서 걸리는 행은 어차피 저장
        단계에서 수량 0으로 버려질 행이므로, AI 검토 예산만 아낀다.
        """

        candidate = item.candidate
        reference_price_text = _level_text(item.ensemble.agreeing, "entry")
        if reference_price_text is None:
            return _PreAiExclusion(
                reason=_PRESIZING_NO_REFERENCE_PRICE,
                evidence=_presizing_exclusion_evidence(
                    item,
                    reason=_PRESIZING_NO_REFERENCE_PRICE,
                    plan=None,
                ),
            )
        reference_price = Decimal(reference_price_text)
        strategy_stop_text = _level_text(item.ensemble.agreeing, "stop")
        ranking = item.factor_ranking
        plan = await self._policy.portfolio_plan(
            self._db,
            owner_user_id,
            action=item.ensemble.action.value,
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
        quantity = plan.target_quantity
        sizing = plan.position_sizing
        if (
            not quantity.is_finite()
            or quantity <= 0
            or (sizing is not None and not sizing.actionable)
        ):
            reason = _presizing_exclusion_reason(plan)
            return _PreAiExclusion(
                reason=reason,
                evidence=_presizing_exclusion_evidence(item, reason=reason, plan=plan),
            )
        return _PreAiSizing(reference_price=reference_price, plan=plan)

    async def _review_candidate(
        self,
        owner_user_id: int,
        item: EvaluatedCandidate,
    ) -> _AiReviewOutcomeBundle:
        """AI에게 설명과 보조순위만 물어본다.

        이 메서드는 후보를 절대 탈락시키지 않는다. router가 없거나 실패하거나
        기술 판정과 다른 방향을 말하거나 신뢰도가 낮아도 상태만 기록한다.
        """

        regime = item.regime
        ranking = item.factor_ranking
        if self._ai_router is None or regime is None or ranking is None:
            return _AiReviewOutcomeBundle(
                ai_review=ai_review_from_observation(
                    status=AiReviewStatus.NOT_REQUESTED,
                    failure_reason=_AI_REVIEW_UNAVAILABLE,
                    detail="no AI route was available for this cycle",
                ),
                ai_shadow=None,
            )
        payload = {
            "symbol": item.candidate.symbol,
            "market": item.candidate.market,
            "candidateSource": item.candidate.source,
            "candidateRanking": ranking.as_evidence(),
            "regime": regime.regime.value,
            "regimeDetail": regime.detail,
            "strategyFamily": item.ensemble.family.value,
            "strategyVotes": list(item.ensemble.votes),
            "dailySetup": item.setup.as_evidence(),
            "entry": _level_text(item.ensemble.agreeing, "entry"),
            "stop": _level_text(item.ensemble.agreeing, "stop"),
            "target": _level_text(item.ensemble.agreeing, "target"),
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
        except AiProviderUnavailable:
            return _AiReviewOutcomeBundle(
                ai_review=ai_review_from_observation(
                    status=AiReviewStatus.UNAVAILABLE,
                    failure_reason="provider_unavailable",
                    detail="the AI provider route was unavailable",
                ),
                ai_shadow=None,
            )
        except ValidationError:
            logger.warning(
                "KAsset AI candidate response rejected: market=%s symbol=%s",
                item.candidate.market,
                item.candidate.symbol,
            )
            return _AiReviewOutcomeBundle(
                ai_review=ai_review_from_observation(
                    status=AiReviewStatus.INVALID,
                    failure_reason="invalid_ai_response",
                    detail="the AI response failed schema validation",
                ),
                ai_shadow=None,
            )
        observation = build_ai_shadow_observation(verdict, observed_at=self._now)
        action_text = str(verdict.action).strip().upper()
        ai_action = (
            Action(action_text)
            if action_text in {"BUY", "SELL", "HOLD"}
            else Action.HOLD
        )
        confidence = Decimal(str(verdict.confidence))
        if ai_action is not item.setup.direction:
            status = AiReviewStatus.DISAGREES
        elif not confidence.is_finite() or confidence < AI_COHORT_CONFIDENCE_FLOOR:
            status = AiReviewStatus.LOW_CONFIDENCE
        else:
            status = AiReviewStatus.AGREES
        return _AiReviewOutcomeBundle(
            ai_review=ai_review_from_observation(
                status=status,
                observation=observation,
                detail=(
                    f"technical direction={item.setup.direction.value} "
                    f"aiAction={ai_action.value} risk={verdict.risk}"
                ),
            ),
            ai_shadow=observation,
            bullish_score=int(verdict.bullish_score),
            bearish_score=int(verdict.bearish_score),
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
        item: AdmittedCandidate,
        regime: RegimeAssessment,
        *,
        position: int,
        total: int,
        sizing: _PreAiSizing,
    ) -> AIRecommendation:
        candidate = item.evaluated.candidate
        # 기술 판정 근거의 방향은 Daily Setup 방향과 같아야 한다. 이 불변식이
        # 깨지면 조용히 다른 방향/수량으로 저장하지 않고 멈춘다.
        if item.decision.action is not item.evaluated.setup.direction:
            raise ValueError("decision evidence must match the daily setup direction")
        plan = sizing.plan
        hard_risk = await self._policy.evaluate_hard_risk(
            self._db,
            owner_user_id,
            action=item.decision.action.value,
            market=candidate.market,
            symbol=candidate.symbol,
            quantity=plan.target_quantity,
            reference_price=sizing.reference_price,
            ai_confidence=item.decision.confidence,
            now=self._now,
        )
        persistence = AIRecommendationService(
            self._db,
            clock=lambda: self._now,
            cycle_trace_id=self._cycle_trace_id,
        )
        hard_risk_evidence = hard_risk.as_evidence()
        cohorts = build_decision_cohorts(
            action=item.decision.action,
            technical_admitted=bool(hard_risk.passed),
            technical_reason=(None if hard_risk.passed else "hard_risk_blocked"),
            ai_review=item.ai_review,
            news_shadow=item.news_shadow,
        )
        advisory_evidence: list[Mapping[str, object]] = [
            item.evaluated.setup.as_evidence(),
            item.trigger_decision.as_evidence(),
            item.ai_review.as_evidence(),
            item.news_shadow.as_evidence(),
            cohorts,
        ]
        row = await RecommendationProducer(
            owner_user_id=str(owner_user_id),
            persistence=persistence,
        ).produce(
            symbol=candidate.symbol,
            market=candidate.market,
            name=candidate.name,
            strategy_results=item.evaluated.strategy_results,
            decision_evidence=item.decision,
            suggested_quantity=plan.target_quantity,
            now=self._now,
            regime=regime.regime.value,
            regime_detail=regime.detail,
            strategy_weights=regime.weights,
            strategy_family=StrategyFamily.BREAKOUT,
            event_evidence=item.events,
            ranking={
                "score": str(item.score),
                "position": position,
                "total": total,
                "note": (
                    f"{candidate.source} 후보 {total}개 중 완료 일봉 Daily Setup과 "
                    "완료 장중 trigger로 진입 후보를 정하고, AI는 보조순위로만 "
                    "썼습니다."
                ),
            },
            portfolio=plan.as_evidence(),
            hard_risk=hard_risk_evidence,
            strategy_promotion={
                "strategyKey": DEFAULT_PAPER_STRATEGY_KEY,
                "version": DEFAULT_PAPER_STRATEGY_VERSION,
                "artifactFingerprint": self._strategy_artifact_fingerprint,
            },
            ai_shadow_evidence=(
                item.ai_shadow.as_selected_evidence()
                if item.ai_shadow is not None
                else None
            ),
            advisory_evidence=advisory_evidence,
        )
        return cast(AIRecommendation, row)

    async def _news_source_health(
        self,
        allowed_markets: frozenset[str],
    ) -> dict[str, bool]:
        """뉴스 수집 경로가 최근에 살아 있었는지 시장별로 한 번만 확인한다.

        ``article_published_at``의 naive KST/UTC 혼재 규약에 맞춰 naive UTC
        경계를 쓰며, 입증하지 못한 시장은 ``False``로 남아 종목별 shadow가
        ``UNKNOWN``이 된다.
        """

        cutoff = self._now.astimezone(UTC).replace(tzinfo=None) - _NEWS_HEALTH_WINDOW

        health: dict[str, bool] = {}
        for ranker_market in sorted(allowed_markets):
            market = "kr" if ranker_market == "KR" else "us"
            try:
                count = await self._db.scalar(
                    select(func.count())
                    .select_from(NewsArticle)
                    .where(
                        NewsArticle.market == market,
                        NewsArticle.article_published_at >= cutoff,
                    )
                )
            except Exception:  # noqa: BLE001 - health proof only, never a gate
                logger.warning(
                    "kasset news source health unprovable: market=%s",
                    market,
                    exc_info=True,
                )
                health[ranker_market] = False
                continue
            health[ranker_market] = bool(count)
        return health

    async def _news_shadow(
        self,
        candidate: TradingCandidate,
        *,
        health_proven: bool,
    ) -> NewsShadowEvidence:
        """뉴스/공시를 shadow로만 관측한다. 실패는 ``UNKNOWN``이다."""

        try:
            events = await self._event_evidence(candidate)
        except Exception:  # noqa: BLE001 - shadow collection never gates a decision
            logger.warning(
                "kasset news shadow collection failed: market=%s symbol=%s",
                candidate.market,
                candidate.symbol,
                exc_info=True,
            )
            return unknown_news_shadow(
                observed_at=self._now,
                detail="news and disclosure collection raised; treated as UNKNOWN",
            )
        news_count = sum(1 for event in events if event.get("kind") == "NEWS")
        disclosure_count = sum(
            1 for event in events if event.get("kind") == "DISCLOSURE"
        )
        return build_news_shadow(
            items=events,
            news_count=news_count,
            disclosure_count=disclosure_count,
            source_health_proven=health_proven,
            observed_at=self._now,
            detail=(
                "news_shadow never gates BUY/SELL; source health window is "
                f"{_NEWS_HEALTH_WINDOW}"
            ),
        )

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


@dataclass(frozen=True, slots=True)
class _AiReviewOutcomeBundle:
    """AI 검토 관측 결과. 어떤 필드도 후보 채택을 바꾸지 않는다."""

    ai_review: AiReviewEvidence
    ai_shadow: AiShadowObservation | None
    bullish_score: int = 0
    bearish_score: int = 0


def _setup_key(setup: DailySetup) -> CandidateKey:
    return ("KR" if setup.market == "KRX" else "US", setup.symbol)


def _benchmark_symbol(ranking: CandidateRankResult) -> str | None:
    """ranker가 일봉 상대강도에 실제로 쓴 벤치마크 식별자."""

    for item in ranking.evidence:
        if item.code == _RANKER_BENCHMARK_CODE:
            symbol = str(item.value).strip().upper()
            return symbol or None
    return None


def _trigger_exclusion_evidence(
    item: EvaluatedCandidate,
    decision: IntradayTriggerDecision,
) -> dict[str, object]:
    """방아쇠가 걸리지 않은 행을 ranker 제외 근거와 같은 모양으로 남긴다."""

    return {
        "title": "Intraday trigger exclusion",
        "source": "kasset_intraday_triggers",
        "kind": "candidate_exclusion",
        "symbol": item.candidate.symbol,
        "market": item.candidate.ranker_market,
        "exclusionReason": (
            f"{_NO_INTRADAY_TRIGGER}:{decision.compact_reason()}"[:128]
        ),
        "dailySetup": item.setup.as_evidence(),
        "intradayTriggers": decision.as_evidence(),
    }


def _admitted_candidate(
    item: EvaluatedCandidate,
    *,
    trigger_decision: IntradayTriggerDecision,
    review: _AiReviewOutcomeBundle,
    news_shadow: NewsShadowEvidence,
    now: datetime,
) -> AdmittedCandidate:
    """기술 판정을 확정 근거로 굳히고 AI는 보조순위에만 쓴다."""

    ranking = item.factor_ranking
    setup = item.setup
    active = tuple(trigger for trigger in trigger_decision.triggers if trigger.active)
    available = tuple(
        trigger for trigger in trigger_decision.triggers if trigger.available
    )
    trigger_strength = (
        Decimal(len(active)) / Decimal(len(available)) if available else Decimal("0")
    )
    valid_until = min(
        (
            result.valid_until.astimezone(UTC)
            for result in item.strategy_results
            if result.valid_until.tzinfo is not None
            and result.valid_until.utcoffset() is not None
        ),
        default=now + timedelta(hours=1),
    )
    if ranking is not None and ranking.valid_until is not None:
        valid_until = min(valid_until, ranking.valid_until.astimezone(UTC))
    valid_until = min(valid_until, now + timedelta(hours=1))
    confidence = min(
        Decimal("1"),
        (item.ensemble.confidence + trigger_strength) / Decimal("2"),
    ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    decision = ExternalEvidence(
        source="kasset_technical_decision:daily_setup+intraday_triggers",
        symbol=item.candidate.symbol,
        market=cast(Any, item.candidate.market),
        action=setup.direction,
        confidence=confidence,
        as_of=now,
        valid_until=valid_until,
        rationale=(
            "완료 일봉 Daily Setup이 적합하고 완료 장중 trigger 정책이 "
            "충족되어 진입 후보로 남았습니다.",
            f"활성 trigger: {', '.join(trigger.code for trigger in active) or '없음'}",
        ),
        evidence=(
            setup.as_evidence(),
            trigger_decision.as_evidence(),
        ),
    )
    # AI는 순위에만 5% 기여한다. AI가 없으면 그 항은 0이 되고 채택은 바뀌지 않는다.
    ai_bonus = Decimal("0")
    if review.ai_review.agrees:
        directional = (
            review.bullish_score
            if setup.direction is Action.BUY
            else review.bearish_score
        )
        ai_bonus = Decimal(directional) / Decimal("100")
    score = (
        (ranking.total_score if ranking is not None else Decimal("0")) * Decimal("0.45")
        + abs(item.ensemble.score) * Decimal("0.35")
        + trigger_strength * Decimal("0.15")
        + ai_bonus * Decimal("0.05")
    ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    return AdmittedCandidate(
        evaluated=item,
        trigger_decision=trigger_decision,
        decision=decision,
        ai_review=review.ai_review,
        news_shadow=news_shadow,
        events=news_shadow.items,
        score=score,
        ai_shadow=review.ai_shadow,
    )


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
            "markets=%s sources=%s ranked=%s evaluated=%s "
            "actionable=%s setup_selected=%s setup_statuses=%s "
            "setup_rejections=%s trigger_statuses=%s trigger_failures=%s "
            "pre_ai_exclusions=%s reviewed=%s review_cap_reached=%s "
            "review_rejections=%s recommendations=%d",
            owner_id,
            cycle_trace_id,
            result.get("skipped"),
            result.get("candidateCount", 0),
            result.get("candidateMarkets"),
            result.get("candidateSources"),
            result.get("rankedCount"),
            result.get("strategyEvaluatedCount"),
            result.get("strategyActionableCount"),
            result.get("dailySetupSelectedCount"),
            result.get("dailySetupStatuses"),
            result.get("dailySetupRejections"),
            result.get("intradayTriggerStatuses"),
            result.get("intradayTriggerFailures"),
            result.get("preAiExclusions"),
            result.get("aiReviewedCount"),
            result.get("strategyReviewCapReached"),
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


def _presizing_exclusion_reason(plan: PortfolioPlan) -> str:
    """Name the deterministic sizing verdict that kept this row away from AI."""

    sizing = plan.position_sizing
    codes = (
        ",".join(dict.fromkeys(reason.code.value for reason in sizing.zero_reasons))
        if sizing is not None
        else ""
    )
    return f"{_PRESIZING_ZERO_QUANTITY}:{codes}" if codes else _PRESIZING_ZERO_QUANTITY


def _presizing_exclusion_evidence(
    item: EvaluatedCandidate,
    *,
    reason: str,
    plan: PortfolioPlan | None,
) -> dict[str, object]:
    """Shape the pre-AI rejection like a ranker exclusion so the audit reads it."""

    ranking = item.factor_ranking
    evidence: dict[str, object] = {
        "title": "Pre-AI position sizing exclusion",
        "source": "kasset_vertical_slice",
        "kind": "candidate_exclusion",
        "symbol": item.candidate.symbol,
        "market": item.candidate.ranker_market,
        "exclusionReason": reason,
        "strategyAction": item.ensemble.action.value,
        "rankPosition": ranking.rank_position if ranking is not None else None,
    }
    if plan is not None:
        evidence["targetQuantity"] = str(plan.target_quantity)
        evidence["portfolio"] = plan.as_evidence()
    return evidence


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
    "AdmittedCandidate",
    "EvaluatedCandidate",
    "TradingCandidate",
    "run_ai_recommendation_cycle_once",
]
