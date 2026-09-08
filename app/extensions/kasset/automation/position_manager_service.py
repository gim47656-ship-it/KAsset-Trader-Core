"""Owner-scoped PAPER holdings management that only emits recommendations."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, DecimalException
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation.intraday_data import (
    CompletedIntradayBars,
    load_completed_session_bars,
)
from app.extensions.kasset.automation.market_session import current_regular_session
from app.extensions.kasset.automation.policy import AITradingPolicyService
from app.extensions.kasset.automation.position_manager import (
    ExitKind,
    ExitLevelVersion,
    ManagedPositionState,
    PositionBar,
    PositionExitSignal,
    PositionManagerConfig,
    adopt_initial_atr,
    apply_stop_loss_floor,
    evaluate_position,
    evaluate_position_intraday,
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
from app.services.market_events.session_calendar import regular_session_bounds

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


def _intraday_position_bars(
    intraday: CompletedIntradayBars,
) -> tuple[PositionBar, ...]:
    """완료 bucket을 bucket **종료 시각** 기준 평가 봉으로 바꾼다.

    적재기는 bucket 시작 시각을 주지만, 청산 신호 시각은 그 값이 증명된 완료
    시점이어야 한다. 시작 시각을 쓰면 진입 직후 bucket이 진입 이전 신호로
    보이고, ``idempotency_key``도 아직 닫히지 않은 구간을 가리킨다.
    """

    return tuple(
        PositionBar(
            as_of=_aware_utc(bar.timestamp) + intraday.bar_interval,
            open=_decimal(bar.open),
            high=_decimal(bar.high),
            low=_decimal(bar.low),
            close=_decimal(bar.close),
            starts_at=_aware_utc(bar.timestamp),
            ends_at=_aware_utc(bar.timestamp) + intraday.bar_interval,
        )
        for bar in intraday.bars
    )


def _daily_position_bar(
    market: str,
    row: DailyCandleRow,
) -> PositionBar | None:
    """일봉 날짜 label을 실제 정규장 관측 구간으로 변환한다."""

    as_of = _aware_utc(row.time_utc)
    calendar_market: Literal["kr", "us"] = "kr" if market == "KRX" else "us"
    bounds = regular_session_bounds(calendar_market, as_of.date())
    if bounds is None:
        return None
    opens_at, closes_at = bounds
    return PositionBar(
        as_of=as_of,
        open=_decimal(row.open),
        high=_decimal(row.high),
        low=_decimal(row.low),
        close=_decimal(row.close),
        starts_at=opens_at,
        ends_at=closes_at,
    )


def _exit_level_history_from_row(
    raw: object,
) -> tuple[ExitLevelVersion, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("position state exit_level_history must be an array")
    versions: list[ExitLevelVersion] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("position state exit-level version must be an object")
        effective_raw = item.get("effectiveAt")
        if effective_raw is None:
            effective_at = None
        elif isinstance(effective_raw, str):
            effective_at = _aware_utc(datetime.fromisoformat(effective_raw))
        else:
            raise ValueError("exit-level effectiveAt must be an ISO timestamp or null")
        atr_raw = item.get("initialAtr")
        versions.append(
            ExitLevelVersion(
                effective_at=effective_at,
                initial_atr=None if atr_raw is None else _decimal(atr_raw),
                initial_stop=_decimal(item.get("initialStop")),
                current_stop=_decimal(item.get("currentStop")),
            )
        )
    return tuple(versions)


def _exit_level_history_json(
    state: ManagedPositionState,
) -> list[dict[str, str | None]]:
    return [
        {
            "effectiveAt": (
                None
                if version.effective_at is None
                else version.effective_at.isoformat()
            ),
            "initialAtr": (
                None if version.initial_atr is None else str(version.initial_atr)
            ),
            "initialStop": str(version.initial_stop),
            "currentStop": str(version.current_stop),
        }
        for version in state.exit_level_history
    ]


def _state_from_row(row: KAssetPaperPositionState) -> ManagedPositionState:
    strategy_version = (row.strategy_version or "").strip()
    if not strategy_version:
        raise ValueError("position state strategy_version is required")
    return ManagedPositionState(
        market=row.market,
        symbol=row.symbol,
        entry_price=Decimal(row.entry_price),
        initial_atr=None if row.initial_atr is None else Decimal(row.initial_atr),
        initial_stop=Decimal(row.initial_stop),
        current_stop=Decimal(row.current_stop),
        highest_close=Decimal(row.highest_close),
        partial_exit_completed=row.partial_exit_completed,
        entry_at=_aware_utc(row.opened_at),
        last_evaluated_at=(
            _aware_utc(row.last_evaluated_at)
            if row.last_evaluated_at is not None
            else None
        ),
        strategy_version=strategy_version,
        position_cycle_id=int(row.position_cycle_id),
        exit_levels_effective_at=(
            _aware_utc(row.exit_levels_effective_at)
            if row.exit_levels_effective_at is not None
            else None
        ),
        exit_level_history=_exit_level_history_from_row(row.exit_level_history),
    )


def _state_matches_position_cycle(
    row: KAssetPaperPositionState,
    *,
    owner_user_id: int,
    account_id: int,
    market: str,
    position: PaperPosition,
) -> bool:
    return (
        row.paper_position_id is not None
        and int(row.paper_position_id) == int(position.id)
        and int(row.position_cycle_id) == int(position.id)
        and int(row.owner_user_id) == owner_user_id
        and int(row.paper_account_id) == account_id
        and row.market == market
        and row.symbol == str(position.symbol)
        and row.closed_at is None
        and bool((row.strategy_version or "").strip())
    )


def _apply_state(
    row: KAssetPaperPositionState,
    state: ManagedPositionState,
    *,
    signal_key: str | None,
) -> None:
    row.initial_atr = state.initial_atr
    row.initial_stop = state.initial_stop
    row.current_stop = state.current_stop
    row.highest_close = state.highest_close
    row.partial_exit_completed = state.partial_exit_completed
    row.last_evaluated_at = state.last_evaluated_at
    row.exit_levels_effective_at = state.exit_levels_effective_at
    row.exit_level_history = _exit_level_history_json(state)
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


def _strategy_provenance_evidence(
    row: KAssetPaperPositionState,
) -> dict[str, object]:
    strategy_key = (row.strategy_key or "").strip()
    strategy_version = (row.strategy_version or "").strip()
    artifact_fingerprint = (row.strategy_fingerprint or "").strip()
    if (
        not strategy_key
        or not strategy_version
        or len(artifact_fingerprint) != 64
        or any(
            character not in "0123456789abcdef" for character in artifact_fingerprint
        )
    ):
        raise ValueError("position strategy promotion identity is incomplete")
    return {
        "title": "PAPER strategy promotion identity",
        "source": "kasset_strategy_promotion",
        "kind": "strategy_promotion",
        "strategyKey": strategy_key,
        "version": strategy_version,
        "artifactFingerprint": artifact_fingerprint,
    }


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


def _stored_exit_bar_as_of(row: AIRecommendation) -> datetime | None:
    """종료된 보호 추천이 어느 완료 bucket을 근거로 나왔는지 돌려준다."""

    for item in row.evidence or []:
        if not isinstance(item, dict) or item.get("kind") != "position_exit":
            continue
        raw = item.get("barAsOf")
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return _aware_utc(datetime.fromisoformat(raw))
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
        strategy_key: str = DEFAULT_PAPER_STRATEGY_KEY,
        strategy_version: str = DEFAULT_PAPER_STRATEGY_VERSION,
        strategy_fingerprint: str,
    ) -> None:
        normalized_key = strategy_key.strip()
        normalized_version = strategy_version.strip()
        normalized_fingerprint = strategy_fingerprint.strip()
        if not normalized_key or not normalized_version:
            raise ValueError("strategy_key and strategy_version are required")
        if len(normalized_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_fingerprint
        ):
            raise ValueError("strategy_fingerprint must be lowercase 64-hex")
        self._db = db
        self._now = _aware_utc(now).replace(microsecond=0)
        self._config = config
        self._strategy_version = normalized_version
        self._strategy_key = normalized_key
        self._strategy_fingerprint = normalized_fingerprint
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
                .with_for_update(of=PaperPosition)
            )
        ).all()
        if not position_rows:
            return ()

        by_market: dict[str, list[tuple[PaperPosition, int]]] = {"KRX": [], "US": []}
        for position, account_id in position_rows:
            market = (
                "KRX" if position.instrument_type == InstrumentType.equity_kr else "US"
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

        intraday = await self._load_intraday(by_market)

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
                            intraday=intraday.get((market, str(position.symbol))),
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

    async def _load_intraday(
        self,
        by_market: dict[str, list[tuple[PaperPosition, int]]],
    ) -> dict[tuple[str, str], CompletedIntradayBars]:
        """보유 종목의 완료 정규장 bucket을 시장별로 한 번씩 적재한다.

        진입 Trigger와 같은 공용 적재기(:func:`load_completed_session_bars`)를
        쓴다. 세션 달력은 시장당 한 번만 풀고 그 세션 객체를 종목마다 넘기므로
        tick당 외부 호출은 "보유 종목 수"로 묶인다. 정규장이 아닌 시장은 아예
        조회하지 않는다. 관심종목 목록이나 일봉 동기화 상태와 무관하게 보유
        종목만으로 대상이 정해진다.
        """

        loaded: dict[tuple[str, str], CompletedIntradayBars] = {}
        for market, positions in by_market.items():
            if not positions:
                continue
            session = current_regular_session(market, self._now)
            if session is None:
                continue
            for position, _account_id in positions:
                symbol = str(position.symbol)
                result = await load_completed_session_bars(
                    symbol=symbol,
                    # by_market 구성이 KRX/US만 만든다.
                    market=cast(Literal["KRX", "US"], market),
                    as_of=self._now,
                    session=session,
                )
                if isinstance(result, CompletedIntradayBars):
                    loaded[(market, symbol)] = result
                    continue
                logger.info(
                    (
                        "PAPER 포지션 장중 평가 데이터를 쓰지 않습니다: "
                        "market=%s symbol=%s reason=%s"
                    ),
                    market,
                    symbol,
                    result.blocked_reason,
                )
        return loaded

    async def _manage_position(
        self,
        *,
        owner_user_id: int,
        account_id: int,
        market: str,
        position: PaperPosition,
        rows: list[DailyCandleRow],
        intraday: CompletedIntradayBars | None = None,
    ) -> str | None:
        ordered = sorted(rows, key=lambda item: item.time_utc)
        completed_daily: list[tuple[DailyCandleRow, PositionBar]] = []
        for row in ordered:
            bar = _daily_position_bar(market, row)
            if bar is None or bar.ends_at is None or bar.ends_at > self._now:
                continue
            completed_daily.append((row, bar))
        latest_completed_at = (
            completed_daily[-1][1].ends_at if completed_daily else None
        )
        # future/current-session daily rows are labels, not completed observations.
        # ATR, trend and exits use only bars whose real session close has passed.
        daily_usable = (
            latest_completed_at is not None
            and self._now - latest_completed_at <= _MAX_BAR_AGE
        )

        position_id = int(position.id)
        if position_id < 1:
            raise ValueError("position_cycle_id must be positive")
        state_row = await self._db.scalar(
            select(KAssetPaperPositionState)
            .where(
                KAssetPaperPositionState.paper_position_id == position_id,
                KAssetPaperPositionState.closed_at.is_(None),
            )
            .with_for_update()
        )
        if state_row is not None and int(state_row.position_cycle_id) != position_id:
            state_row.paper_position_id = None
            state_row.closed_at = self._now
            await self._db.flush()
            state_row = None
        state_matches = state_row is not None and _state_matches_position_cycle(
            state_row,
            owner_user_id=owner_user_id,
            account_id=account_id,
            market=market,
            position=position,
        )
        filled_average_price = Decimal(position.avg_price)
        completed_rows = [row for row, _bar in completed_daily]
        atr = _average_true_range(completed_rows) if daily_usable else None
        if not state_matches:
            opened_at = _aware_utc(position.created_at)
            state = apply_stop_loss_floor(
                initialize_position(
                    market=market,
                    symbol=str(position.symbol),
                    entry_price=filled_average_price,
                    initial_atr=atr,
                    entry_at=opened_at,
                    strategy_version=self._strategy_version,
                    position_cycle_id=position_id,
                    exit_levels_effective_at=self._now,
                    config=self._config,
                ),
                filled_average_price=filled_average_price,
                effective_at=self._now,
            )
            if state_row is None:
                state_row = KAssetPaperPositionState(position_cycle_id=position_id)
                self._db.add(state_row)
            state_row.paper_position_id = position_id
            state_row.owner_user_id = owner_user_id
            state_row.paper_account_id = account_id
            state_row.market = state.market
            state_row.symbol = state.symbol
            state_row.entry_price = state.entry_price
            state_row.initial_atr = state.initial_atr
            state_row.initial_stop = state.initial_stop
            state_row.current_stop = state.current_stop
            state_row.exit_levels_effective_at = state.exit_levels_effective_at
            state_row.exit_level_history = _exit_level_history_json(state)
            state_row.highest_close = state.highest_close
            state_row.partial_exit_completed = False
            state_row.opened_at = state.entry_at
            state_row.closed_at = None
            state_row.last_evaluated_at = None
            state_row.last_exit_signal_key = None
            state_row.strategy_key = self._strategy_key
            state_row.strategy_version = state.strategy_version
            state_row.strategy_fingerprint = self._strategy_fingerprint
        else:
            # 이 snapshot으로 미처리 과거 관측을 먼저 평가한다. 새 floor/ATR은
            # 아래 replay가 끝난 뒤 _now version으로 append한다.
            state = _state_from_row(state_row)
            stored_strategy_key = (state_row.strategy_key or "").strip()
            if (
                not stored_strategy_key
                and state.strategy_version == self._strategy_version
            ):
                state_row.strategy_key = self._strategy_key
                stored_strategy_key = self._strategy_key
            if (
                not (state_row.strategy_fingerprint or "").strip()
                and stored_strategy_key == self._strategy_key
                and state.strategy_version == self._strategy_version
            ):
                state_row.strategy_fingerprint = self._strategy_fingerprint

        # ``last_exit_signal_key``는 "이 사이클이 마지막으로 만든 보호 추천"을
        # 가리킨다. 종료된 뒤에도 그 참조를 지운다면 재시도 경계를 재시작에서
        # 잃어버리므로, pending 해제와 참조 보존을 분리한다.
        stored_signal_key = state_row.last_exit_signal_key
        pending_kind: ExitKind | None = None
        pending_active = False
        intraday_retry_after: datetime | None = None
        previous_recommendation: AIRecommendation | None = None
        if stored_signal_key is not None:
            previous_recommendation = await self._db.scalar(
                select(AIRecommendation)
                .where(AIRecommendation.id == stored_signal_key)
                .with_for_update()
            )
            if previous_recommendation is not None:
                previous_kind = _stored_exit_kind(previous_recommendation)
                previous_status = previous_recommendation.paper_execution_status
                try:
                    expired = (
                        _aware_utc(previous_recommendation.valid_until) <= self._now
                    )
                except (TypeError, ValueError):
                    expired = True
                if previous_status == "SUCCEEDED":
                    if previous_kind is ExitKind.PARTIAL_SELL:
                        state = replace(state, partial_exit_completed=True)
                elif previous_status == "CLAIMED":
                    # 집행이 진행 중이거나 lease 복구를 기다리는 상태다. 주문을
                    # 취소하거나 새 CAS를 만들지 않고, claim 결과가 화해된 다음
                    # tick에서 최신 잔량으로 다시 산정한다. 유효기간이 지났어도
                    # 결과를 모르는 채로 두 번째 청산을 만들어서는 안 된다.
                    pending_active = True
                    pending_kind = previous_kind
                elif (
                    previous_recommendation.decision == "REJECTED"
                    or previous_status == "FAILED"
                    or expired
                ):
                    # 종료됐으니 대기는 풀되, 그 추천이 근거로 삼은 bucket까지는
                    # 다시 판정하지 않는다. 같은 bucket은 같은 결정론 id로
                    # 수렴해 그날 재시도가 영구히 막히기 때문이다.
                    intraday_retry_after = _stored_exit_bar_as_of(
                        previous_recommendation
                    )
                else:
                    pending_active = True
                    pending_kind = previous_kind
            _apply_state(
                state_row,
                state,
                signal_key=stored_signal_key,
            )
        signal: PositionExitSignal | None = None
        persisted_state = state
        exit_horizon = "intraday"
        # 미처리 일봉을 최신 한 건으로 건너뛰지 않고 시간순으로 replay한다.
        # 앞선 partial은 보존하되, 뒤에 이미 발생한 전량 exit가 있으면 그것이
        # 우선한다. partial state는 PAPER 집행 성공 전에는 확정하지 않는다.
        if daily_usable:
            daily_partial: PositionExitSignal | None = None
            for index, (_row, bar) in enumerate(completed_daily):
                row_at = bar.as_of
                if row_at <= persisted_state.entry_at or (
                    persisted_state.last_evaluated_at is not None
                    and row_at <= persisted_state.last_evaluated_at
                ):
                    continue
                bars_held = sum(
                    persisted_state.entry_at < _aware_utc(candidate.time_utc) <= row_at
                    for candidate in completed_rows
                )
                evaluation = evaluate_position(
                    persisted_state,
                    bar,
                    bars_held=max(1, bars_held),
                    trend_intact=_trend_intact(completed_rows[: index + 1]),
                    config=self._config,
                )
                candidate_signal = evaluation.signal
                persisted_state = _persistable_state(
                    persisted_state,
                    evaluation.state,
                    candidate_signal.kind if candidate_signal is not None else None,
                )
                if candidate_signal is None:
                    continue
                if candidate_signal.kind is ExitKind.PARTIAL_SELL:
                    if daily_partial is None:
                        daily_partial = candidate_signal
                    continue
                signal = candidate_signal
                exit_horizon = "daily"
                break
            if signal is None and daily_partial is not None:
                signal = daily_partial
                exit_horizon = "daily"
        if intraday is not None and (
            signal is None or signal.kind is ExitKind.PARTIAL_SELL
        ):
            intraday_signal = evaluate_position_intraday(
                persisted_state,
                _intraday_position_bars(intraday),
                bar_interval=intraday.bar_interval,
                after=intraday_retry_after,
                config=self._config,
            )
            if intraday_signal is not None and (
                signal is None or intraday_signal.kind is not ExitKind.PARTIAL_SELL
            ):
                signal = intraday_signal
                exit_horizon = "intraday"

        # Full exit는 강화 전 snapshot에서 이미 성립한 더 이른 사실이므로 먼저
        # 내보낸다. 그 외에는 no-data여도 새 floor/ATR을 즉시 _now부터 활성화하고
        # 이전 version을 history에 남겨 늦은 관측의 old-stop crossing을 보존한다.
        if signal is None or signal.kind is ExitKind.PARTIAL_SELL:
            persisted_state = apply_stop_loss_floor(
                persisted_state,
                filled_average_price=filled_average_price,
                effective_at=self._now,
            )
            if persisted_state.initial_atr is None and atr is not None:
                persisted_state = adopt_initial_atr(
                    persisted_state,
                    initial_atr=atr,
                    effective_at=self._now,
                    config=self._config,
                )
        if signal is None:
            _apply_state(
                state_row,
                persisted_state,
                signal_key=stored_signal_key,
            )
            return None
        if (
            pending_active
            and pending_kind is ExitKind.PARTIAL_SELL
            and signal.kind is not ExitKind.PARTIAL_SELL
            and previous_recommendation is not None
            and previous_recommendation.paper_execution_status is None
        ):
            previous_recommendation.valid_until = self._now
            previous_recommendation.updated_at = self._now
            pending_active = False
        if pending_active:
            # 대기 중인 보호 추천이 아직 종료되지 않았다. 같은 보유에 두 번째
            # 청산 의도를 만들지 않고 그 결과를 먼저 기다린다.
            _apply_state(
                state_row,
                persisted_state,
                signal_key=stored_signal_key,
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
                signal_key=stored_signal_key,
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
        exit_evidence: dict[str, object] = {
            "title": "Deterministic PAPER position exit",
            "source": "position_manager",
            "kind": "position_exit",
            "exitKind": signal.kind.value,
            "idempotencyKey": recommendation_id,
            "paperPositionId": position_id,
            "positionCycleId": state.position_cycle_id,
            "quantityFraction": str(signal.quantity_fraction),
            "initialAtr": (
                None if signal.initial_atr is None else str(signal.initial_atr)
            ),
            "initialStop": str(signal.initial_stop),
            "currentStop": str(signal.current_stop),
            "evaluationHorizon": exit_horizon,
            "barAsOf": signal.signal_at.isoformat(),
        }
        if exit_horizon == "intraday" and intraday is not None:
            # 장중 청산은 "어느 완료 bucket이 언제까지의 값이었는지"가 증거다.
            exit_evidence.update(
                {
                    "barPeriod": intraday.period,
                    "barSource": intraday.source,
                    "dataAsOf": intraday.data_as_of.isoformat(),
                }
            )
        evidence: list[dict[str, object]] = [
            exit_evidence,
            {
                "title": "PAPER exit Hard Risk",
                "source": "kasset_hard_risk",
                "kind": "hard_risk",
                **hard_risk.as_evidence(),
            },
        ]
        evidence.append(_strategy_provenance_evidence(state_row))
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
            evidence=evidence,
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
