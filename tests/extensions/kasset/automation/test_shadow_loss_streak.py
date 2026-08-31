from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions.kasset.automation.shadow_loss_streak import (
    SHADOW_LOSS_STREAK_EVIDENCE_VERSION,
    SHADOW_LOSS_STREAK_SCHEMA_VERSION,
    ShadowLockScope,
    ShadowLossReason,
    ShadowLossStatus,
    ShadowLossStreakConfig,
    ShadowPaperTradeFact,
    evaluate_shadow_loss_streak,
    load_shadow_loss_lock_state,
    persist_shadow_loss_locks,
)

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
def config() -> ShadowLossStreakConfig:
    return ShadowLossStreakConfig(
        stop_loss_reasons=("VOLATILITY_STOP", "RISK_STOP"),
        loss_limit=3,
        lookback=timedelta(days=7),
        lock_duration=timedelta(hours=1),
    )


def _fact(
    id_: int,
    *,
    minutes_ago: int = 0,
    at: datetime | None = None,
    symbol: str = "005930",
    pnl: str | None = "-100",
    reason: str | None = "RISK_STOP",
    owner_user_id: int = 17,
    account_key: str = "paper:alpha",
    market: str = "KRX",
    side: str = "SELL",
    lifecycle_status: str = "CLOSED",
    account_mode: str = "PAPER",
    transaction_id: str | None = None,
    trade_id: str | None = None,
) -> ShadowPaperTradeFact:
    return ShadowPaperTradeFact(
        id=id_,
        transaction_id=transaction_id or f"txn-{id_}",
        trade_id=trade_id or f"trade-{id_}",
        owner_user_id=owner_user_id,
        account_key=account_key,
        market=market,
        symbol=symbol,
        side=side,
        lifecycle_status=lifecycle_status,
        realized_pnl=Decimal(pnl) if pnl is not None else None,
        exit_reason=reason,
        executed_at=at or (_NOW - timedelta(minutes=minutes_ago)),
        account_mode=account_mode,
    )


def _evaluate(
    facts: list[ShadowPaperTradeFact],
    *,
    config: ShadowLossStreakConfig,
    symbol: str = "005930",
    evaluated_at: datetime = _NOW,
):
    return evaluate_shadow_loss_streak(
        facts,
        owner_user_id=17,
        account_key="paper:alpha",
        market="KRX",
        symbol=symbol,
        evaluated_at=evaluated_at,
        config=config,
    )


@pytest.mark.parametrize(
    ("count", "locked"),
    [(2, False), (3, True), (4, True)],
)
def test_limit_minus_one_limit_and_limit_plus_one(
    config: ShadowLossStreakConfig,
    count: int,
    locked: bool,
) -> None:
    facts = [_fact(index, minutes_ago=count - index) for index in range(1, count + 1)]

    result = _evaluate(list(reversed(facts)), config=config)

    assert result.status is ShadowLossStatus.VALID
    assert result.global_lock.streak_count == count
    assert result.symbol_lock.streak_count == count
    assert result.effective_buy_locked is locked
    assert result.global_lock.reason is (
        ShadowLossReason.LIMIT_REACHED if locked else ShadowLossReason.BELOW_LIMIT
    )
    newest = facts[-1]
    assert result.global_lock.newest_loss_id == newest.id
    assert result.global_lock.expires_at == newest.executed_at + config.lock_duration


def test_only_configured_negative_finite_closed_sell_reasons_count(
    config: ShadowLossStreakConfig,
) -> None:
    facts = [
        _fact(1, minutes_ago=9),
        _fact(2, minutes_ago=8, lifecycle_status="OPEN"),
        _fact(3, minutes_ago=7, pnl=None),
        _fact(4, minutes_ago=6, reason="MANUAL_EXIT"),
        _fact(5, minutes_ago=5, reason="volatility_stop"),
        _fact(6, minutes_ago=4, pnl="0"),
        _fact(7, minutes_ago=3),
        _fact(8, minutes_ago=2, pnl="12"),
        _fact(9, minutes_ago=1),
    ]

    result = _evaluate(facts, config=config)

    # OPEN/NULL은 완결 사실이 아니어서 제외하고, 완결된 비대상/비손실은 연속을 끊는다.
    assert result.global_lock.streak_count == 1
    assert result.global_lock.newest_loss_id == 9
    assert result.global_lock.buy_locked is False
    assert len(result.source_timestamps) == 7


def test_nonfinite_realized_pnl_is_excluded_from_the_closed_stream(
    config: ShadowLossStreakConfig,
) -> None:
    facts = [
        _fact(1, minutes_ago=4),
        _fact(2, minutes_ago=3, pnl="NaN"),
        _fact(3, minutes_ago=2, pnl="-Infinity"),
        _fact(4, minutes_ago=1),
    ]

    result = _evaluate(facts, config=config)

    assert result.global_lock.streak_count == 2
    assert result.global_lock.buy_locked is False
    assert len(result.source_timestamps) == 2


def test_old_boundary_is_inclusive_and_future_or_older_facts_are_excluded(
    config: ShadowLossStreakConfig,
) -> None:
    boundary = _NOW - config.lookback
    facts = [
        _fact(1, at=boundary - timedelta(microseconds=1)),
        _fact(2, at=boundary),
        _fact(3, minutes_ago=1),
        _fact(4, at=_NOW + timedelta(microseconds=1)),
    ]

    result = _evaluate(facts, config=config)

    assert result.global_lock.streak_count == 2
    assert result.source_timestamps == (boundary, _NOW - timedelta(minutes=1))


def test_transaction_and_trade_ids_are_deduplicated_after_deterministic_sort(
    config: ShadowLossStreakConfig,
) -> None:
    facts = [
        _fact(1, minutes_ago=5),
        _fact(2, minutes_ago=4, transaction_id="txn-1"),
        _fact(3, minutes_ago=3),
        _fact(4, minutes_ago=2, trade_id="trade-3"),
        _fact(5, minutes_ago=1),
    ]

    result = _evaluate(list(reversed(facts)), config=config)

    assert result.global_lock.streak_count == 3
    assert result.global_lock.buy_locked is True
    assert result.global_lock.newest_loss_id == 5
    assert result.source_timestamps == (
        facts[0].executed_at,
        facts[2].executed_at,
        facts[4].executed_at,
    )


def test_symbol_scope_filters_before_its_own_identifier_deduplication(
    config: ShadowLossStreakConfig,
) -> None:
    facts = [
        _fact(
            1,
            minutes_ago=3,
            symbol="000660",
            transaction_id="shared-transaction",
        ),
        _fact(
            2,
            minutes_ago=2,
            symbol="005930",
            transaction_id="shared-transaction",
        ),
        _fact(3, minutes_ago=1, symbol="005930"),
    ]

    result = _evaluate(facts, config=config, symbol="005930")

    assert result.global_lock.streak_count == 2
    assert result.symbol_lock.streak_count == 2
    assert result.symbol_lock.source_timestamps == (
        facts[1].executed_at,
        facts[2].executed_at,
    )


def test_global_and_requested_symbol_scopes_are_independent(
    config: ShadowLossStreakConfig,
) -> None:
    facts = [
        _fact(1, minutes_ago=3, symbol="005930"),
        _fact(2, minutes_ago=2, symbol="000660"),
        _fact(3, minutes_ago=1, symbol="005930"),
    ]

    samsung = _evaluate(facts, config=config, symbol="005930")
    hynix = _evaluate(facts, config=config, symbol="000660")

    assert samsung.global_lock.streak_count == 3
    assert samsung.global_lock.buy_locked is True
    assert samsung.symbol_lock.streak_count == 2
    assert samsung.symbol_lock.buy_locked is False
    assert hynix.global_lock.streak_count == 3
    assert hynix.symbol_lock.streak_count == 1


def test_owner_account_market_symbol_and_side_isolation(
    config: ShadowLossStreakConfig,
) -> None:
    facts = [
        _fact(1, minutes_ago=8),
        _fact(2, minutes_ago=7, owner_user_id=99),
        _fact(3, minutes_ago=6, account_key="paper:other"),
        _fact(4, minutes_ago=5, market="US"),
        _fact(5, minutes_ago=4, symbol="000660"),
        _fact(6, minutes_ago=3, side="BUY"),
        _fact(7, minutes_ago=2, account_mode="LIVE"),
        _fact(8, minutes_ago=1),
    ]

    result = _evaluate(facts, config=config, symbol="005930")

    assert result.global_lock.streak_count == 3
    assert result.global_lock.newest_loss_id == 8
    assert result.symbol_lock.streak_count == 2
    assert result.symbol_lock.newest_loss_id == 8


def test_expiry_and_newest_loss_rollover(
    config: ShadowLossStreakConfig,
) -> None:
    expired_facts = [
        _fact(1, minutes_ago=130),
        _fact(2, minutes_ago=125),
        _fact(3, minutes_ago=120),
    ]

    expired = _evaluate(expired_facts, config=config)
    rolled = _evaluate(
        [*expired_facts, _fact(4, minutes_ago=10)],
        config=config,
    )

    assert expired.global_lock.streak_count == 3
    assert expired.global_lock.buy_locked is False
    assert expired.global_lock.reason is ShadowLossReason.EXPIRED
    assert expired.global_lock.expires_at == _NOW - timedelta(minutes=60)
    assert rolled.global_lock.streak_count == 4
    assert rolled.global_lock.buy_locked is True
    assert rolled.global_lock.reason is ShadowLossReason.LIMIT_REACHED
    assert rolled.global_lock.expires_at == _NOW + timedelta(minutes=50)


def test_profit_after_a_lock_clears_the_current_observational_state(
    config: ShadowLossStreakConfig,
) -> None:
    result = _evaluate(
        [
            _fact(1, minutes_ago=4),
            _fact(2, minutes_ago=3),
            _fact(3, minutes_ago=2),
            _fact(4, minutes_ago=1, pnl="1"),
        ],
        config=config,
    )

    assert result.global_lock.streak_count == 0
    assert result.global_lock.buy_locked is False
    assert result.global_lock.newest_loss_at is None
    assert result.global_lock.expires_at is None


def test_sell_and_position_manager_risk_reduction_are_always_allowed(
    config: ShadowLossStreakConfig,
) -> None:
    valid = _evaluate([_fact(1), _fact(2), _fact(3)], config=config)
    insufficient = _evaluate([], config=config)

    for result in (valid, insufficient):
        assert result.global_lock.sell_allowed is True
        assert result.symbol_lock.sell_allowed is True
        assert result.global_lock.position_manager_risk_reduction_allowed is True
        assert result.symbol_lock.position_manager_risk_reduction_allowed is True
        hypothetical = result.as_evidence()["hypothetical"]
        assert hypothetical["sellAllowed"] is True  # type: ignore[index]
        assert hypothetical["positionManagerRiskReductionAllowed"] is True  # type: ignore[index]


def test_invalid_timestamp_fails_closed_without_persistence(
    config: ShadowLossStreakConfig,
) -> None:
    result = _evaluate(
        [_fact(1, at=datetime(2026, 8, 31, 11, 0))],
        config=config,
    )

    assert result.status is ShadowLossStatus.FAIL_CLOSED
    assert result.global_lock.buy_locked is True
    assert result.symbol_lock.buy_locked is True
    assert result.global_lock.reason is ShadowLossReason.INVALID_TIMESTAMP
    assert result.persistent_states == ()


def test_empty_or_entirely_out_of_scope_input_is_explicitly_insufficient(
    config: ShadowLossStreakConfig,
) -> None:
    result = _evaluate([_fact(1, owner_user_id=99)], config=config)

    assert result.status is ShadowLossStatus.INSUFFICIENT
    assert result.global_lock.reason is ShadowLossReason.INSUFFICIENT
    assert result.global_lock.buy_locked is True
    assert result.as_evidence()["sourceTimestamps"] == []


def test_closed_evidence_schema_and_separate_config_fingerprint(
    config: ShadowLossStreakConfig,
) -> None:
    result = _evaluate([_fact(1), _fact(2), _fact(3)], config=config)
    evidence = result.as_evidence()

    assert set(evidence) == {
        "schemaVersion",
        "evidenceVersion",
        "mode",
        "status",
        "scope",
        "sourceTimestamps",
        "evaluatedAt",
        "ordering",
        "shadowConfig",
        "hypothetical",
        "lockEvidence",
    }
    assert evidence["schemaVersion"] == SHADOW_LOSS_STREAK_SCHEMA_VERSION
    assert evidence["evidenceVersion"] == SHADOW_LOSS_STREAK_EVIDENCE_VERSION
    assert evidence["mode"] == "SHADOW"
    assert evidence["status"] == "valid"
    assert evidence["ordering"] == {
        "fields": ["executedAt", "id"],
        "direction": "ascending",
        "deduplicateBy": ["transactionId", "tradeId"],
    }
    shadow_config = evidence["shadowConfig"]
    assert shadow_config["fingerprint"] == config.fingerprint  # type: ignore[index]
    assert len(config.fingerprint) == 64
    assert json.loads(json.dumps(config.as_serializable())) == config.as_serializable()
    with pytest.raises(FrozenInstanceError):
        config.loss_limit = 4  # type: ignore[misc]


def test_evidence_feature_flags_do_not_change_calculation() -> None:
    enabled = ShadowLossStreakConfig(
        stop_loss_reasons=("RISK_STOP",),
        loss_limit=3,
        lookback=timedelta(days=7),
        lock_duration=timedelta(hours=1),
    )
    hidden = ShadowLossStreakConfig(
        stop_loss_reasons=("RISK_STOP",),
        loss_limit=3,
        lookback=timedelta(days=7),
        lock_duration=timedelta(hours=1),
        emit_global_evidence=False,
        emit_symbol_evidence=False,
    )
    facts = [_fact(1), _fact(2), _fact(3)]

    visible_result = _evaluate(facts, config=enabled)
    hidden_result = _evaluate(facts, config=hidden)

    assert (
        hidden_result.global_lock.streak_count
        == visible_result.global_lock.streak_count
    )
    assert hidden_result.symbol_lock.buy_locked == visible_result.symbol_lock.buy_locked
    assert hidden_result.as_evidence()["lockEvidence"] == []
    assert hidden.fingerprint != enabled.fingerprint


class _ScalarSequenceDb:
    def __init__(self, *results: object | None) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.results.pop(0)


def _row(state) -> SimpleNamespace:
    return SimpleNamespace(
        owner_user_id=state.owner_user_id,
        account_key=state.account_key,
        market=state.market,
        lock_scope=state.scope.value,
        symbol=state.symbol or "",
        evaluated_at=state.evaluated_at,
        streak_count=state.streak_count,
        loss_limit=state.loss_limit,
        newest_loss_id=state.newest_loss_id,
        newest_loss_transaction_id=state.newest_loss_transaction_id,
        newest_loss_trade_id=state.newest_loss_trade_id,
        newest_loss_at=state.newest_loss_at,
        expires_at=state.expires_at,
        buy_locked=state.buy_locked,
        lock_reason=state.reason.value,
        config_fingerprint=state.config_fingerprint,
        schema_version=state.schema_version,
        evidence_version=state.evidence_version,
        mode=state.mode,
        status=state.status.value,
    )


@pytest.mark.asyncio
async def test_persistence_uses_unique_global_and_symbol_identities(
    config: ShadowLossStreakConfig,
) -> None:
    result = _evaluate([_fact(1), _fact(2), _fact(3)], config=config)
    expected = result.persistent_states
    db = _ScalarSequenceDb(_row(expected[0]), _row(expected[1]))

    persisted = await persist_shadow_loss_locks(db, result)  # type: ignore[arg-type]

    assert persisted == expected
    assert [statement.__class__.__name__ for statement in db.statements] == [
        "Insert",
        "Insert",
    ]
    assert {(item.scope, item.symbol) for item in persisted} == {
        (ShadowLockScope.GLOBAL, None),
        (ShadowLockScope.SYMBOL, "005930"),
    }


@pytest.mark.asyncio
async def test_restart_recovers_idempotent_state_and_expiration(
    config: ShadowLossStreakConfig,
) -> None:
    result = _evaluate([_fact(1), _fact(2), _fact(3)], config=config)
    global_state, symbol_state = result.persistent_states
    replay_db = _ScalarSequenceDb(
        None,
        _row(global_state),
        None,
        _row(symbol_state),
    )

    replayed = await persist_shadow_loss_locks(  # type: ignore[arg-type]
        replay_db,
        result,
    )
    restarted_db = _ScalarSequenceDb(_row(global_state))
    recovered = await load_shadow_loss_lock_state(
        restarted_db,  # type: ignore[arg-type]
        owner_user_id=17,
        account_key="paper:alpha",
        market="KRX",
        scope=ShadowLockScope.GLOBAL,
        symbol=None,
        active_at=_NOW,
    )

    assert replayed == (global_state, symbol_state)
    assert recovered == global_state
    assert recovered is not None
    assert recovered.buy_locked is True
    assert recovered.expires_at == result.global_lock.expires_at
    assert [statement.__class__.__name__ for statement in replay_db.statements] == [
        "Insert",
        "Select",
        "Insert",
        "Select",
    ]
