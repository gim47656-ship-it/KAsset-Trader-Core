"""Screener-to-recommendation vertical slice for APPROVAL and AUTO_PAPER."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.model_router import AnalysisKind, OpenAiModelRouter
from app.extensions.kasset.api.watchlist import watchlist_service
from app.extensions.kasset.automation.contracts import (
    Action,
    ExternalEvidence,
    PriceBar,
    StrategyResult,
)
from app.extensions.kasset.automation.policy import AITradingPolicyService
from app.extensions.kasset.automation.producer import (
    RecommendationProducer,
    WeightedEnsembleDecision,
    compose_weighted_ensemble,
)
from app.extensions.kasset.automation.regime import (
    RegimeAssessment,
    assess_market_regime,
)
from app.extensions.kasset.automation.strategies import STRATEGIES
from app.models.ai_recommendations import AIRecommendation
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.news import NewsArticle
from app.models.symbol_master import SymbolMaster
from app.models.trading import User, UserRole
from app.services.ai_recommendations.service import AIRecommendationService
from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey
from app.services.disclosures.feed_sources import DISCLOSURE_FEED_SOURCES
from app.services.symbol_news_store import load_symbol_news

_CANDIDATE_LIMIT = 100
_MIN_CANDIDATE_TARGET = 50
_AI_REVIEW_LIMIT = 12
_RECOMMENDATION_LIMIT = 5
_OWNER_COOLDOWN = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class TradingCandidate:
    symbol: str
    market: str
    name: str | None
    source: str


@dataclass(frozen=True, slots=True)
class EvaluatedCandidate:
    candidate: TradingCandidate
    strategy_results: tuple[StrategyResult, ...]
    ensemble: WeightedEnsembleDecision


@dataclass(frozen=True, slots=True)
class ReviewedCandidate:
    evaluated: EvaluatedCandidate
    external: ExternalEvidence
    events: tuple[Mapping[str, object], ...]
    event_score: Decimal
    score: Decimal


async def _load_live_kr_candidates() -> tuple[TradingCandidate, ...]:
    from app.services.invest_kr_fundamentals_snapshots.provider import (
        TvScreenerKrFundamentalsProvider,
    )

    rows = await TvScreenerKrFundamentalsProvider(timeout=30).fetch_rows(
        limit=_CANDIDATE_LIMIT
    )
    ordered = sorted(
        rows,
        key=lambda row: (row.price or Decimal("0")) * (row.volume or Decimal("0")),
        reverse=True,
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
            )
        )
    return tuple(candidates)


class AIRecommendationVerticalSlice:
    """Run one owner through existing candles, strategies, AI, and persistence."""

    def __init__(
        self,
        db: AsyncSession,
        ai_router: OpenAiModelRouter,
        *,
        now: datetime,
        live_candidates_cache: dict[str, tuple[TradingCandidate, ...]] | None = None,
    ) -> None:
        self._db = db
        self._ai_router = ai_router
        self._now = _aware_utc(now)
        self._policy = AITradingPolicyService()
        self._live_candidates_cache = (
            live_candidates_cache if live_candidates_cache is not None else {}
        )

    async def run_owner(self, owner_user_id: int) -> dict[str, object]:
        if await self._cooldown_active(owner_user_id):
            return {
                "ownerUserId": owner_user_id,
                "skipped": "recommendation_cooldown_active",
                "candidateCount": 0,
                "recommendationIds": [],
            }

        snapshot = await self._policy.get_snapshot(
            self._db,
            owner_user_id,
            now=self._now,
            execution_limit=0,
        )
        candidates = await self._load_candidates(
            owner_user_id,
            currency=snapshot.limits.currency,
        )
        if not candidates:
            return {
                "ownerUserId": owner_user_id,
                "skipped": "screener_candidates_unavailable",
                "candidateCount": 0,
                "recommendationIds": [],
            }
        candle_sync = (
            await self._sync_missing_kr_candles(candidates)
            if snapshot.limits.currency == "KRW"
            else {"requested": 0, "synced": 0, "failed": 0}
        )

        market_key = MarketKey.KR if snapshot.limits.currency == "KRW" else MarketKey.US
        bars_by_symbol = await DailyCandlesRepository(
            session=self._db
        ).fetch_recent_batch(
            market=market_key,
            symbols=[candidate.symbol for candidate in candidates],
            partition="KRX" if market_key == MarketKey.KR else None,
            count=60,
        )
        normalized_bars = {
            symbol: _price_bars(rows) for symbol, rows in bars_by_symbol.items()
        }
        regime = assess_market_regime(normalized_bars)
        evaluated = self._evaluate_candidates(candidates, normalized_bars, regime)
        actionable = sorted(
            (
                item
                for item in evaluated
                if item.ensemble.action in {Action.BUY, Action.SELL}
            ),
            key=lambda item: (
                abs(item.ensemble.score),
                item.candidate.symbol,
            ),
            reverse=True,
        )
        reviewed: list[ReviewedCandidate] = []
        ai_failures = 0
        for item in actionable[:_AI_REVIEW_LIMIT]:
            try:
                reviewed_item = await self._review_candidate(
                    owner_user_id,
                    item,
                    regime,
                )
            except AiProviderUnavailable:
                ai_failures += 1
                continue
            if reviewed_item is not None:
                reviewed.append(reviewed_item)

        reviewed.sort(
            key=lambda item: (item.score, item.evaluated.candidate.symbol),
            reverse=True,
        )
        recommendation_ids: list[str] = []
        total = len(evaluated)
        for position, item in enumerate(reviewed[:_RECOMMENDATION_LIMIT], start=1):
            row = await self._persist_recommendation(
                owner_user_id,
                item,
                regime,
                position=position,
                total=total,
                snapshot=snapshot,
            )
            if row.action in {"BUY", "SELL"}:
                recommendation_ids.append(row.id)

        result: dict[str, object] = {
            "ownerUserId": owner_user_id,
            "candidateCount": len(candidates),
            "candidateTargetMet": len(evaluated) >= _MIN_CANDIDATE_TARGET,
            "strategyEvaluatedCount": len(evaluated),
            "aiReviewedCount": len(reviewed),
            "aiFailureCount": ai_failures,
            "candleSync": candle_sync,
            "regime": regime.regime.value,
            "recommendationIds": recommendation_ids,
        }
        if len(evaluated) < _MIN_CANDIDATE_TARGET:
            result["dataPrerequisite"] = (
                "fewer than 50 screener candidates have usable daily candles"
            )
        if not actionable:
            result["skipped"] = "no_dynamic_ensemble_signal"
        elif not reviewed:
            result["skipped"] = "no_ai_confirmed_signal"
        return result

    async def _load_candidates(
        self,
        owner_user_id: int,
        *,
        currency: str,
    ) -> list[TradingCandidate]:
        market = "kr" if currency == "KRW" else "us"
        recommendation_market = "KRX" if market == "kr" else "US"
        watchlist = await watchlist_service.list_items(self._db, owner_user_id)
        ordered: dict[str, TradingCandidate] = {}
        for item in watchlist.items:
            if item.market != recommendation_market:
                continue
            ordered[item.symbol] = TradingCandidate(
                symbol=item.symbol,
                market=recommendation_market,
                name=item.name,
                source="watchlist",
            )

        latest_date = await self._db.scalar(
            select(func.max(InvestScreenerSnapshot.snapshot_date)).where(
                InvestScreenerSnapshot.market == market
            )
        )
        if latest_date is not None:
            rows = (
                await self._db.scalars(
                    select(InvestScreenerSnapshot)
                    .where(
                        InvestScreenerSnapshot.market == market,
                        InvestScreenerSnapshot.snapshot_date == latest_date,
                    )
                    .order_by(
                        InvestScreenerSnapshot.daily_turnover.desc().nullslast(),
                        InvestScreenerSnapshot.daily_volume.desc().nullslast(),
                        InvestScreenerSnapshot.symbol,
                    )
                    .limit(_CANDIDATE_LIMIT)
                )
            ).all()
            snapshot_symbols = tuple(
                dict.fromkeys(
                    str(row.symbol).strip().upper()
                    for row in rows
                    if str(row.symbol).strip()
                )
            )
            snapshot_names = (
                {
                    symbol: name
                    for symbol, name in (
                        await self._db.execute(
                            select(SymbolMaster.symbol, SymbolMaster.name).where(
                                SymbolMaster.market == recommendation_market,
                                SymbolMaster.symbol.in_(snapshot_symbols),
                            )
                        )
                    ).all()
                    if name and name.strip() and name.strip() != symbol
                }
                if snapshot_symbols
                else {}
            )
            for row in rows:
                symbol = str(row.symbol).strip().upper()
                if symbol and symbol not in ordered:
                    ordered[symbol] = TradingCandidate(
                        symbol=symbol,
                        market=recommendation_market,
                        name=snapshot_names.get(symbol),
                        source=f"invest_screener_snapshots:{latest_date.isoformat()}",
                    )
                if len(ordered) >= _CANDIDATE_LIMIT:
                    break
        if market == "kr" and ordered and len(ordered) < _MIN_CANDIDATE_TARGET:
            live = self._live_candidates_cache.get(market)
            if live is None:
                live = await _load_live_kr_candidates()
                self._live_candidates_cache[market] = live
            for candidate in live:
                ordered.setdefault(candidate.symbol, candidate)
                if len(ordered) >= _CANDIDATE_LIMIT:
                    break

        return list(ordered.values())[:_CANDIDATE_LIMIT]

    async def _sync_missing_kr_candles(
        self,
        candidates: Sequence[TradingCandidate],
    ) -> dict[str, int]:
        repository = DailyCandlesRepository(session=self._db)
        existing = await repository.fetch_recent_batch(
            market=MarketKey.KR,
            symbols=[candidate.symbol for candidate in candidates],
            partition="KRX",
            count=20,
        )
        missing = [
            candidate
            for candidate in candidates
            if len(existing.get(candidate.symbol, ())) < 20
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
                        count=60,
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
            if len(rows) < 20:
                failed += 1
                continue
            await repository.upsert_rows(market=MarketKey.KR, rows=rows)
            synced += 1
        await self._db.commit()
        return {"requested": len(missing), "synced": synced, "failed": failed}

    def _evaluate_candidates(
        self,
        candidates: Sequence[TradingCandidate],
        bars_by_symbol: Mapping[str, Sequence[PriceBar]],
        regime: RegimeAssessment,
    ) -> list[EvaluatedCandidate]:
        evaluated: list[EvaluatedCandidate] = []
        for candidate in candidates:
            bars = bars_by_symbol.get(candidate.symbol, ())
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
                )
            )
        return evaluated

    async def _review_candidate(
        self,
        owner_user_id: int,
        item: EvaluatedCandidate,
        regime: RegimeAssessment,
    ) -> ReviewedCandidate | None:
        events = await self._event_evidence(item.candidate)
        payload = {
            "symbol": item.candidate.symbol,
            "market": item.candidate.market,
            "candidateSource": item.candidate.source,
            "regime": regime.regime.value,
            "regimeDetail": regime.detail,
            "strategyVotes": list(item.ensemble.votes),
            "entry": _level_text(item.ensemble.agreeing, "entry"),
            "stop": _level_text(item.ensemble.agreeing, "stop"),
            "target": _level_text(item.ensemble.agreeing, "target"),
            "events": [dict(event) for event in events],
        }
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
        action_text = str(verdict.action).strip().upper()
        action = (
            Action(action_text)
            if action_text in {"BUY", "SELL", "HOLD"}
            else Action.HOLD
        )
        if action != item.ensemble.action:
            return None
        confidence = Decimal(str(verdict.confidence))
        if not confidence.is_finite() or confidence < Decimal("0.50"):
            return None
        valid_until = min(
            (
                result.valid_until.astimezone(UTC)
                for result in item.strategy_results
                if result.valid_until.tzinfo is not None
                and result.valid_until.utcoffset() is not None
            ),
            default=self._now,
        )
        valid_until = min(valid_until, self._now + timedelta(hours=1))
        if valid_until <= self._now:
            return None
        external = ExternalEvidence(
            source=f"model_router:{verdict.tier_used}",
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
                    "kind": "ai_analysis",
                    "tier": verdict.tier_used,
                    "confidence": str(confidence),
                    "risk": str(verdict.risk),
                    "bullishScore": int(verdict.bullish_score),
                    "bearishScore": int(verdict.bearish_score),
                    "eventCount": len(events),
                },
            ),
        )
        directional_score = Decimal(
            verdict.bullish_score if action == Action.BUY else verdict.bearish_score
        ) / Decimal("100")
        event_score = directional_score if events else Decimal("0")
        score = (
            abs(item.ensemble.score) * Decimal("0.65")
            + confidence * Decimal("0.25")
            + event_score * Decimal("0.10")
        ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
        return ReviewedCandidate(
            evaluated=item,
            external=external,
            events=events,
            event_score=event_score,
            score=score,
        )

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
        persistence = AIRecommendationService(self._db, clock=lambda: self._now)
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
    """Operator/task entrypoint; generates review rows but never calls a broker."""

    current = _aware_utc(now or datetime.now(UTC))
    if not settings.KASSET_MARKET_EVENTS_ENABLED:
        return {"enabled": False, "owners": [], "candidateCount": 0}
    from app.extensions.kasset.ai.factory import build_model_router

    try:
        ai_router = build_model_router()
    except AiProviderUnavailable:
        return {
            "enabled": True,
            "owners": [],
            "candidateCount": 0,
            "skipped": "ai_unavailable",
        }
    live_candidates_cache: dict[str, tuple[TradingCandidate, ...]] = {}
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
    owners: list[dict[str, object]] = []
    total_candidates = 0
    for raw_owner_id in owner_ids:
        owner_id = int(raw_owner_id)
        try:
            async with _session() as db:
                result = await AIRecommendationVerticalSlice(
                    db,
                    ai_router,
                    now=current,
                    live_candidates_cache=live_candidates_cache,
                ).run_owner(owner_id)
        except Exception as exc:
            result = {
                "ownerUserId": owner_id,
                "candidateCount": 0,
                "recommendationIds": [],
                "skipped": "owner_cycle_failed",
                "errorClass": type(exc).__name__,
            }
        total_candidates += int(result.get("candidateCount", 0))
        owners.append(result)
    return {
        "enabled": True,
        "owners": owners,
        "candidateCount": total_candidates,
    }


def _price_bars(rows: Sequence[Any]) -> tuple[PriceBar, ...]:
    bars: list[PriceBar] = []
    for row in rows:
        timestamp = row.time_utc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        bars.append(
            PriceBar(
                timestamp=timestamp.astimezone(UTC),
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
