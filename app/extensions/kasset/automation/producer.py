"""Deterministic recommendation synthesis and persistence boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, cast

from app.extensions.kasset.automation.contracts import (
    Action,
    ExternalEvidence,
    RecommendationDraft,
    RecommendationPersistence,
    StrategyName,
    StrategyResult,
    utc_datetime,
)

_EXPECTED_STRATEGIES = frozenset(StrategyName)
_CONFIDENCE_TEXT = {
    "low": Decimal("0.25"),
    "medium": Decimal("0.50"),
    "high": Decimal("0.75"),
}


def _decimal_confidence(value: object) -> Decimal:
    if isinstance(value, str) and value.strip().lower() in _CONFIDENCE_TEXT:
        return _CONFIDENCE_TEXT[value.strip().lower()]
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not result.is_finite():
        return Decimal("0")
    return max(Decimal("0"), min(Decimal("1"), result))


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def external_evidence_from_mapping(
    payload: Mapping[str, object],
    *,
    now: datetime,
) -> ExternalEvidence:
    """Normalize ``analyze_stock_impl`` or an injected mapping without I/O.

    Missing/stale/future metadata and incomplete analysis are converted to HOLD;
    arbitrary provider payload is never retained wholesale in recommendation
    evidence.
    """

    current = utc_datetime(now, field_name="now")
    recommendation_value = payload.get("recommendation")
    recommendation = (
        recommendation_value if isinstance(recommendation_value, Mapping) else payload
    )
    raw_action = str(recommendation.get("action", "HOLD")).strip().upper()
    action = (
        Action(raw_action) if raw_action in {"BUY", "SELL", "HOLD"} else Action.HOLD
    )
    confidence = _decimal_confidence(recommendation.get("confidence", 0))
    symbol = str(payload.get("symbol") or "").strip().upper()
    raw_market = (
        str(payload.get("market") or payload.get("market_type") or "").strip().upper()
    )
    market = {
        "KR": "KRX",
        "KRX": "KRX",
        "EQUITY_KR": "KRX",
        "US": "US",
        "EQUITY_US": "US",
    }.get(raw_market, "")

    insufficient = recommendation.get("insufficient_inputs")
    has_insufficient = bool(insufficient)

    as_of = (
        _parse_datetime(payload.get("derived_as_of"))
        or _parse_datetime(payload.get("as_of"))
        or _parse_datetime(recommendation.get("as_of"))
    )
    validity_seconds = payload.get("valid_for_seconds", 86400)
    try:
        validity = timedelta(seconds=max(1, int(str(validity_seconds))))
    except (TypeError, ValueError):
        validity = timedelta(days=1)

    reasons: list[str] = []
    reasoning = recommendation.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        reasons.append(reasoning.strip())
    if not symbol or market not in {"KRX", "US"}:
        reasons.append("External evidence has no supported symbol and market scope.")
        action = Action.HOLD
        confidence = Decimal("0")
    if as_of is None:
        reasons.append("External evidence has no timezone-aware as-of timestamp.")
        as_of = current
        valid_until = current
        action = Action.HOLD
        confidence = Decimal("0")
    else:
        valid_until = as_of + validity
        if as_of > current:
            reasons.append("Future-dated external evidence was rejected.")
            action = Action.HOLD
            confidence = Decimal("0")
            valid_until = current
        elif valid_until <= current:
            reasons.append("Expired external evidence was rejected.")
            action = Action.HOLD
            confidence = Decimal("0")
    if has_insufficient:
        reasons.append("External analysis reported insufficient inputs.")
        action = Action.HOLD
        confidence = Decimal("0")
    if not reasons:
        reasons.append("Normalized deterministic external analysis result.")

    source = str(payload.get("source") or "external_evidence").strip()
    return ExternalEvidence(
        source=source[:128] or "external_evidence",
        symbol=symbol,
        market=cast(Any, market),
        action=action,
        confidence=confidence,
        as_of=as_of,
        valid_until=valid_until,
        rationale=tuple(reasons),
        evidence=(
            {
                "kind": "external_analysis",
                "source": source[:128] or "external_evidence",
                "symbol": symbol,
                "market": market,
                "action": action.value,
                "confidence": str(confidence),
                "asOf": as_of.isoformat(),
            },
        ),
    )


class RecommendationProducer:
    """Synthesizes advisory facts and delegates the only write to an adapter."""

    def __init__(
        self,
        *,
        owner_user_id: str,
        persistence: RecommendationPersistence,
    ) -> None:
        owner = owner_user_id.strip()
        if not owner:
            raise ValueError("owner_user_id is required")
        self._owner_user_id = owner
        self._persistence = persistence

    async def produce(
        self,
        *,
        symbol: str,
        market: str,
        strategy_results: Sequence[StrategyResult],
        external_evidence: ExternalEvidence | Mapping[str, object],
        suggested_quantity: Decimal | str | None,
        now: datetime,
    ) -> object:
        current = utc_datetime(now, field_name="now").replace(microsecond=0)
        normalized_symbol = symbol.strip().upper()
        normalized_market = market.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required")
        if normalized_market not in {"KRX", "US"}:
            raise ValueError("market must be KRX or US")
        typed_market = cast(Any, normalized_market)

        external = (
            external_evidence
            if isinstance(external_evidence, ExternalEvidence)
            else external_evidence_from_mapping(external_evidence, now=current)
        )
        external = self._validated_external(
            external,
            now=current,
            symbol=normalized_symbol,
            market=normalized_market,
        )

        valid_results: dict[StrategyName, StrategyResult] = {}
        rejected_reasons: list[str] = []
        for result in strategy_results:
            if result.strategy in valid_results:
                rejected_reasons.append(f"duplicate strategy result: {result.strategy}")
                continue
            if result.symbol != normalized_symbol or result.market != normalized_market:
                rejected_reasons.append(f"scope mismatch: {result.strategy}")
                continue
            if result.as_of.tzinfo is None or result.as_of.utcoffset() is None:
                rejected_reasons.append(f"naive as-of: {result.strategy}")
                continue
            if (
                result.valid_until.tzinfo is None
                or result.valid_until.utcoffset() is None
            ):
                rejected_reasons.append(f"naive valid-until: {result.strategy}")
                continue
            if result.as_of.astimezone(UTC) > current:
                rejected_reasons.append(f"future strategy result: {result.strategy}")
                continue
            if result.valid_until.astimezone(UTC) <= current:
                rejected_reasons.append(f"expired strategy result: {result.strategy}")
                continue
            if not result.confidence.is_finite() or not (
                Decimal("0") <= result.confidence <= Decimal("1")
            ):
                rejected_reasons.append(f"invalid confidence: {result.strategy}")
                continue
            valid_results[result.strategy] = result

        missing = _EXPECTED_STRATEGIES - valid_results.keys()
        if missing:
            rejected_reasons.append(
                "missing strategies: "
                + ",".join(sorted(item.value for item in missing))
            )
        strategy_input_valid = not rejected_reasons

        buy_results = [
            result for result in valid_results.values() if result.action == Action.BUY
        ]
        sell_results = [
            result for result in valid_results.values() if result.action == Action.SELL
        ]
        candidate = Action.HOLD
        agreeing: list[StrategyResult] = []
        if len(buy_results) >= 2 and len(buy_results) > len(sell_results):
            candidate, agreeing = Action.BUY, buy_results
        elif len(sell_results) >= 2 and len(sell_results) > len(buy_results):
            candidate, agreeing = Action.SELL, sell_results

        confidence = Decimal("0")
        if (
            candidate != Action.HOLD
            and external.action == candidate
            and strategy_input_valid
        ):
            strategy_confidence = sum(
                (result.confidence for result in agreeing), Decimal("0")
            ) / Decimal(len(agreeing))
            confidence = min(strategy_confidence, external.confidence)
            if confidence < Decimal("0.50"):
                rejected_reasons.append("combined confidence is below the action floor")
                candidate = Action.HOLD
                confidence = Decimal("0")
        else:
            if candidate != Action.HOLD and external.action != candidate:
                rejected_reasons.append(
                    "external evidence does not confirm strategy quorum"
                )
            candidate = Action.HOLD

        quantity = self._quantity_or_none(suggested_quantity)
        if candidate != Action.HOLD and quantity is None:
            rejected_reasons.append(
                "positive suggested quantity is required for automation"
            )
            candidate = Action.HOLD
            confidence = Decimal("0")

        reference_price = self._reference_price(agreeing)
        strategy_valid_until = min(
            (result.valid_until.astimezone(UTC) for result in valid_results.values()),
            default=current,
        )
        valid_until = min(strategy_valid_until, external.valid_until.astimezone(UTC))
        if valid_until <= current:
            candidate = Action.HOLD
            confidence = Decimal("0")
            valid_until = current

        vote_summary = ", ".join(
            f"{name.value}={valid_results[name].action.value}"
            for name in sorted(valid_results, key=lambda item: item.value)
        )
        rationale = [f"Deterministic strategy votes: {vote_summary or 'none'}."]
        rationale.extend(external.rationale)
        evidence: list[Mapping[str, object]] = []
        for result in valid_results.values():
            evidence.append(
                {
                    "kind": "strategy",
                    "strategy": result.strategy.value,
                    "version": result.version,
                    "action": result.action.value,
                    "confidence": str(result.confidence),
                    "entry": str(result.entry) if result.entry is not None else None,
                    "stop": str(result.stop) if result.stop is not None else None,
                    "target": str(result.target) if result.target is not None else None,
                    "asOf": result.as_of.isoformat(),
                    "validUntil": result.valid_until.isoformat(),
                    "rationale": list(result.rationale),
                    "evidence": [
                        {
                            "code": item.code,
                            "value": item.value,
                            "description": item.description,
                        }
                        for item in result.evidence
                    ],
                }
            )
        evidence.extend(external.evidence)

        draft = RecommendationDraft(
            owner_user_id=self._owner_user_id,
            action=candidate,
            market=typed_market,
            symbol=normalized_symbol,
            headline=f"{normalized_symbol} {candidate.value} deterministic consensus",
            rationale=tuple(rationale),
            risks=tuple(rejected_reasons),
            evidence=tuple(evidence),
            confidence=confidence.quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_EVEN
            ),
            reference_price=reference_price,
            suggested_quantity=quantity if candidate != Action.HOLD else None,
            source="kasset-automation",
            created_at=current,
            valid_until=valid_until,
        )
        return await self._persistence.create_recommendation(
            owner_user_id=self._owner_user_id,
            draft=draft,
        )

    @staticmethod
    def _validated_external(
        external: ExternalEvidence,
        *,
        now: datetime,
        symbol: str,
        market: str,
    ) -> ExternalEvidence:
        try:
            as_of = utc_datetime(external.as_of, field_name="external.as_of")
            valid_until = utc_datetime(
                external.valid_until, field_name="external.valid_until"
            )
        except (AttributeError, TypeError, ValueError):
            return ExternalEvidence(
                source=external.source,
                symbol=external.symbol,
                market=external.market,
                action=Action.HOLD,
                confidence=Decimal("0"),
                as_of=now,
                valid_until=now,
                rationale=("External evidence has invalid time metadata.",),
            )
        confidence_valid = (
            isinstance(external.confidence, Decimal)
            and external.confidence.is_finite()
            and Decimal("0") <= external.confidence <= Decimal("1")
        )
        if (
            str(external.symbol).strip().upper() != symbol
            or str(external.market).strip().upper() != market
            or as_of > now
            or valid_until <= now
            or not confidence_valid
        ):
            return ExternalEvidence(
                source=external.source,
                symbol=external.symbol,
                market=external.market,
                action=Action.HOLD,
                confidence=Decimal("0"),
                as_of=min(as_of, now),
                valid_until=now,
                rationale=("External evidence failed freshness validation.",),
            )
        return external

    @staticmethod
    def _quantity_or_none(value: Decimal | str | None) -> Decimal | None:
        if value is None:
            return None
        try:
            quantity = value if isinstance(value, Decimal) else Decimal(value)
        except (InvalidOperation, ValueError):
            return None
        if not quantity.is_finite() or quantity <= Decimal("0"):
            return None
        return quantity

    @staticmethod
    def _reference_price(results: Sequence[StrategyResult]) -> Decimal | None:
        entries = sorted(
            result.entry
            for result in results
            if result.entry is not None
            and result.entry.is_finite()
            and result.entry > 0
        )
        if not entries:
            return None
        return entries[len(entries) // 2]


__all__ = ["RecommendationProducer", "external_evidence_from_mapping"]
