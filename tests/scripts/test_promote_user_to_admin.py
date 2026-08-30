"""scripts/promote_user_to_admin.py — the only way to make the first admin.

Runs against the real ``users`` table because the refusal rule it enforces is a
column-level fact (``hashed_password`` and ``google_sub`` both NULL), and
because "dry-run writes nothing" is only meaningful against a real row.
"""

import contextlib
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.trading import User, UserRole
from scripts.promote_user_to_admin import (
    PromotionRefused,
    _build_parser,
    _run,
    promote_user_to_admin,
)


@pytest.fixture
def no_cache_invalidation():
    """Keep the best-effort Redis session drop away from the test Redis."""
    with patch(
        "app.auth.web_router.invalidate_user_cache", new=AsyncMock(return_value=None)
    ) as mock:
        yield mock


@dataclass(frozen=True, slots=True)
class SeededUser:
    """Plain identity: the CLI rolls the session back, expiring ORM instances."""

    id: int
    username: str


@pytest_asyncio.fixture
async def make_user(db_session):
    created: list[int] = []

    async def _make(
        *, password: bool, google: bool, role=UserRole.trader
    ) -> SeededUser:
        tag = uuid4().hex[:12]
        user = User(
            username=f"promo-{tag}",
            email=f"promo-{tag}@example.com",
            hashed_password="$2b$12$notarealhash" if password else None,
            google_sub=f"google-{tag}" if google else None,
            role=role,
            is_active=True,
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        seeded = SeededUser(id=user.id, username=user.username)
        created.append(seeded.id)
        return seeded

    yield _make

    await db_session.rollback()
    if created:
        await db_session.execute(delete(User).where(User.id.in_(created)))
        await db_session.commit()


async def _role_in_db(db_session, user_id: int) -> UserRole:
    return await db_session.scalar(select(User.role).where(User.id == user_id))


# ------------------------------------------------------------- promotion


@pytest.mark.asyncio
async def test_password_account_is_promoted(
    db_session, make_user, no_cache_invalidation
):
    user = await make_user(password=True, google=False)

    result = await promote_user_to_admin(db_session, user_id=user.id, commit=True)

    assert result.committed is True
    assert (result.previous_role, result.new_role) == ("trader", "admin")
    assert result.login_methods == ("password",)
    assert await _role_in_db(db_session, user.id) is UserRole.admin
    no_cache_invalidation.assert_awaited_once_with(user.id)


@pytest.mark.asyncio
async def test_google_only_account_is_promoted(
    db_session, make_user, no_cache_invalidation
):
    """The operator's own account has google_sub and no password."""
    user = await make_user(password=False, google=True)

    result = await promote_user_to_admin(
        db_session, username=user.username, commit=True
    )

    assert result.committed is True
    assert result.login_methods == ("google",)
    assert await _role_in_db(db_session, user.id) is UserRole.admin


@pytest.mark.asyncio
async def test_account_without_any_login_method_is_refused(db_session, make_user):
    """kasset-mobile shape: no password, no google_sub."""
    user = await make_user(password=False, google=False)

    with pytest.raises(PromotionRefused) as exc:
        await promote_user_to_admin(db_session, user_id=user.id, commit=True)

    assert "로그인 수단이 없는 계정입니다" in str(exc.value)
    assert await _role_in_db(db_session, user.id) is UserRole.trader


@pytest.mark.asyncio
async def test_dry_run_changes_nothing(db_session, make_user, no_cache_invalidation):
    user = await make_user(password=False, google=True)

    result = await promote_user_to_admin(db_session, user_id=user.id)

    assert result.committed is False
    assert (result.previous_role, result.new_role) == ("trader", "admin")
    assert await _role_in_db(db_session, user.id) is UserRole.trader
    no_cache_invalidation.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_admin_is_reported_not_re_revoked(
    db_session, make_user, no_cache_invalidation
):
    user = await make_user(password=True, google=False, role=UserRole.admin)

    result = await promote_user_to_admin(db_session, user_id=user.id, commit=True)

    assert result.already_admin is True
    assert result.revoked_refresh_tokens == 0
    assert await _role_in_db(db_session, user.id) is UserRole.admin
    no_cache_invalidation.assert_not_awaited()


# ----------------------------------------------------------- target rules


@pytest.mark.asyncio
async def test_unknown_target_is_refused(db_session):
    with pytest.raises(PromotionRefused) as exc:
        await promote_user_to_admin(db_session, username="no-such-user", commit=True)
    assert "사용자를 찾을 수 없습니다" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [{}, {"user_id": 1, "username": "u"}],
    ids=["no-target", "both-targets"],
)
async def test_target_must_be_exactly_one(db_session, kwargs):
    with pytest.raises(ValueError):
        await promote_user_to_admin(db_session, commit=True, **kwargs)


def test_cli_requires_an_explicit_target():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["--user-id", "4", "--username", "alice"])


def test_cli_defaults_to_dry_run():
    args = _build_parser().parse_args(["--username", "alice"])
    assert args.commit is False
    assert (args.user_id, args.username) == (None, "alice")


# ------------------------------------------------------------ CLI wiring


@contextlib.contextmanager
def _cli_session(db_session):
    """Point the CLI's session factory at the test session without closing it."""

    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    with patch("app.core.db.AsyncSessionLocal", factory):
        yield


@pytest.mark.asyncio
async def test_cli_dry_run_prints_the_effect_and_writes_nothing(
    db_session, make_user, capsys
):
    user = await make_user(password=False, google=True)
    args = _build_parser().parse_args(["--username", user.username])

    with _cli_session(db_session):
        exit_code = await _run(args)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "mode: DRY-RUN (no write)" in out
    assert "login_methods: google" in out
    assert "role: trader -> admin" in out
    assert await _role_in_db(db_session, user.id) is UserRole.trader


@pytest.mark.asyncio
async def test_cli_refusal_exits_nonzero(db_session, make_user, capsys):
    user = await make_user(password=False, google=False)
    args = _build_parser().parse_args(["--user-id", str(user.id), "--commit"])

    with _cli_session(db_session):
        exit_code = await _run(args)

    out = capsys.readouterr().out
    assert exit_code == 2
    assert out.startswith("refused: ")
    assert "로그인 수단이 없는 계정입니다" in out
    assert await _role_in_db(db_session, user.id) is UserRole.trader
