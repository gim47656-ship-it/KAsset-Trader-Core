"""Owner-scoped PAPER holdings management that only emits recommendations."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, DecimalException

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation.policy import AITradingPolicyService
from app.extensions.kasset.automation.position_manager import (
    ExitKind,
    ManagedPositionState,
    PositionBar,
    PositionManagerConfig,
    evaluate_position,
    initialize_position,
)
from app.extensions.kasset.automation.strategy_promotion import (
    DEFAULT_PAPER_STRATEGY_KEY,
    DEFAULT_PAPER_STRATEGY_VERSION,
)
from app.extensions.kasset.models import (
    AndroidPaperAccount,
    KAssetPaperPositionState,
)
from app.models.ai_recommendations import (
    AIRecommendation,
    RecommendationDecision,
)
from app.models.paper_trading import PaperPosition
from app.models.trading import InstrumentType
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)

logger = logging.getLogger(__name__)

_ATR_PERIOD = 14
_HISTORY_BARS = 40
_TREND_WINDOW = 20
_SIGNAL_LIFETIME = timedelta(days=4)
_MAX_BAR_AGE = timedelta(days=4)
_ZERO = Decimal("0")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("position-manager timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _decimal(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("position-manager price must be finite")
    return result


def _average_true_range(rows: list[DailyCandleRow]) -> Decimal | None:
    ordered = sorted(rows, key=lambda item: item.time_utc)
    if len(ordered) < _ATR_PERIOD + 1:
        return None
    true_ranges: list[Decimal] = []
    previous_close = _decimal(ordered[0].close)
    for row in ordered[1:]:
        high = _decimal(row.high)
        low = _decimal(row.low)
        true_ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
        previous_close = _decimal(row.close)
    result = sum(true_ranges[-_ATR_PERIOD:], _ZERO) / Decimal(_ATR_PERIOD)
    return result if result > _ZERO else None


def _trend_intact(rows: list[DailyCandleRow]) -> bool:
    ordered = sorted(rows, key=lambda item: item.time_utc)
    if len(ordered) < _TREND_WINDOW:
        # Missing long-horizon trend evidence must not manufacture a liquidation.
        return True
    closes = [_decimal(row.close) for row in ordered[-_TREND_WINDOW:]]
    return closes[-1] >= sum(closes, _ZERO) / Decimal(_TREND_WINDOW)


def _state_from_row(row: KAssetPaperPositionState) -> ManagedPositionState:
    return ManagedPositionState(
        market=row.market,
        symbol=row.symbol,
        entry_price=Decimal(row.entry_price),
        initial_atr=Decimal(row.initial_atr),
        initial_stop=Decimal(row.initial_stop),
        current_stop=Decimal(row.current_stop),
        highest_close=Decimal(row.highest_close),
        partial_exit_completed=row.partial_exit_completed,
        entry_at=_aware_utc(row.entry_at),
        last_evaluated_at=(
            _aware_utc(row.last_evaluated_at)
            if row.last_evaluated_at is not None
            else None
        ),
        strategy_version=row.strategy_version,
    )


def _apply_state(
    row: KAssetPaperPositionState,
    state: ManagedPositionState,
    *,
    signal_key: str | None,
) -> None:
    row.initial_stop = state.initial_stop
    row.current_stop = state.current_stop
    row.highest_close = state.highest_close
    row.partial_exit_completed = state.partial_exit_completed
    row.last_evaluated_at = state.last_evaluated_at
    row.last_exit_signal_key = signal_key


def _quantity_for_signal(
    *,
    market: str,
    held_quantity: Decimal,
    fraction: Decimal,
) -> Decimal:
    raw = max(_ZERO, min(held_quantity, held_quantity * fraction))
    quantum = Decimal("1") if market == "KRX" else Decimal("0.0001")
    return raw.quantize(quantum, rounding=ROUND_DOWN)


def position_recommendation_id(signal_key: str, owner_user_id: int) -> str:
    normalized = signal_key.strip()
    if not normalized or owner_user_id < 1:
        raise ValueError("signal_key and positive owner_user_id are required")
    return f"{normalized}:{owner_user_id}"


def _persistable_state(
    previous: ManagedPositionState,
    evaluated: ManagedPositionState,
    signal_kind: ExitKind | None,
) -> ManagedPositionState:
    if signal_kind is ExitKind.PARTIAL_SELL and not previous.partial_exit_completed:
        return replace(evaluated, partial_exit_completed=False)
    return evaluated


def _stored_exit_kind(row: AIRecommendation) -> ExitKind | None:
    for item in row.evidence or []:
        if not isinstance(item, dict) or item.get("kind") != "position_exit":
            continue
        try:
            return ExitKind(str(item.get("exitKind")))
        except ValueError:
            return None
    return None


class PaperPositionManagerService:
    """Manage current owner holdings before new candidates; never calls a broker."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        now: datetime,
        config: PositionManagerConfig = PositionManagerConfig(),
        strategy_version: str = DEFAULT_PAPER_STRATEGY_VERSION,
    ) -> None:
        normalized_version = strategy_version.strip()
        if not normalized_version:
            raise ValueError("strategy_version is required")
        self._db = db
        self._now = _aware_utc(now).replace(microsecond=0)
        self._config = config
        self._strategy_version = normalized_version
        self._policy = AITradingPolicyService()

    async def run_owner(self, owner_user_id: int) -> tuple[str, ...]:
        position_rows = (
            await self._db.execute(
                select(PaperPosition, AndroidPaperAccount.paper_account_id)
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
        if not position_rows:
            return ()

        by_market: dict[str, list[tuple[PaperPosition, int]]] = {"KRX": [], "US": []}
        for position, account_id in position_rows:
            market = (
                "KRX"
                if position.instrument_type == InstrumentType.equity_kr
                else "US"
            )
            by_market[market].append((position, int(account_id)))

        repository = DailyCandlesRepository(session=self._db)
        candles: dict[tuple[str, str], list[DailyCandleRow]] = {}
        for market, positions in by_market.items():
            if not positions:
                continue
            rows = await repository.fetch_recent_batch(
                market=MarketKey.KR if market == "KRX" else MarketKey.US,
                symbols=[str(position.symbol) for position, _ in positions],
                partition="KRX" if market == "KRX" else None,
                count=_HISTORY_BARS,
            )
            candles.update(
                ((market, symbol), list(symbol_rows))
                for symbol, symbol_rows in rows.items()
            )

        created: list[str] = []
        for market in ("KRX", "US"):
            for position, account_id in by_market[market]:
                try:
                    async with self._db.begin_nested():
                        recommendation_id = await self._manage_position(
                            owner_user_id=owner_user_id,
                            account_id=account_id,
                            market=market,
                            position=position,
                            rows=candles.get((market, str(position.symbol)), []),
                        )
                except (DecimalException, TypeError, ValueError) as exc:
                    logger.warning(
                        (
                            "PAPER 포지션 관리 데이터 오류를 건너뜁니다: "
                            "owner=%s market=%s symbol=%s exception=%s"
                        ),
                        owner_user_id,
                        market,
                        position.symbol,
                        type(exc).__name__,
                    )
                    continue
                if recommendation_id is not None:
                    created.append(recommendation_id)
        await self._db.commit()
        return tuple(created)

    async def _manage_position(
        self,
        *,
        owner_user_id: int,
        account_id: int,
        market: str,
        position: PaperPosition,
        rows: list[DailyCandleRow],
    ) -> str | None:
        ordered = sorted(rows, key=lambda item: item.time_utc)
        if not ordered:
            return None
        latest = ordered[-1]
        latest_at = _aware_utc(latest.time_utc)
        if latest_at > self._now or self._now - latest_at > _MAX_BAR_AGE:
            return None

        state_row = await self._db.scalar(
            select(KAssetPaperPositionState)
            .where(
                KAssetPaperPositionState.owner_user_id == owner_user_id,
                KAssetPaperPositionState.paper_account_id == account_id,
                KAssetPaperPositionState.symbol == str(position.symbol),
            )
            .with_for_update()
        )
        if state_row is None:
            atr = _average_true_range(ordered)
            if atr is None:
                return None
            entry_at = _aware_utc(position.created_at)
            state = initialize_position(
                market=market,
                symbol=str(position.symbol),
                entry_price=Decimal(position.avg_price),
                initial_atr=atr,
                entry_at=entry_at,
                strategy_version=self._strategy_version,
                config=self._config,
            )
            state_row = KAssetPaperPositionState(
                owner_user_id=owner_user_id,
                paper_account_id=account_id,
                symbol=state.symbol,
                market=state.market,
                entry_price=state.entry_price,
                initial_atr=state.initial_atr,
                initial_stop=state.initial_stop,
                current_stop=state.current_stop,
                highest_close=state.highest_close,
                partial_exit_completed=False,
                entry_at=state.entry_at,
                last_evaluated_at=None,
                last_exit_signal_key=None,
                strategy_version=state.strategy_version,
            )
            self._db.add(state_row)
        else:
            state = _state_from_row(state_row)

        pending_recommendation_id = state_row.last_exit_signal_key
        pending_kind: ExitKind | None = None
        pending_active = False
        if pending_recommendation_id is not None:
            previous_recommendation = await self._db.get(
                AIRecommendation,
                pending_recommendation_id,
            )
            if previous_recommendation is not None:
                previous_kind = _stored_exit_kind(previous_recommendation)
                try:
                    expired = (
                        _aware_utc(previous_recommendation.valid_until) <= self._now
                    )
                except (TypeError, ValueError):
                    expired = True
                if previous_recommendation.paper_execution_status == "SUCCEEDED":
                    if previous_kind is ExitKind.PARTIAL_SELL:
                        state = replace(state, partial_exit_completed=True)
                elif (
                    previous_recommendation.decision == "REJECTED"
                    or previous_recommendation.paper_execution_status == "FAILED"
                    or expired
                ):
                    pass
                else:
                    pending_active = True
                    pending_kind = previous_kind
            if not pending_active:
                pending_recommendation_id = None
            _apply_state(
                state_row,
                state,
                signal_key=pending_recommendation_id,
            )
        if state.last_evaluated_at is not None and latest_at <= state.last_evaluated_at:
            return None
        if latest_at <= state.entry_at:
            return None

        bar = PositionBar(
            as_of=latest_at,
            open=_decimal(latest.open),
            high=_decimal(latest.high),
            low=_decimal(latest.low),
            close=_decimal(latest.close),
        )
        bars_held = sum(
            state.entry_at < _aware_utc(row.time_utc) <= latest_at
            for row in ordered
        )
        evaluation = evaluate_position(
            state,
            bar,
            bars_held=max(1, bars_held),
            trend_intact=_trend_intact(ordered),
            config=self._config,
        )
        signal = evaluation.signal
        signal_kind = signal.kind if signal is not None else None
        persisted_state = _persistable_state(state, evaluation.state, signal_kind)
        if signal is None:
            _apply_state(
                state_row,
                persisted_state,
                signal_key=pending_recommendation_id,
            )
            return None
        if pending_active and (
            pending_kind is not ExitKind.PARTIAL_SELL
            or signal.kind is ExitKind.PARTIAL_SELL
        ):
            _apply_state(
                state_row,
                persisted_state,
                signal_key=pending_recommendation_id,
            )
            return None
        quantity = _quantity_for_signal(
            market=market,
            held_quantity=Decimal(position.quantity),
            fraction=signal.quantity_fraction,
        )
        if quantity <= _ZERO:
            _apply_state(
                state_row,
                persisted_state,
                signal_key=pending_recommendation_id,
            )
            return None
        recommendation_id = position_recommendation_id(
            signal.idempotency_key,
            owner_user_id,
        )
        _apply_state(
            state_row,
            persisted_state,
            signal_key=recommendation_id,
        )
        existing = await self._db.get(AIRecommendation, recommendation_id)
        if existing is not None:
            return None
        hard_risk = await self._policy.evaluate_hard_risk(
            self._db,
            owner_user_id,
            action="SELL",
            market=market,
            symbol=str(position.symbol),
            quantity=quantity,
            reference_price=signal.reference_price,
            ai_confidence=Decimal("1"),
            now=self._now,
        )
        failed = [check.detail for check in hard_risk.checks if not check.passed]
        row = AIRecommendation(
            id=recommendation_id,
            owner_user_id=owner_user_id,
            action="SELL",
            decision=RecommendationDecision.PENDING.value,
            market=market,
            symbol=str(position.symbol),
            name=None,
            currency="KRW" if market == "KRX" else "USD",
            headline=f"{position.symbol} {signal.kind.value} 청산 검토",
            rationale=[signal.reason],
            risks=failed,
            evidence=[
                {
                    "title": "Deterministic PAPER position exit",
                    "source": "position_manager",
                    "kind": "position_exit",
                    "exitKind": signal.kind.value,
                    "idempotencyKey": recommendation_id,
                    "quantityFraction": str(signal.quantity_fraction),
                    "initialAtr": str(state.initial_atr),
                    "initialStop": str(state.initial_stop),
                    "currentStop": str(state.current_stop),
                    "barAsOf": bar.as_of.isoformat(),
                },
                {
                    "title": "PAPER exit Hard Risk",
                    "source": "kasset_hard_risk",
                    "kind": "hard_risk",
                    **hard_risk.as_evidence(),
                },
                {
                    "title": "PAPER strategy promotion identity",
                    "source": "kasset_strategy_promotion",
                    "kind": "strategy_promotion",
                    "strategyKey": DEFAULT_PAPER_STRATEGY_KEY,
                    "version": state.strategy_version,
                },
            ],
            confidence="1",
            reference_price=str(signal.reference_price),
            suggested_quantity=str(quantity),
            source="kasset-automation",
            created_at=self._now,
            valid_until=self._now + _SIGNAL_LIFETIME,
            updated_at=self._now,
        )
        self._db.add(row)
        await self._db.flush()
        return row.id
