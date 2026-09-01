"""Deterministic recommendation synthesis and persistence boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, cast

from app.extensions.kasset.automation.ai_shadow import (
    validate_selected_ai_shadow_evidence,
)
from app.extensions.kasset.automation.contracts import (
    Action,
    ExternalEvidence,
    RecommendationDraft,
    RecommendationPersistence,
    StrategyFamily,
    StrategyName,
    StrategyResult,
    strategies_in_family,
    utc_datetime,
)

_EXPECTED_STRATEGIES = frozenset(StrategyName)
_CONFIDENCE_TEXT = {
    "low": Decimal("0.25"),
    "medium": Decimal("0.50"),
    "high": Decimal("0.75"),
}

_STRATEGY_LABELS = {
    StrategyName.MOMENTUM: "모멘텀",
    StrategyName.MEAN_REVERSION: "평균회귀",
    StrategyName.BREAKOUT: "돌파",
    StrategyName.VOLATILITY_TREND: "변동성추세",
}
_ACTION_LABELS = {
    Action.BUY: "매수",
    Action.SELL: "매도",
    Action.HOLD: "관망",
}


def _korean_vote_rationale(
    valid_results: Mapping[StrategyName, StrategyResult],
) -> str:
    if not valid_results:
        return "유효한 전략 투표가 없습니다."
    votes = ", ".join(
        f"{_STRATEGY_LABELS[name]}={_ACTION_LABELS[valid_results[name].action]}"
        for name in StrategyName
        if name in valid_results
    )
    return f"전략 투표 결과는 {votes}입니다."


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


@dataclass(frozen=True, slots=True)
class WeightedEnsembleDecision:
    family: StrategyFamily
    action: Action
    score: Decimal
    confidence: Decimal
    agreeing: tuple[StrategyResult, ...]
    votes: tuple[Mapping[str, object], ...]


#: 전략군 가중합이 방향을 주장하기 위한 최소 절대 점수.
_ENSEMBLE_DIRECTION_FLOOR = Decimal("0.25")


def compose_weighted_ensemble(
    strategy_results: Sequence[StrategyResult],
    weights: Mapping[StrategyName, Decimal],
    *,
    family: StrategyFamily,
) -> WeightedEnsembleDecision:
    """Combine exactly one strategy family's existing signals.

    ``family`` 밖의 전략 결과는 표도, 가중치도, 점수도 받지 못한다. 방향은
    전략군 가중합 부호와 ``_ENSEMBLE_DIRECTION_FLOOR``만으로 정하며 "N개 중
    2개 동의" 같은 일반 정족수는 쓰지 않는다. 진입 여부는 이 방향이 아니라
    Daily Setup 적합과 장중 trigger 정책이 판정한다.
    """

    members = strategies_in_family(family)
    normalized: dict[StrategyName, Decimal] = {}
    total = Decimal("0")
    for name in members:
        raw = weights.get(name, Decimal("0"))
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
        if not value.is_finite() or value < 0:
            raise ValueError(f"invalid strategy weight: {name.value}")
        normalized[name] = value
        total += value
    if total <= 0:
        raise ValueError("strategy weights must have a positive sum")
    normalized = {name: value / total for name, value in normalized.items()}

    by_name = {result.strategy: result for result in strategy_results}
    score = Decimal("0")
    votes: list[Mapping[str, object]] = []
    for name in members:
        result = by_name.get(name)
        if result is None:
            continue
        direction = {
            Action.BUY: Decimal("1"),
            Action.SELL: Decimal("-1"),
            Action.HOLD: Decimal("0"),
        }[result.action]
        contribution = direction * result.confidence * normalized[name]
        score += contribution
        votes.append(
            {
                "strategy": name.name,
                "family": family.value,
                "vote": result.action.value,
                "weight": str(normalized[name].quantize(Decimal("0.000001"))),
                "score": str(contribution.quantize(Decimal("0.000001"))),
            }
        )

    member_results = tuple(
        result for result in strategy_results if result.strategy in normalized
    )
    action = Action.HOLD
    agreeing: tuple[StrategyResult, ...] = ()
    if score >= _ENSEMBLE_DIRECTION_FLOOR:
        action = Action.BUY
    elif score <= -_ENSEMBLE_DIRECTION_FLOOR:
        action = Action.SELL
    if action is not Action.HOLD:
        agreeing = tuple(result for result in member_results if result.action == action)
        if not agreeing:
            # 가중합만 임계를 넘고 같은 방향 표가 하나도 없으면 진입가와
            # 손절선을 만들 근거가 없다. 조용히 방향을 주장하지 않는다.
            action = Action.HOLD
    confidence = (
        min(Decimal("1"), abs(score)) if action != Action.HOLD else Decimal("0")
    )
    return WeightedEnsembleDecision(
        family=family,
        action=action,
        score=score.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN),
        confidence=confidence.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN),
        agreeing=agreeing,
        votes=tuple(votes),
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
        decision_evidence: ExternalEvidence | Mapping[str, object],
        suggested_quantity: Decimal | str | None,
        now: datetime,
        name: str | None = None,
        regime: str = "RANGING",
        regime_detail: str = "",
        strategy_weights: Mapping[StrategyName, Decimal] | None = None,
        strategy_family: StrategyFamily = StrategyFamily.BREAKOUT,
        event_evidence: Sequence[Mapping[str, object]] = (),
        ranking: Mapping[str, object] | None = None,
        portfolio: Mapping[str, object] | None = None,
        hard_risk: Mapping[str, object] | None = None,
        strategy_promotion: Mapping[str, object] | None = None,
        ai_shadow_evidence: Mapping[str, object] | None = None,
        advisory_evidence: Sequence[Mapping[str, object]] = (),
    ) -> object:
        current = utc_datetime(now, field_name="now").replace(microsecond=0)
        normalized_symbol = symbol.strip().upper()
        normalized_market = market.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required")
        if normalized_market not in {"KRX", "US"}:
            raise ValueError("market must be KRX or US")
        typed_market = cast(Any, normalized_market)
        normalized_name = name.strip()[:200] if name and name.strip() else None
        if normalized_name == normalized_symbol:
            normalized_name = None

        decision = (
            decision_evidence
            if isinstance(decision_evidence, ExternalEvidence)
            else external_evidence_from_mapping(decision_evidence, now=current)
        )
        decision = self._validated_external(
            decision,
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
        weights = strategy_weights or {name: Decimal("0.25") for name in StrategyName}
        ensemble = compose_weighted_ensemble(
            tuple(valid_results.values()),
            weights,
            family=strategy_family,
        )
        candidate = ensemble.action if strategy_input_valid else Action.HOLD
        agreeing = ensemble.agreeing if strategy_input_valid else ()

        # ``decision``은 기술 판정(완료 일봉 Daily Setup + 장중 trigger)이다.
        # AI 검토와 뉴스는 ``advisory_evidence``로만 붙고 이 관문에 참여하지
        # 않으므로, AI 실패나 불일치가 여기서 action을 바꾸지 못한다.
        confidence = Decimal("0")
        if candidate != Action.HOLD and decision.action == candidate:
            # Daily Setup과 intraday trigger를 통과한 기술 판정이 action의 관문이다.
            # confidence는 근거 강도이지 별도의 숨은 허용/차단 기준이 아니다.
            confidence = min(ensemble.confidence, decision.confidence)
        else:
            if candidate != Action.HOLD and decision.action != candidate:
                rejected_reasons.append(
                    "technical decision evidence does not confirm the "
                    f"{ensemble.family.value} family direction"
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
        stop_price = self._level(agreeing, "stop")
        target_price = self._level(agreeing, "target")
        strategy_valid_until = min(
            (result.valid_until.astimezone(UTC) for result in valid_results.values()),
            default=current,
        )
        valid_until = min(strategy_valid_until, decision.valid_until.astimezone(UTC))
        if valid_until <= current:
            candidate = Action.HOLD
            confidence = Decimal("0")
            valid_until = current

        normalized_hard_risk = dict(
            hard_risk
            or {
                "passed": True,
                "checks": [],
                "blockedReason": None,
            }
        )
        if normalized_hard_risk.get("passed") is False:
            blocked_reason = str(
                normalized_hard_risk.get("blockedReason") or "HARD_RISK_REJECTED"
            )
            rejected_reasons.append(f"hard risk blocked: {blocked_reason}")
        normalized_strategy_promotion: dict[str, object] | None = None
        if strategy_promotion is not None:
            raw_strategy_key = strategy_promotion.get("strategyKey")
            raw_strategy_version = strategy_promotion.get("version")
            raw_artifact_fingerprint = strategy_promotion.get("artifactFingerprint")
            if not all(
                isinstance(value, str)
                for value in (
                    raw_strategy_key,
                    raw_strategy_version,
                    raw_artifact_fingerprint,
                )
            ):
                raise ValueError(
                    "strategy_promotion requires string strategyKey, version, "
                    "and artifactFingerprint"
                )
            strategy_key = cast(str, raw_strategy_key).strip()
            strategy_version = cast(str, raw_strategy_version).strip()
            artifact_fingerprint = cast(str, raw_artifact_fingerprint).strip()
            if (
                not strategy_key
                or not strategy_version
                or len(artifact_fingerprint) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in artifact_fingerprint
                )
            ):
                raise ValueError(
                    "strategy_promotion requires strategyKey/version and a "
                    "lowercase 64-hex artifactFingerprint"
                )
            normalized_strategy_promotion = {
                "title": "PAPER strategy promotion identity",
                "source": "kasset_strategy_promotion",
                "kind": "strategy_promotion",
                "strategyKey": strategy_key,
                "version": strategy_version,
                "artifactFingerprint": artifact_fingerprint,
            }

        # ai_shadow는 관측 기록이므로 기술 판정과 일치해야 할 이유가 없다.
        # 예전에는 여기서 AI action/confidence를 기술 판정과 강제 일치시켰고,
        # 그것이 AI를 사실상 PAPER 관문으로 만들었다. 이제는 형식만 검증한다.
        normalized_ai_shadow = (
            validate_selected_ai_shadow_evidence(ai_shadow_evidence)
            if ai_shadow_evidence is not None
            else None
        )

        normalized_advisory = tuple(
            self._validated_advisory(item) for item in advisory_evidence
        )

        rationale = [
            _korean_vote_rationale(valid_results),
            (
                f"기술 판정 의견은 {_ACTION_LABELS[decision.action]}이며 "
                f"신뢰도는 {decision.confidence}입니다."
            ),
        ]
        vote_by_strategy = {str(vote["strategy"]): vote for vote in ensemble.votes}
        evidence: list[Mapping[str, object]] = []
        for result in valid_results.values():
            vote = vote_by_strategy.get(result.strategy.value, {})
            evidence.append(
                {
                    "title": f"{result.strategy.value} strategy vote",
                    "source": "kasset_strategy",
                    "kind": "strategy",
                    "strategy": result.strategy.value,
                    "version": result.version,
                    "action": result.action.value,
                    "confidence": str(result.confidence),
                    "weight": vote.get("weight"),
                    "score": vote.get("score"),
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
        evidence.append(
            {
                "title": "AI trading vertical-slice review evidence",
                "source": decision.source,
                "kind": "ai_vertical_slice",
                "regime": regime,
                "regimeDetail": regime_detail,
                "strategyFamily": ensemble.family.value,
                "strategyVotes": list(ensemble.votes),
                # 앱이 이미 읽는 키다. 이제 여기 담기는 것은 AI 의견이 아니라
                # 기술 판정 근거이며, AI 의견은 kind="ai_review" 근거로 따로 붙는다.
                "aiRationale": list(decision.rationale),
                "aiEvidence": [dict(item) for item in decision.evidence],
                "eventEvidence": [dict(item) for item in event_evidence],
                "entryPrice": str(reference_price)
                if reference_price is not None
                else None,
                "stopPrice": str(stop_price) if stop_price is not None else None,
                "targetPrice": str(target_price) if target_price is not None else None,
                "ranking": dict(
                    ranking
                    or {
                        "score": str(abs(ensemble.score)),
                        "position": 1,
                        "total": 1,
                        "note": "single recommendation",
                    }
                ),
                "portfolio": dict(
                    portfolio
                    or {
                        "targetWeight": None,
                        "targetQuantity": str(quantity)
                        if quantity is not None
                        else None,
                        "cashAfter": None,
                        "note": "portfolio evidence unavailable",
                    }
                ),
                "hardRisk": normalized_hard_risk,
            }
        )
        evidence.extend(normalized_advisory)
        if normalized_ai_shadow is not None:
            evidence.append(normalized_ai_shadow)
        if normalized_strategy_promotion is not None:
            evidence.append(normalized_strategy_promotion)

        draft = RecommendationDraft(
            owner_user_id=self._owner_user_id,
            action=candidate,
            market=typed_market,
            symbol=normalized_symbol,
            name=normalized_name,
            headline=(
                f"{normalized_name or normalized_symbol} "
                f"{_ACTION_LABELS[candidate]} 검토 의견"
            ),
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
    def _validated_advisory(item: Mapping[str, object]) -> Mapping[str, object]:
        """Accept one non-gating evidence block without letting it decide.

        AI 검토, 뉴스/공시 shadow, 비교 코호트는 모두 이 경로로만 들어온다.
        형식이 깨진 근거는 조용히 버리지 않고 즉시 실패시켜 감사 원장에
        정체불명 항목이 남지 않게 한다.
        """

        if not isinstance(item, Mapping):
            raise ValueError("advisory_evidence entries must be mappings")
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("advisory_evidence entries require a non-empty kind")
        if kind in {"ai_vertical_slice", "strategy", "ai_shadow", "strategy_promotion"}:
            raise ValueError(f"advisory_evidence cannot reuse the {kind} kind")
        return dict(item)

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

    @staticmethod
    def _level(
        results: Sequence[StrategyResult],
        field_name: str,
    ) -> Decimal | None:
        levels = sorted(
            value
            for result in results
            if (value := getattr(result, field_name, None)) is not None
            and value.is_finite()
            and value > 0
        )
        if not levels:
            return None
        return levels[len(levels) // 2]


__all__ = [
    "RecommendationProducer",
    "WeightedEnsembleDecision",
    "compose_weighted_ensemble",
    "external_evidence_from_mapping",
]
