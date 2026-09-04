from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.security import get_password_hash
from app.extensions.kasset.api.auth import MobileAuthService
from app.extensions.kasset.api.credential_vault import CredentialVault
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper import paper_account_adapter
from app.extensions.kasset.api.paper_orders import paper_orders
from app.extensions.kasset.api.router import _require_admin, _require_trader
from app.extensions.kasset.api.runtime_state import runtime_state
from app.extensions.kasset.api.schemas import LoginRequest, RegisterRequest
from app.extensions.kasset.models import (
    AndroidPaperAccount,
    AndroidPaperOrder,
)
from app.models.ai_recommendations import AIRecommendation
from app.models.paper_trading import PaperAccount
from app.models.trading import User, UserRole
from app.services.ai_recommendations import (
    AIRecommendationService,
    RecommendationNotFoundError,
    RecommendationStateConflictError,
)

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def two_owners(db_session: AsyncSession):
    suffix = uuid4().hex
    users = [
        User(
            username=f"kasset-a-{suffix}",
            email=f"kasset-a-{suffix}@example.com",
            hashed_password=get_password_hash("Owner-A-secret-1!"),
            role=UserRole.trader,
            is_active=True,
        ),
        User(
            username=f"kasset-b-{suffix}",
            email=f"kasset-b-{suffix}@example.com",
            hashed_password=get_password_hash("Owner-B-secret-1!"),
            role=UserRole.trader,
            is_active=True,
        ),
    ]
    db_session.add_all(users)
    await db_session.commit()
    for user in users:
        await db_session.refresh(user)
    # Capture primary keys eagerly: commit/rollback inside the tests expires
    # these instances, and expired attribute access on an async session raises
    # MissingGreenlet instead of lazy-loading.
    user_ids = [user.id for user in users]
    try:
        yield users
    finally:
        await db_session.rollback()
        account_ids = list(
            (
                await db_session.scalars(
                    select(AndroidPaperAccount.paper_account_id).where(
                        AndroidPaperAccount.owner_user_id.in_(user_ids)
                    )
                )
            ).all()
        )
        await db_session.execute(delete(User).where(User.id.in_(user_ids)))
        if account_ids:
            await db_session.execute(
                delete(PaperAccount).where(PaperAccount.id.in_(account_ids))
            )
        await db_session.commit()


@pytest.mark.asyncio
async def test_public_auth_device_revoke_is_owner_and_device_scoped(
    db_session: AsyncSession,
) -> None:
    suffix = uuid4().hex
    auth = MobileAuthService()
    usernames = [f"register-a-{suffix}", f"register-b-{suffix}"]
    requests = [
        RegisterRequest(
            username=usernames[0],
            email=f"{usernames[0]}@example.com",
            password="Register-A-secret-1!",
            deviceId="shared-hardware-id",
            deviceName="A phone",
        ),
        RegisterRequest(
            username=usernames[1],
            email=f"{usernames[1]}@example.com",
            password="Register-B-secret-1!",
            deviceId="shared-hardware-id",
            deviceName="B phone",
        ),
    ]
    try:
        tokens_a = await auth.register(db_session, requests[0])
        tokens_b = await auth.register(db_session, requests[1])
        tokens_a_other_device = await auth.login(
            db_session,
            LoginRequest(
                username=usernames[0],
                password="Register-A-secret-1!",
                deviceId="a-tablet-id",
                deviceName="A tablet",
            ),
        )
        session_a = await auth.authenticate(db_session, tokens_a.access_token)
        session_b = await auth.authenticate(db_session, tokens_b.access_token)

        assert session_a.user.role == UserRole.trader
        assert session_b.user.role == UserRole.trader
        assert session_a.user.id != session_b.user.id
        assert (await auth.current_user(db_session, session_a)).model_dump() == {
            "id": session_a.user.id,
            "username": usernames[0],
            "email": f"{usernames[0]}@example.com",
            "nickname": session_a.user.nickname,
            "role": UserRole.trader,
        }

        rotated_a = await auth.refresh(db_session, tokens_a.refresh_token)
        with pytest.raises(MobileApiError) as replayed_refresh:
            await auth.refresh(db_session, tokens_a.refresh_token)
        assert replayed_refresh.value.code == "UNAUTHORIZED"
        # Rotating the refresh token must not strand access tokens the device
        # already has in flight; only revoke/expiry end a session.
        assert (
            await auth.authenticate(db_session, tokens_a.access_token)
        ).user.id == session_a.user.id
        session_a = await auth.authenticate(db_session, rotated_a.access_token)

        await auth.revoke(db_session, session_a)

        with pytest.raises(MobileApiError) as revoked:
            await auth.authenticate(db_session, rotated_a.access_token)
        assert revoked.value.code == "UNAUTHORIZED"
        assert (await auth.authenticate(db_session, tokens_b.access_token)).user.id == (
            session_b.user.id
        )
        assert (
            await auth.authenticate(db_session, tokens_a_other_device.access_token)
        ).user.id == session_a.user.id
    finally:
        await db_session.rollback()
        await db_session.execute(delete(User).where(User.username.in_(usernames)))
        await db_session.commit()


@pytest.mark.asyncio
async def test_public_auth_fails_closed_for_password_duplicates_and_inactive_user(
    db_session: AsyncSession,
) -> None:
    suffix = uuid4().hex
    auth = MobileAuthService()
    username = f"register-guard-{suffix}"
    email = f"register-guard-{suffix}@example.com"
    request = RegisterRequest(
        username=username,
        email=email,
        password="Register-guard-1!",
        deviceId="guard-device",
        deviceName="Guard phone",
    )
    try:
        with pytest.raises(MobileApiError) as weak:
            await auth.register(
                db_session,
                RegisterRequest(
                    username=f"weak-{suffix}",
                    email=f"weak-{suffix}@example.com",
                    password="weak",
                    deviceId="weak-device",
                    deviceName="Weak phone",
                ),
            )
        assert weak.value.code == "WEAK_PASSWORD"

        await auth.register(db_session, request)
        with pytest.raises(MobileApiError) as duplicate_username:
            await auth.register(
                db_session,
                request.model_copy(update={"email": f"other-{suffix}@example.com"}),
            )
        assert duplicate_username.value.code == "USERNAME_TAKEN"

        with pytest.raises(MobileApiError) as duplicate_email:
            await auth.register(
                db_session,
                request.model_copy(update={"username": f"other-{suffix}"}),
            )
        assert duplicate_email.value.code == "EMAIL_TAKEN"

        user = await db_session.scalar(select(User).where(User.username == username))
        assert user is not None
        user.is_active = False
        await db_session.commit()
        with pytest.raises(MobileApiError) as inactive:
            await auth.login(
                db_session,
                LoginRequest(
                    username=email,
                    password="Register-guard-1!",
                    deviceId="guard-device",
                    deviceName="Guard phone",
                ),
            )
        assert inactive.value.code == "INVALID_CREDENTIALS"
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(User).where(
                User.username.in_([username, f"weak-{suffix}", f"other-{suffix}"])
            )
        )
        await db_session.commit()


def test_role_guards_keep_viewers_read_only_and_kill_switch_admin_only() -> None:
    viewer = SimpleNamespace(user=SimpleNamespace(role=UserRole.viewer))
    trader = SimpleNamespace(user=SimpleNamespace(role=UserRole.trader))
    admin = SimpleNamespace(user=SimpleNamespace(role=UserRole.admin))

    with pytest.raises(MobileApiError) as viewer_write:
        _require_trader(viewer)
    assert viewer_write.value.status_code == 403

    _require_trader(trader)
    with pytest.raises(MobileApiError) as trader_global:
        _require_admin(trader)
    assert trader_global.value.status_code == 403
    _require_admin(admin)


@pytest.mark.asyncio
async def test_risk_policy_isolated_per_owner(
    db_session: AsyncSession,
    two_owners: list[User],
) -> None:
    owner_a, owner_b = two_owners
    await runtime_state.update_policy(
        db_session,
        owner_a.id,
        max_order_ratio=Decimal("0.0500"),
        max_symbol_ratio=Decimal("0.1500"),
    )

    state_a = await runtime_state.get(db_session, owner_a.id)
    state_b = await runtime_state.get(db_session, owner_b.id)
    assert Decimal(state_a.max_order_ratio) == Decimal("0.0500")
    assert Decimal(state_a.max_symbol_ratio) == Decimal("0.1500")
    assert Decimal(state_b.max_order_ratio) == Decimal("0.1000")
    assert Decimal(state_b.max_symbol_ratio) == Decimal("0.2500")


@pytest.mark.asyncio
async def test_same_client_order_id_is_independent_and_foreign_order_is_hidden(
    db_session: AsyncSession,
    two_owners: list[User],
) -> None:
    owner_a, owner_b = two_owners
    owner_a_id, owner_b_id = owner_a.id, owner_b.id
    accounts = [
        PaperAccount(
            name=f"KAsset order isolation {uuid4().hex}",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        for _ in range(2)
    ]
    db_session.add_all(accounts)
    await db_session.flush()
    account_ids = [account.id for account in accounts]
    db_session.add_all(
        [
            AndroidPaperAccount(
                owner_user_id=owner.id,
                paper_account_id=account.id,
            )
            for owner, account in zip(two_owners, accounts, strict=True)
        ]
    )
    shared_client_id = f"shared-client-{uuid4().hex}"
    shared_broker_id = f"shared-broker-{uuid4().hex}"
    orders = [
        AndroidPaperOrder(
            id=str(uuid4()),
            owner_user_id=owner.id,
            client_order_id=shared_client_id,
            paper_account_id=account.id,
            broker_order_id=shared_broker_id,
            market="KRX",
            symbol="005930",
            currency="KRW",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("1"),
            limit_price=Decimal("1"),
            status="OPEN",
            filled_quantity=Decimal("0"),
        )
        for owner, account in zip(two_owners, accounts, strict=True)
    ]
    order_ids = [order.id for order in orders]
    db_session.add_all(orders)
    await db_session.commit()

    assert (
        await paper_orders.get_by_client_order_id(
            db_session, owner_a_id, shared_client_id
        )
    ).id == order_ids[0]
    assert (
        await paper_orders.get_by_client_order_id(
            db_session, owner_b_id, shared_client_id
        )
    ).id == order_ids[1]
    with pytest.raises(MobileApiError) as hidden:
        await paper_orders.get(db_session, owner_b_id, order_ids[0])
    assert hidden.value.status_code == 404

    with pytest.raises(MobileApiError) as hidden_account:
        await paper_account_adapter.resolve_account(
            db_session,
            owner_b_id,
            f"PAPER-{account_ids[0]}",
        )
    assert hidden_account.value.status_code == 404


@pytest.mark.asyncio
async def test_credentials_are_isolated_by_owner(
    db_session: AsyncSession,
    two_owners: list[User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        CredentialVault,
        "_master_key",
        staticmethod(lambda: b"kasset-test-master-key-32-bytes!"),
    )
    vault = CredentialVault()
    owner_a, owner_b = two_owners

    credential_a = await vault.store_nh(
        db_session,
        owner_a.id,
        app_key="a-key",
        app_secret="a-secret",
        account_no="a-account",
    )
    credential_b = await vault.store_nh(
        db_session,
        owner_b.id,
        app_key="b-key",
        app_secret="b-secret",
        account_no="b-account",
    )

    assert credential_a.id != credential_b.id
    assert (await vault.reveal_nh(db_session, owner_a.id)).app_key == "a-key"
    assert (await vault.reveal_nh(db_session, owner_b.id)).app_key == "b-key"

    await vault.delete_nh(db_session, owner_b.id)
    assert await vault.record(db_session, owner_b.id, "NH") is None
    assert await vault.record(db_session, owner_a.id, "NH") is not None


@pytest.mark.asyncio
async def test_recommendation_decision_is_owner_scoped_and_has_no_order_side_effect(
    db_session: AsyncSession,
    two_owners: list[User],
) -> None:
    owner_a, owner_b = two_owners
    recommendation_a = _recommendation(owner_a.id, f"rec-a-{uuid4().hex}")
    recommendation_b = _recommendation(owner_b.id, f"rec-b-{uuid4().hex}")
    db_session.add_all([recommendation_a, recommendation_b])
    await db_session.commit()
    service = AIRecommendationService(db_session, clock=lambda: _NOW)
    before = await db_session.scalar(
        select(func.count())
        .select_from(AndroidPaperOrder)
        .where(AndroidPaperOrder.owner_user_id.in_([owner_a.id, owner_b.id]))
    )

    decided = await service.decide(
        owner_a.id,
        recommendation_id=recommendation_a.id,
        decision="APPROVED",
    )

    assert decided.decision == "APPROVED"
    with pytest.raises(RecommendationNotFoundError):
        await service.decide(
            owner_b.id,
            recommendation_id=recommendation_a.id,
            decision="APPROVED",
        )
    after = await db_session.scalar(
        select(func.count())
        .select_from(AndroidPaperOrder)
        .where(AndroidPaperOrder.owner_user_id.in_([owner_a.id, owner_b.id]))
    )
    assert after == before
    assert [
        row.id
        for row in await service.list_recommendations(
            owner_b.id, status="PENDING", limit=10
        )
    ] == [recommendation_b.id]


@pytest.mark.asyncio
async def test_paper_execution_claim_lease_fences_stale_and_foreign_workers(
    db_session: AsyncSession,
    two_owners: list[User],
) -> None:
    owner_a_id, owner_b_id = (owner.id for owner in two_owners)
    recommendation_ids = [
        f"claim-{owner_id}-{uuid4().hex}" for owner_id in (owner_a_id, owner_b_id)
    ]
    accounts = [
        PaperAccount(
            name=f"KAsset claim lease {uuid4().hex}",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        for _ in range(2)
    ]
    db_session.add_all(accounts)
    await db_session.flush()
    account_ids = [account.id for account in accounts]
    db_session.add_all(
        [
            AndroidPaperAccount(
                owner_user_id=owner_id,
                paper_account_id=account_id,
            )
            for owner_id, account_id in zip(
                (owner_a_id, owner_b_id),
                account_ids,
                strict=True,
            )
        ]
    )
    recommendations = [
        _recommendation(
            owner_id,
            recommendation_id,
            decision="APPROVED",
            decided_at=_NOW - timedelta(minutes=1),
        )
        for owner_id, recommendation_id in zip(
            (owner_a_id, owner_b_id), recommendation_ids, strict=True
        )
    ]
    recommendations[0].valid_until = _NOW + timedelta(minutes=1)
    db_session.add_all(recommendations)
    await db_session.commit()
    service = AIRecommendationService(db_session)

    first_a = await service.claim_for_paper_execution(owner_a_id, _NOW)
    assert first_a is not None
    first_a_token = str(first_a.paper_execution_token)
    assert first_a.id == recommendation_ids[0]
    assert first_a_token
    assert first_a.paper_execution_claimed_at == _NOW
    assert (
        first_a.paper_execution_lease_expires_at == _NOW + service.PAPER_EXECUTION_LEASE
    )
    assert first_a.paper_execution_attempt_count == 1
    assert (
        await service.claim_for_paper_execution(
            owner_a_id,
            _NOW + service.PAPER_EXECUTION_LEASE - timedelta(seconds=1),
        )
        is None
    )
    assert (
        await service.claim_for_paper_execution(
            owner_b_id,
            _NOW,
            recommendation_id=recommendation_ids[0],
        )
        is None
    )

    claimed_b = await service.claim_for_paper_execution(owner_b_id, _NOW)
    assert claimed_b is not None
    claimed_b_token = str(claimed_b.paper_execution_token)

    reclaimed_a = await service.claim_for_paper_execution(
        owner_a_id,
        _NOW + service.PAPER_EXECUTION_LEASE,
    )
    assert reclaimed_a is not None
    reclaimed_a_token = str(reclaimed_a.paper_execution_token)
    assert reclaimed_a.id == recommendation_ids[0]
    assert reclaimed_a_token != first_a_token
    assert reclaimed_a.paper_execution_attempt_count == 2

    orders = [
        AndroidPaperOrder(
            id=f"paper-order-{owner_id}-{uuid4().hex}",
            owner_user_id=owner_id,
            client_order_id=f"ai-rec:{recommendation_id}",
            paper_account_id=account_id,
            broker_order_id=f"paper-broker-{uuid4().hex}",
            market="KRX",
            symbol="005930",
            currency="KRW",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("1"),
            status="FILLED",
            filled_quantity=Decimal("1"),
            average_fill_price=Decimal("70000"),
        )
        for owner_id, recommendation_id, account_id in zip(
            (owner_a_id, owner_b_id),
            recommendation_ids,
            account_ids,
            strict=True,
        )
    ]
    wrong_link = AndroidPaperOrder(
        id=f"paper-order-wrong-{uuid4().hex}",
        owner_user_id=owner_a_id,
        client_order_id=f"ai-rec:not-{recommendation_ids[0]}",
        paper_account_id=account_ids[0],
        broker_order_id=f"paper-broker-{uuid4().hex}",
        market="KRX",
        symbol="005930",
        currency="KRW",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("1"),
        status="FILLED",
        filled_quantity=Decimal("1"),
        average_fill_price=Decimal("70000"),
    )
    order_ids = [order.id for order in orders]
    wrong_link_id = wrong_link.id
    db_session.add_all([*orders, wrong_link])
    await db_session.commit()

    with pytest.raises(RecommendationStateConflictError):
        await service.complete_paper_execution(
            owner_a_id,
            recommendation_ids[0],
            first_a_token,
            order_ids[0],
            _NOW + service.PAPER_EXECUTION_LEASE,
        )
    with pytest.raises(RecommendationStateConflictError):
        await service.fail_paper_execution(
            owner_a_id,
            recommendation_ids[0],
            first_a_token,
            "stale worker",
            _NOW + service.PAPER_EXECUTION_LEASE,
        )
    with pytest.raises(RecommendationStateConflictError):
        await service.complete_paper_execution(
            owner_b_id,
            recommendation_ids[0],
            reclaimed_a_token,
            order_ids[0],
            _NOW + service.PAPER_EXECUTION_LEASE,
        )
    with pytest.raises(RecommendationStateConflictError):
        await service.complete_paper_execution(
            owner_a_id,
            recommendation_ids[0],
            reclaimed_a_token,
            wrong_link_id,
            _NOW + service.PAPER_EXECUTION_LEASE,
        )

    completed = await service.complete_paper_execution(
        owner_a_id,
        recommendation_ids[0],
        reclaimed_a_token,
        order_ids[0],
        _NOW + service.PAPER_EXECUTION_LEASE,
    )
    assert completed.paper_execution_status == "SUCCEEDED"
    assert completed.paper_execution_token is None
    assert completed.paper_execution_lease_expires_at is None
    assert (
        await service.reconcile_paper_execution_completion(
            owner_a_id,
            recommendation_ids[0],
            reclaimed_a_token,
            order_ids[0],
            _NOW + service.PAPER_EXECUTION_LEASE,
        )
        is True
    )
    with pytest.raises(RecommendationStateConflictError):
        await service.complete_paper_execution(
            owner_a_id,
            recommendation_ids[0],
            reclaimed_a_token,
            order_ids[0],
            _NOW + service.PAPER_EXECUTION_LEASE,
        )

    failed = await service.fail_paper_execution(
        owner_b_id,
        recommendation_ids[1],
        claimed_b_token,
        "risk rejected",
        _NOW,
    )
    assert failed.paper_execution_status == "FAILED"
    assert failed.paper_execution_token is None
    assert failed.paper_execution_lease_expires_at is None
    assert await service.claim_for_paper_execution(owner_b_id, _NOW) is None

    exhausted_id = f"claim-exhausted-{owner_b_id}-{uuid4().hex}"
    exhausted = _recommendation(
        owner_b_id,
        exhausted_id,
        decision="APPROVED",
        decided_at=_NOW - timedelta(minutes=1),
    )
    db_session.add(exhausted)
    await db_session.commit()
    for attempt in range(1, service.PAPER_EXECUTION_MAX_ATTEMPTS + 1):
        claimed = await service.claim_for_paper_execution(
            owner_b_id,
            _NOW + service.PAPER_EXECUTION_LEASE * (attempt - 1),
            recommendation_id=exhausted_id,
        )
        assert claimed is not None
        assert claimed.paper_execution_attempt_count == attempt
    assert (
        await service.claim_for_paper_execution(
            owner_b_id,
            _NOW + service.PAPER_EXECUTION_LEASE * service.PAPER_EXECUTION_MAX_ATTEMPTS,
            recommendation_id=exhausted_id,
        )
        is None
    )
    await db_session.refresh(exhausted)
    assert exhausted.paper_execution_status == "FAILED"
    assert exhausted.paper_execution_token is None
    assert exhausted.paper_execution_lease_expires_at is None
    assert exhausted.paper_execution_error == "paper_execution_attempt_limit_exceeded"

    reconciled_id = f"claim-reconciled-{owner_b_id}-{uuid4().hex}"
    reconciled = _recommendation(
        owner_b_id,
        reconciled_id,
        decision="APPROVED",
        decided_at=_NOW - timedelta(minutes=1),
    )
    reconciled_order = AndroidPaperOrder(
        id=f"paper-order-reconciled-{uuid4().hex}",
        owner_user_id=owner_b_id,
        client_order_id=f"ai-rec:{reconciled_id}",
        paper_account_id=account_ids[1],
        broker_order_id=f"paper-broker-{uuid4().hex}",
        market="KRX",
        symbol="005930",
        currency="KRW",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("1"),
        status="FILLED",
        filled_quantity=Decimal("1"),
        average_fill_price=Decimal("70000"),
    )
    db_session.add_all([reconciled, reconciled_order])
    await db_session.commit()
    for attempt in range(1, service.PAPER_EXECUTION_MAX_ATTEMPTS + 1):
        claimed = await service.claim_for_paper_execution(
            owner_b_id,
            _NOW + service.PAPER_EXECUTION_LEASE * (attempt - 1),
            recommendation_id=reconciled_id,
        )
        assert claimed is not None
        assert claimed.paper_execution_attempt_count == attempt
    assert (
        await service.claim_for_paper_execution(
            owner_b_id,
            _NOW + service.PAPER_EXECUTION_LEASE * service.PAPER_EXECUTION_MAX_ATTEMPTS,
            recommendation_id=reconciled_id,
        )
        is None
    )
    await db_session.refresh(reconciled)
    assert reconciled.paper_execution_status == "SUCCEEDED"
    assert reconciled.paper_order_id == reconciled_order.id
    assert reconciled.paper_execution_error is None

    # 부분체결은 주문 의도가 남아 있으므로 성공으로 닫히면 안 된다. 실제 운영에서
    # 미국 소수 주문이 SUCCEEDED로 표기돼 미체결 잔량이 가려졌다.
    partial_id = f"claim-partial-{owner_b_id}-{uuid4().hex}"
    partial = _recommendation(
        owner_b_id,
        partial_id,
        decision="APPROVED",
        decided_at=_NOW - timedelta(minutes=1),
    )
    partial_order = AndroidPaperOrder(
        id=f"paper-order-partial-{uuid4().hex}",
        owner_user_id=owner_b_id,
        client_order_id=f"ai-rec:{partial_id}",
        paper_account_id=account_ids[1],
        broker_order_id=f"paper-broker-{uuid4().hex}",
        market="US",
        symbol="CRWD",
        currency="USD",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("4.0065"),
        status="PARTIALLY_FILLED",
        filled_quantity=Decimal("4"),
        average_fill_price=Decimal("214.13"),
    )
    db_session.add_all([partial, partial_order])
    await db_session.commit()
    for attempt in range(1, service.PAPER_EXECUTION_MAX_ATTEMPTS + 1):
        claimed = await service.claim_for_paper_execution(
            owner_b_id,
            _NOW + service.PAPER_EXECUTION_LEASE * (attempt - 1),
            recommendation_id=partial_id,
        )
        assert claimed is not None
    assert (
        await service.claim_for_paper_execution(
            owner_b_id,
            _NOW + service.PAPER_EXECUTION_LEASE * service.PAPER_EXECUTION_MAX_ATTEMPTS,
            recommendation_id=partial_id,
        )
        is None
    )
    await db_session.refresh(partial)
    assert partial.paper_execution_status == "FAILED"
    assert partial.paper_execution_error == "paper_order_not_final:PARTIALLY_FILLED"
    # 미체결 상태에서는 coherence 제약이 주문 링크를 금지하므로 오류 문자열이 증거다.
    assert partial.paper_order_id is None


def _http_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 5000),
        }
    )


@pytest.mark.asyncio
async def test_kasset_token_is_scoped_to_mobile_and_recommendation_paths(
    db_session: AsyncSession,
) -> None:
    """A self-registered mobile trader token must not reach generic Core APIs.

    KAsset registration grants the trader role for the mobile product. The
    token stays valid on the Android compatibility surface and the
    recommendation review API, and is rejected everywhere else — including
    trader-gated generic web APIs such as loss-cut approvals.
    """
    suffix = uuid4().hex
    auth = MobileAuthService()
    username = f"scope-{suffix}"
    try:
        tokens = await auth.register(
            db_session,
            RegisterRequest(
                username=username,
                email=f"{username}@example.com",
                password="Scope-secret-1!",
                deviceId="scope-device",
                deviceName="Scope phone",
            ),
        )

        for allowed in (
            "/api/v1/ai/recommendations",
            "/api/v1/ai/recommendations/rec-1/decision",
            "/api/v1/orders",
            "/api/v1/system/status",
            "/api/v1/watchlist",
            "/api/v1/watchlist/005930",
            "/api/v1/instruments/search",
        ):
            user = await get_current_user(
                tokens.access_token, db_session, request=_http_request(allowed)
            )
            assert user.username == username

        for forbidden in (
            "/api/invest/loss-cut-approvals",
            "/trading/api/trade-journals",
            "/api/research-retrospective",
            "/api/v1/ai/briefing-adjacent",
        ):
            with pytest.raises(HTTPException) as excinfo:
                await get_current_user(
                    tokens.access_token,
                    db_session,
                    request=_http_request(forbidden),
                )
            assert excinfo.value.status_code == 401
    finally:
        await db_session.rollback()
        await db_session.execute(delete(User).where(User.username == username))
        await db_session.commit()


def _recommendation(
    owner_user_id: int,
    recommendation_id: str,
    *,
    decision: str = "PENDING",
    decided_at: datetime | None = None,
) -> AIRecommendation:
    return AIRecommendation(
        id=recommendation_id,
        owner_user_id=owner_user_id,
        action="BUY",
        decision=decision,
        market="KRX",
        symbol="005930",
        currency="KRW",
        rationale=["owner-scoped rationale"],
        risks=[],
        evidence=[],
        suggested_quantity="1",
        created_at=_NOW - timedelta(minutes=2),
        valid_until=_NOW + timedelta(hours=1),
        decided_at=decided_at,
        updated_at=decided_at or _NOW - timedelta(minutes=2),
    )
