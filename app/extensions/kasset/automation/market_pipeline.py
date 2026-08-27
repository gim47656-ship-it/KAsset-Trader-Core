"""Event-driven KAsset market analysis and recommendation persistence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.automation.event_detector import EventDetector
from app.extensions.kasset.automation.feature_engine import FeatureEngine
from app.models.ai_recommendations import (
    AIRecommendation,
    RecommendationDecision,
)
from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey

if TYPE_CHECKING:
    from app.extensions.kasset.ai.model_router import OpenAiModelRouter


class MarketEventPipeline:
    """Analyze only detected events and persist actionable recommendations."""

    def __init__(
        self,
        session: AsyncSession,
        router: OpenAiModelRouter,
        clock: Callable[[], datetime],
    ) -> None:
        self._session = session
        self._router = router
        self._clock = clock
        self._features = FeatureEngine()
        self._detector = EventDetector()

    async def run_symbol_scan(
        self,
        owner_user_id: int,
        market: str,
        symbol: str,
    ) -> dict[str, object]:
        candle_market, partition, recommendation_market = _market_route(market)
        candles = await DailyCandlesRepository(session=self._session).fetch_recent(
            market=candle_market,
            symbol=symbol,
            partition=partition,
            count=40,
        )
        features = self._features.calculate(candles)
        if features["insufficient"] is True:
            return {"skipped": "insufficient_data"}

        news_summaries: list[dict[str, object]] = []
        triggers = self._detector.detect(features, news_summaries)
        if not triggers:
            return {"skipped": "no_triggers"}

        now = _normalized_now(self._clock())
        correlation_id = (
            f"market-scan:{owner_user_id}:{market}:{symbol}:{int(now.timestamp())}"
        )
        payload: dict[str, object] = {
            "symbol": symbol,
            "market": market,
            "features": features,
            "triggers": triggers,
            "news": news_summaries,
        }

        from app.extensions.kasset.ai.model_router import AnalysisKind

        try:
            verdict = await self._router.analyze(
                AnalysisKind.CANDIDATE_SCAN,
                payload,
                correlation_id=correlation_id,
            )
        except AiProviderUnavailable:
            return {"skipped": "ai_unavailable"}

        action = str(verdict.action)
        confidence = float(verdict.confidence)
        result: dict[str, object] = {
            "action": action,
            "confidence": confidence,
            "tier_used": verdict.tier_used,
        }
        if action not in {"BUY", "SELL"} or confidence < 0.60:
            return result
        if recommendation_market is None:
            return {**result, "skipped": "unsupported_recommendation_market"}

        recommendation = AIRecommendation(
            owner_user_id=owner_user_id,
            action=action,
            decision=RecommendationDecision.PENDING,
            market=recommendation_market,
            symbol=symbol,
            currency="KRW" if recommendation_market == "KRX" else "USD",
            rationale=list(verdict.rationale_tags),
            risks=[str(verdict.risk)],
            evidence=[
                {
                    "features": features,
                    "triggers": triggers,
                    "tier_used": verdict.tier_used,
                }
            ],
            confidence=str(confidence),
            source="kasset_market_events",
            created_at=now,
            valid_until=now + timedelta(hours=1),
            updated_at=now,
        )
        self._session.add(recommendation)
        await self._session.commit()
        await self._session.refresh(recommendation)
        return {**result, "recommendation_id": recommendation.id}


def _market_route(market: str) -> tuple[MarketKey, str, str | None]:
    normalized = market.strip().upper()
    if normalized in {"KR", "KRX", "NTX", "NXT"}:
        # KR alternate-venue candles are partitioned as "NTX" in the database;
        # accept the display spelling "NXT" but never query with it.
        partition = "NTX" if normalized in {"NTX", "NXT"} else "KRX"
        return MarketKey.KR, partition, "KRX"
    if normalized in {"US", "NASD", "NASDAQ", "NYSE", "AMEX"}:
        partition = {"US": "NASD", "NASDAQ": "NASD"}.get(normalized, normalized)
        return MarketKey.US, partition, "US"
    if normalized in {"CRYPTO", "UPBIT", "UPBIT_KRW"}:
        return MarketKey.CRYPTO, "upbit_krw", None
    raise ValueError("unsupported market")


def _normalized_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError(
            "market pipeline clock must return a timezone-aware datetime"
        )
    return value.astimezone(UTC).replace(microsecond=0)


__all__ = ["MarketEventPipeline"]
