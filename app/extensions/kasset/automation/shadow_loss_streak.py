"""닫힌 PAPER 거래 사실로 BUY 손실 연속 잠금을 관찰하는 SHADOW 계약."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.models import KAssetShadowLossLock

SHADOW_LOSS_STREAK_SCHEMA_VERSION = "kasset.shadow-loss-streak.v1"
SHADOW_LOSS_STREAK_EVIDENCE_VERSION = "kasset.shadow-loss-streak-evidence.v1"
SHADOW_LOSS_STREAK_CONFIG_SCHEMA_VERSION = "kasset.shadow-loss-streak-config.v1"
SHADOW_MODE: Literal["SHADOW"] = "SHADOW"

_ZERO = Decimal("0")
_GLOBAL_SYMBOL_KEY = ""


class ShadowLossStatus(StrEnum):
    VALID = "valid"
    INSUFFICIENT = "insufficient"
    FAIL_CLOSED = "fail-closed"


class ShadowLockScope(StrEnum):
    GLOBAL = "GLOBAL"
    SYMBOL = "SYMBOL"


class ShadowLossReason(StrEnum):
    LIMIT_REACHED = "LOSS_STREAK_LIMIT_REACHED"
    BELOW_LIMIT = "LOSS_STREAK_BELOW_LIMIT"
    EXPIRED = "LOSS_STREAK_EXPIRED"
    INSUFFICIENT = "INSUFFICIENT_CLOSED_PAPER_FACTS"
    INVALID_SCOPE = "INVALID_EVALUATION_SCOPE"
    INVALID_TIMESTAMP = "INVALID_FACT_TIMESTAMP"
    INVALID_FACT_SCHEMA = "INVALID_FACT_SCHEMA"
    ORDERING_COLLISION = "FACT_ORDERING_COLLISION"


@dataclass(frozen=True, slots=True)
class ShadowLossStreakConfig:
    """활성 정책과 독립적으로 지문을 만드는 불변 SHADOW 설정."""

    stop_loss_reasons: tuple[str, ...]
    loss_limit: int
    lookback: timedelta
    lock_duration: timedelta
    emit_global_evidence: bool = True
    emit_symbol_evidence: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.stop_loss_reasons, tuple) or any(
            not isinstance(reason, str) for reason in self.stop_loss_reasons
        ):
            raise ValueError("stop_loss_reasons must be a tuple of strings")
        normalized = tuple(
            sorted({_normalize_reason(reason) for reason in self.stop_loss_reasons})
        )
        if not normalized or any(not reason for reason in normalized):
            raise ValueError("stop_loss_reasons must contain nonempty values")
        if (
            isinstance(self.loss_limit, bool)
            or not isinstance(self.loss_limit, int)
            or self.loss_limit < 1
        ):
            raise ValueError("loss_limit must be a positive integer")
        if not isinstance(self.lookback, timedelta) or self.lookback <= timedelta(0):
            raise ValueError("lookback must be positive")
        if (
            not isinstance(self.lock_duration, timedelta)
            or self.lock_duration <= timedelta(0)
        ):
            raise ValueError("lock_duration must be positive")
        if not isinstance(self.emit_global_evidence, bool) or not isinstance(
            self.emit_symbol_evidence, bool
        ):
            raise ValueError("evidence emission flags must be bool")
        object.__setattr__(self, "stop_loss_reasons", normalized)

    def as_serializable(self) -> dict[str, object]:
        return {
            "configSchemaVersion": SHADOW_LOSS_STREAK_CONFIG_SCHEMA_VERSION,
            "stopLossReasons": list(self.stop_loss_reasons),
            "lossLimit": self.loss_limit,
            "lookbackMicroseconds": _timedelta_microseconds(self.lookback),
            "lockDurationMicroseconds": _timedelta_microseconds(
                self.lock_duration
            ),
            "emission": {
                "globalEvidenceEnabled": self.emit_global_evidence,
                "symbolEvidenceEnabled": self.emit_symbol_evidence,
            },
        }

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.as_serializable(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class ShadowPaperTradeFact:
    """호출자가 명시적으로 공급하는 하나의 닫힌 PAPER 거래 사실."""

    id: int
    transaction_id: str
    trade_id: str
    owner_user_id: int
    account_key: str
    market: str
    symbol: str
    side: str
    lifecycle_status: str
    realized_pnl: Decimal | None
    exit_reason: str | None
    executed_at: datetime
    account_mode: Literal["PAPER"] | str = "PAPER"


@dataclass(frozen=True, slots=True)
class ShadowLossLockObservation:
    schema_version: str
    evidence_version: str
    mode: Literal["SHADOW"]
    status: ShadowLossStatus
    scope: ShadowLockScope
    symbol: str | None
    owner_user_id: int
    account_key: str
    market: str
    evaluated_at: datetime
    source_timestamps: tuple[datetime, ...]
    config_fingerprint: str
    streak_count: int
    loss_limit: int
    newest_loss_id: int | None
    newest_loss_transaction_id: str | None
    newest_loss_trade_id: str | None
    newest_loss_at: datetime | None
    expires_at: datetime | None
    buy_locked: bool
    reason: ShadowLossReason
    sell_allowed: bool
    position_manager_risk_reduction_allowed: bool

    def as_evidence(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "evidenceVersion": self.evidence_version,
            "mode": self.mode,
            "status": self.status.value,
            "scope": {
                "kind": self.scope.value,
                "symbol": self.symbol,
                "ownerUserId": self.owner_user_id,
                "accountKey": self.account_key,
                "market": self.market,
            },
            "sourceTimestamps": [
                _timestamp_text(item) for item in self.source_timestamps
            ],
            "evaluatedAt": _timestamp_text(self.evaluated_at),
            "shadowConfigFingerprint": self.config_fingerprint,
            "streak": {
                "count": self.streak_count,
                "limit": self.loss_limit,
                "newestLossId": self.newest_loss_id,
                "newestLossTransactionId": self.newest_loss_transaction_id,
                "newestLossTradeId": self.newest_loss_trade_id,
                "newestLossAt": _timestamp_text(self.newest_loss_at),
            },
            "hypotheticalLock": {
                "buyLocked": self.buy_locked,
                "expiresAt": _timestamp_text(self.expires_at),
                "reason": self.reason.value,
                "sellAllowed": self.sell_allowed,
                "positionManagerRiskReductionAllowed": (
                    self.position_manager_risk_reduction_allowed
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class ShadowLossLockState:
    """재시작 시 복구되는 현재 SHADOW 관찰 상태."""

    owner_user_id: int
    account_key: str
    market: str
    scope: ShadowLockScope
    symbol: str | None
    evaluated_at: datetime
    streak_count: int
    loss_limit: int
    newest_loss_id: int | None
    newest_loss_transaction_id: str | None
    newest_loss_trade_id: str | None
    newest_loss_at: datetime | None
    expires_at: datetime | None
    buy_locked: bool
    reason: ShadowLossReason
    config_fingerprint: str
    schema_version: str
    evidence_version: str
    mode: Literal["SHADOW"] = SHADOW_MODE
    status: ShadowLossStatus = ShadowLossStatus.VALID

    @classmethod
    def from_observation(
        cls, observation: ShadowLossLockObservation
    ) -> ShadowLossLockState:
        return cls(
            owner_user_id=observation.owner_user_id,
            account_key=observation.account_key,
            market=observation.market,
            scope=observation.scope,
            symbol=observation.symbol,
            evaluated_at=observation.evaluated_at,
            streak_count=observation.streak_count,
            loss_limit=observation.loss_limit,
            newest_loss_id=observation.newest_loss_id,
            newest_loss_transaction_id=(
                observation.newest_loss_transaction_id
            ),
            newest_loss_trade_id=observation.newest_loss_trade_id,
            newest_loss_at=observation.newest_loss_at,
            expires_at=observation.expires_at,
            buy_locked=observation.buy_locked,
            reason=observation.reason,
            config_fingerprint=observation.config_fingerprint,
            schema_version=observation.schema_version,
            evidence_version=observation.evidence_version,
        )

    def as_evidence(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "evidenceVersion": self.evidence_version,
            "mode": self.mode,
            "status": self.status.value,
            "scope": {
                "kind": self.scope.value,
                "symbol": self.symbol,
                "ownerUserId": self.owner_user_id,
                "accountKey": self.account_key,
                "market": self.market,
            },
            "sourceTimestamps": {
                "evaluatedAt": _timestamp_text(self.evaluated_at),
                "newestLossAt": _timestamp_text(self.newest_loss_at),
                "expiresAt": _timestamp_text(self.expires_at),
            },
            "shadowConfigFingerprint": self.config_fingerprint,
            "streakCount": self.streak_count,
            "lossLimit": self.loss_limit,
            "newestLossId": self.newest_loss_id,
            "newestLossTransactionId": self.newest_loss_transaction_id,
            "newestLossTradeId": self.newest_loss_trade_id,
            "reason": self.reason.value,
            "hypotheticalBuyLocked": self.buy_locked,
            "sellAllowed": True,
            "positionManagerRiskReductionAllowed": True,
        }


@dataclass(frozen=True, slots=True)
class ShadowLossStreakResult:
    schema_version: str
    evidence_version: str
    mode: Literal["SHADOW"]
    status: ShadowLossStatus
    owner_user_id: int
    account_key: str
    market: str
    symbol: str
    evaluated_at: datetime
    source_timestamps: tuple[datetime, ...]
    config: ShadowLossStreakConfig
    config_fingerprint: str
    global_lock: ShadowLossLockObservation
    symbol_lock: ShadowLossLockObservation

    @property
    def effective_buy_locked(self) -> bool:
        return self.global_lock.buy_locked or self.symbol_lock.buy_locked

    @property
    def persistent_states(self) -> tuple[ShadowLossLockState, ...]:
        if self.status is not ShadowLossStatus.VALID:
            return ()
        return (
            ShadowLossLockState.from_observation(self.global_lock),
            ShadowLossLockState.from_observation(self.symbol_lock),
        )

    def as_evidence(self) -> dict[str, object]:
        emitted: list[dict[str, object]] = []
        if self.config.emit_global_evidence:
            emitted.append(self.global_lock.as_evidence())
        if self.config.emit_symbol_evidence:
            emitted.append(self.symbol_lock.as_evidence())
        return {
            "schemaVersion": self.schema_version,
            "evidenceVersion": self.evidence_version,
            "mode": self.mode,
            "status": self.status.value,
            "scope": {
                "ownerUserId": self.owner_user_id,
                "accountKey": self.account_key,
                "market": self.market,
                "symbol": self.symbol,
            },
            "sourceTimestamps": [
                _timestamp_text(item) for item in self.source_timestamps
            ],
            "evaluatedAt": _timestamp_text(self.evaluated_at),
            "ordering": {
                "fields": ["executedAt", "id"],
                "direction": "ascending",
                "deduplicateBy": ["transactionId", "tradeId"],
            },
            "shadowConfig": {
                "fingerprint": self.config_fingerprint,
                "config": self.config.as_serializable(),
            },
            "hypothetical": {
                "globalBuyLocked": self.global_lock.buy_locked,
                "symbolBuyLocked": self.symbol_lock.buy_locked,
                "effectiveBuyLocked": self.effective_buy_locked,
                "sellAllowed": True,
                "positionManagerRiskReductionAllowed": True,
            },
            "lockEvidence": emitted,
        }


class ConcurrentShadowLossLockUpdate(RuntimeError):
    """더 최신인 SHADOW 상태를 오래된 관찰로 덮으려 한 경우."""


def evaluate_shadow_loss_streak(
    facts: Sequence[ShadowPaperTradeFact],
    *,
    owner_user_id: int,
    account_key: str,
    market: str,
    symbol: str,
    evaluated_at: datetime,
    config: ShadowLossStreakConfig,
) -> ShadowLossStreakResult:
    """실제 정책을 호출하지 않고 전역·종목별 가상 BUY 잠금을 계산한다."""
    config_fingerprint = config.fingerprint

    normalized_market = (
        market.strip().upper() if isinstance(market, str) else ""
    )
    normalized_symbol = (
        symbol.strip().upper() if isinstance(symbol, str) else ""
    )
    normalized_evaluated_at = _utc_or_none(evaluated_at)
    scope_valid = (
        not isinstance(owner_user_id, bool)
        and isinstance(owner_user_id, int)
        and owner_user_id > 0
        and isinstance(account_key, str)
        and bool(account_key.strip())
        and normalized_market in {"KRX", "US"}
        and bool(normalized_symbol)
        and normalized_evaluated_at is not None
    )
    if not scope_valid:
        return _non_valid_result(
            owner_user_id=owner_user_id,
            account_key=account_key,
            market=normalized_market,
            symbol=normalized_symbol,
            evaluated_at=evaluated_at,
            config=config,
            status=ShadowLossStatus.FAIL_CLOSED,
            reason=ShadowLossReason.INVALID_SCOPE,
        )
    assert normalized_evaluated_at is not None

    prepared, failure = _prepare_facts(
        facts,
        owner_user_id=owner_user_id,
        account_key=account_key,
        market=normalized_market,
        evaluated_at=normalized_evaluated_at,
        lookback=config.lookback,
    )
    if failure is not None:
        return _non_valid_result(
            owner_user_id=owner_user_id,
            account_key=account_key,
            market=normalized_market,
            symbol=normalized_symbol,
            evaluated_at=normalized_evaluated_at,
            config=config,
            status=ShadowLossStatus.FAIL_CLOSED,
            reason=failure,
        )
    if not prepared:
        return _non_valid_result(
            owner_user_id=owner_user_id,
            account_key=account_key,
            market=normalized_market,
            symbol=normalized_symbol,
            evaluated_at=normalized_evaluated_at,
            config=config,
            status=ShadowLossStatus.INSUFFICIENT,
            reason=ShadowLossReason.INSUFFICIENT,
        )

    global_facts, global_failure = _deduplicate_facts(prepared)
    symbol_facts, symbol_failure = _deduplicate_facts(
        tuple(
            fact
            for fact in prepared
            if fact.symbol == normalized_symbol
        )
    )
    deduplication_failure = global_failure or symbol_failure
    if deduplication_failure is not None:
        return _non_valid_result(
            owner_user_id=owner_user_id,
            account_key=account_key,
            market=normalized_market,
            symbol=normalized_symbol,
            evaluated_at=normalized_evaluated_at,
            config=config,
            status=ShadowLossStatus.FAIL_CLOSED,
            reason=deduplication_failure,
        )
    global_lock = _observe_scope(
        global_facts,
        scope=ShadowLockScope.GLOBAL,
        symbol=None,
        owner_user_id=owner_user_id,
        account_key=account_key,
        market=normalized_market,
        evaluated_at=normalized_evaluated_at,
        config=config,
        config_fingerprint=config_fingerprint,
    )
    symbol_lock = _observe_scope(
        symbol_facts,
        scope=ShadowLockScope.SYMBOL,
        symbol=normalized_symbol,
        owner_user_id=owner_user_id,
        account_key=account_key,
        market=normalized_market,
        evaluated_at=normalized_evaluated_at,
        config=config,
        config_fingerprint=config_fingerprint,
    )
    return ShadowLossStreakResult(
        schema_version=SHADOW_LOSS_STREAK_SCHEMA_VERSION,
        evidence_version=SHADOW_LOSS_STREAK_EVIDENCE_VERSION,
        mode=SHADOW_MODE,
        status=ShadowLossStatus.VALID,
        owner_user_id=owner_user_id,
        account_key=account_key,
        market=normalized_market,
        symbol=normalized_symbol,
        evaluated_at=normalized_evaluated_at,
        source_timestamps=tuple(
            fact.executed_at for fact in global_facts
        ),
        config=config,
        config_fingerprint=config_fingerprint,
        global_lock=global_lock,
        symbol_lock=symbol_lock,
    )


async def load_shadow_loss_lock_state(
    db: AsyncSession,
    *,
    owner_user_id: int,
    account_key: str,
    market: str,
    scope: ShadowLockScope,
    symbol: str | None,
    active_at: datetime | None = None,
) -> ShadowLossLockState | None:
    """복합 고유 범위의 마지막 SHADOW 상태를 재시작 후 복구한다."""

    symbol_key = _scope_symbol_key(scope, symbol)
    statement = select(KAssetShadowLossLock).where(
        KAssetShadowLossLock.owner_user_id == owner_user_id,
        KAssetShadowLossLock.account_key == account_key,
        KAssetShadowLossLock.market == market.strip().upper(),
        KAssetShadowLossLock.lock_scope == scope.value,
        KAssetShadowLossLock.symbol == symbol_key,
    )
    if active_at is not None:
        normalized_active_at = _require_utc(active_at, "active_at")
        statement = statement.where(
            KAssetShadowLossLock.buy_locked.is_(True),
            KAssetShadowLossLock.expires_at > normalized_active_at,
        )
    row = await db.scalar(statement)
    return _row_to_state(row) if row is not None else None


async def persist_shadow_loss_locks(
    db: AsyncSession,
    result: ShadowLossStreakResult,
) -> tuple[ShadowLossLockState, ...]:
    """호출자 트랜잭션 안에서 두 SHADOW 범위 상태를 멱등 upsert한다."""

    if result.status is not ShadowLossStatus.VALID:
        raise ValueError("only valid SHADOW loss-streak results may be persisted")
    persisted: list[ShadowLossLockState] = []
    for observation in (result.global_lock, result.symbol_lock):
        expected = ShadowLossLockState.from_observation(observation)
        values = _persistence_values(observation)
        base = insert(KAssetShadowLossLock).values(**values)
        mutable_values = {
            key: getattr(base.excluded, key)
            for key in values
            if key
            not in {
                "owner_user_id",
                "account_key",
                "market",
                "lock_scope",
                "symbol",
                "created_at",
            }
        }
        mutable_values["updated_at"] = func.now()
        statement = (
            base.on_conflict_do_update(
                index_elements=[
                    "owner_user_id",
                    "account_key",
                    "market",
                    "lock_scope",
                    "symbol",
                ],
                set_=mutable_values,
                where=(
                    KAssetShadowLossLock.evaluated_at
                    < base.excluded.evaluated_at
                ),
            )
            .returning(KAssetShadowLossLock)
        )
        row = await db.scalar(statement)
        if row is not None:
            persisted.append(_row_to_state(row))
            continue
        existing = await load_shadow_loss_lock_state(
            db,
            owner_user_id=observation.owner_user_id,
            account_key=observation.account_key,
            market=observation.market,
            scope=observation.scope,
            symbol=observation.symbol,
        )
        if existing == expected:
            persisted.append(existing)
            continue
        raise ConcurrentShadowLossLockUpdate(
            "persisted SHADOW loss-lock state is newer or differs at the same timestamp"
        )
    return tuple(persisted)


def _prepare_facts(
    facts: Sequence[ShadowPaperTradeFact],
    *,
    owner_user_id: int,
    account_key: str,
    market: str,
    evaluated_at: datetime,
    lookback: timedelta,
) -> tuple[tuple[ShadowPaperTradeFact, ...], ShadowLossReason | None]:
    cutoff = evaluated_at - lookback
    retained: list[ShadowPaperTradeFact] = []
    for fact in facts:
        if (
            fact.owner_user_id != owner_user_id
            or fact.account_key != account_key
            or str(fact.market).strip().upper() != market
            or str(fact.account_mode).strip().upper() != "PAPER"
            or str(fact.lifecycle_status).strip().upper() != "CLOSED"
            or str(fact.side).strip().upper() != "SELL"
            or not str(fact.symbol).strip()
        ):
            continue
        if (
            not isinstance(fact.symbol, str)
            or not isinstance(fact.transaction_id, str)
            or not isinstance(fact.trade_id, str)
            or (
                fact.exit_reason is not None
                and not isinstance(fact.exit_reason, str)
            )
            or (
                fact.realized_pnl is not None
                and not isinstance(fact.realized_pnl, Decimal)
            )
        ):
            return (), ShadowLossReason.INVALID_FACT_SCHEMA
        executed_at = _utc_or_none(fact.executed_at)
        if executed_at is None:
            return (), ShadowLossReason.INVALID_TIMESTAMP
        if executed_at < cutoff or executed_at > evaluated_at:
            continue
        if fact.realized_pnl is None or not _finite_decimal(fact.realized_pnl):
            continue
        if (
            isinstance(fact.id, bool)
            or not isinstance(fact.id, int)
            or fact.id <= 0
            or not fact.transaction_id.strip()
            or not fact.trade_id.strip()
        ):
            return (), ShadowLossReason.ORDERING_COLLISION
        retained.append(
            ShadowPaperTradeFact(
                id=fact.id,
                transaction_id=fact.transaction_id.strip(),
                trade_id=fact.trade_id.strip(),
                owner_user_id=fact.owner_user_id,
                account_key=fact.account_key,
                market=market,
                symbol=fact.symbol.strip().upper(),
                side="SELL",
                lifecycle_status="CLOSED",
                realized_pnl=fact.realized_pnl,
                exit_reason=(
                    fact.exit_reason.strip() if fact.exit_reason is not None else None
                ),
                executed_at=executed_at,
                account_mode="PAPER",
            )
        )
    retained.sort(key=lambda item: (item.executed_at, item.id))

    return tuple(retained), None


def _deduplicate_facts(
    facts: tuple[ShadowPaperTradeFact, ...],
) -> tuple[tuple[ShadowPaperTradeFact, ...], ShadowLossReason | None]:
    deduplicated: list[ShadowPaperTradeFact] = []
    seen_transaction_ids: set[str] = set()
    seen_trade_ids: set[str] = set()
    previous_key: tuple[datetime, int] | None = None
    for fact in facts:
        if (
            fact.transaction_id in seen_transaction_ids
            or fact.trade_id in seen_trade_ids
        ):
            continue
        key = (fact.executed_at, fact.id)
        if key == previous_key:
            return (), ShadowLossReason.ORDERING_COLLISION
        previous_key = key
        seen_transaction_ids.add(fact.transaction_id)
        seen_trade_ids.add(fact.trade_id)
        deduplicated.append(fact)
    return tuple(deduplicated), None


def _observe_scope(
    facts: tuple[ShadowPaperTradeFact, ...],
    *,
    scope: ShadowLockScope,
    symbol: str | None,
    owner_user_id: int,
    account_key: str,
    market: str,
    evaluated_at: datetime,
    config: ShadowLossStreakConfig,
    config_fingerprint: str,
) -> ShadowLossLockObservation:
    streak_count = 0
    newest: ShadowPaperTradeFact | None = None
    allowed_reasons = frozenset(config.stop_loss_reasons)
    for fact in facts:
        reason = _normalize_reason(fact.exit_reason or "")
        if (
            fact.realized_pnl is not None
            and fact.realized_pnl < _ZERO
            and reason in allowed_reasons
        ):
            streak_count += 1
            newest = fact
        else:
            streak_count = 0
            newest = None

    expires_at = newest.executed_at + config.lock_duration if newest else None
    if streak_count >= config.loss_limit and expires_at is not None:
        if expires_at > evaluated_at:
            buy_locked = True
            reason = ShadowLossReason.LIMIT_REACHED
        else:
            buy_locked = False
            reason = ShadowLossReason.EXPIRED
    else:
        buy_locked = False
        reason = ShadowLossReason.BELOW_LIMIT
    return ShadowLossLockObservation(
        schema_version=SHADOW_LOSS_STREAK_SCHEMA_VERSION,
        evidence_version=SHADOW_LOSS_STREAK_EVIDENCE_VERSION,
        mode=SHADOW_MODE,
        status=ShadowLossStatus.VALID,
        scope=scope,
        symbol=symbol,
        owner_user_id=owner_user_id,
        account_key=account_key,
        market=market,
        evaluated_at=evaluated_at,
        source_timestamps=tuple(fact.executed_at for fact in facts),
        config_fingerprint=config_fingerprint,
        streak_count=streak_count,
        loss_limit=config.loss_limit,
        newest_loss_id=newest.id if newest else None,
        newest_loss_transaction_id=newest.transaction_id if newest else None,
        newest_loss_trade_id=newest.trade_id if newest else None,
        newest_loss_at=newest.executed_at if newest else None,
        expires_at=expires_at,
        buy_locked=buy_locked,
        reason=reason,
        sell_allowed=True,
        position_manager_risk_reduction_allowed=True,
    )


def _non_valid_result(
    *,
    owner_user_id: int,
    account_key: str,
    market: str,
    symbol: str,
    evaluated_at: datetime,
    config: ShadowLossStreakConfig,
    status: ShadowLossStatus,
    reason: ShadowLossReason,
) -> ShadowLossStreakResult:
    config_fingerprint = config.fingerprint

    def observation(scope: ShadowLockScope) -> ShadowLossLockObservation:
        return ShadowLossLockObservation(
            schema_version=SHADOW_LOSS_STREAK_SCHEMA_VERSION,
            evidence_version=SHADOW_LOSS_STREAK_EVIDENCE_VERSION,
            mode=SHADOW_MODE,
            status=status,
            scope=scope,
            symbol=symbol if scope is ShadowLockScope.SYMBOL else None,
            owner_user_id=owner_user_id,
            account_key=account_key,
            market=market,
            evaluated_at=evaluated_at,
            source_timestamps=(),
            config_fingerprint=config_fingerprint,
            streak_count=0,
            loss_limit=config.loss_limit,
            newest_loss_id=None,
            newest_loss_transaction_id=None,
            newest_loss_trade_id=None,
            newest_loss_at=None,
            expires_at=None,
            buy_locked=True,
            reason=reason,
            sell_allowed=True,
            position_manager_risk_reduction_allowed=True,
        )

    return ShadowLossStreakResult(
        schema_version=SHADOW_LOSS_STREAK_SCHEMA_VERSION,
        evidence_version=SHADOW_LOSS_STREAK_EVIDENCE_VERSION,
        mode=SHADOW_MODE,
        status=status,
        owner_user_id=owner_user_id,
        account_key=account_key,
        market=market,
        symbol=symbol,
        evaluated_at=evaluated_at,
        source_timestamps=(),
        config=config,
        config_fingerprint=config_fingerprint,
        global_lock=observation(ShadowLockScope.GLOBAL),
        symbol_lock=observation(ShadowLockScope.SYMBOL),
    )


def _persistence_values(
    observation: ShadowLossLockObservation,
) -> dict[str, object]:
    evaluated_at = _require_utc(observation.evaluated_at, "evaluated_at")
    return {
        "owner_user_id": observation.owner_user_id,
        "account_key": observation.account_key,
        "market": observation.market,
        "lock_scope": observation.scope.value,
        "symbol": _scope_symbol_key(observation.scope, observation.symbol),
        "evaluated_at": evaluated_at,
        "streak_count": observation.streak_count,
        "loss_limit": observation.loss_limit,
        "newest_loss_id": observation.newest_loss_id,
        "newest_loss_transaction_id": observation.newest_loss_transaction_id,
        "newest_loss_trade_id": observation.newest_loss_trade_id,
        "newest_loss_at": observation.newest_loss_at,
        "expires_at": observation.expires_at,
        "buy_locked": observation.buy_locked,
        "lock_reason": observation.reason.value,
        "config_fingerprint": observation.config_fingerprint,
        "schema_version": observation.schema_version,
        "evidence_version": observation.evidence_version,
        "status": observation.status.value,
        "mode": observation.mode,
        "evidence": observation.as_evidence(),
    }


def _row_to_state(row: KAssetShadowLossLock) -> ShadowLossLockState:
    scope = ShadowLockScope(str(row.lock_scope))
    symbol = None if scope is ShadowLockScope.GLOBAL else str(row.symbol)
    return ShadowLossLockState(
        owner_user_id=int(row.owner_user_id),
        account_key=str(row.account_key),
        market=str(row.market),
        scope=scope,
        symbol=symbol,
        evaluated_at=_require_utc(row.evaluated_at, "evaluated_at"),
        streak_count=int(row.streak_count),
        loss_limit=int(row.loss_limit),
        newest_loss_id=(
            int(row.newest_loss_id) if row.newest_loss_id is not None else None
        ),
        newest_loss_transaction_id=(
            str(row.newest_loss_transaction_id)
            if row.newest_loss_transaction_id is not None
            else None
        ),
        newest_loss_trade_id=(
            str(row.newest_loss_trade_id)
            if row.newest_loss_trade_id is not None
            else None
        ),
        newest_loss_at=(
            _require_utc(row.newest_loss_at, "newest_loss_at")
            if row.newest_loss_at is not None
            else None
        ),
        expires_at=(
            _require_utc(row.expires_at, "expires_at")
            if row.expires_at is not None
            else None
        ),
        buy_locked=bool(row.buy_locked),
        reason=ShadowLossReason(str(row.lock_reason)),
        config_fingerprint=str(row.config_fingerprint),
        schema_version=str(row.schema_version),
        evidence_version=str(row.evidence_version),
        mode=str(row.mode),  # type: ignore[arg-type]
        status=ShadowLossStatus(str(row.status)),
    )


def _scope_symbol_key(scope: ShadowLockScope, symbol: str | None) -> str:
    if scope is ShadowLockScope.GLOBAL:
        if symbol is not None:
            raise ValueError("GLOBAL lock scope cannot have a symbol")
        return _GLOBAL_SYMBOL_KEY
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("SYMBOL lock scope requires a symbol")
    return normalized


def _normalize_reason(value: str) -> str:
    return str(value).strip().upper()


def _finite_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _utc_or_none(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _require_utc(value: object, field_name: str) -> datetime:
    normalized = _utc_or_none(value)
    if normalized is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return normalized


def _timestamp_text(value: object) -> str | None:
    normalized = _utc_or_none(value)
    return normalized.isoformat() if normalized is not None else None


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        (value.days * 86_400 + value.seconds) * 1_000_000
        + value.microseconds
    )


__all__ = [
    "ConcurrentShadowLossLockUpdate",
    "SHADOW_LOSS_STREAK_CONFIG_SCHEMA_VERSION",
    "SHADOW_LOSS_STREAK_EVIDENCE_VERSION",
    "SHADOW_LOSS_STREAK_SCHEMA_VERSION",
    "SHADOW_MODE",
    "ShadowLockScope",
    "ShadowLossLockObservation",
    "ShadowLossLockState",
    "ShadowLossReason",
    "ShadowLossStatus",
    "ShadowLossStreakConfig",
    "ShadowLossStreakResult",
    "ShadowPaperTradeFact",
    "evaluate_shadow_loss_streak",
    "load_shadow_loss_lock_state",
    "persist_shadow_loss_locks",
]
