"""Bootstrap the first ``admin`` user (chicken-and-egg breaker for /admin/*).

``/admin/users`` can only change a role for a caller that is already an admin.
When a deployment has zero admins there is no in-product way to make the first
one, so this CLI is the only sanctioned path.

It is deliberately narrow:

* The target is **explicit** — exactly one of ``--user-id`` / ``--username``.
  There is no default and no "promote the newest account" heuristic.
* Dry-run is the **default**. ``--commit`` is required to write, matching the
  rest of ``scripts/``.
* An account with **no login means at all** (neither ``hashed_password`` nor
  ``google_sub``) is refused: promoting it would create an admin nobody can
  sign in as, and something else would have to hand it a credential later.
  A Google-only account (``google_sub`` set, no password) is a **normal**
  target and is promoted — that is what the operator's own account looks like.
* Password reset is **out of scope**. This CLI never touches
  ``hashed_password``.

On commit it performs exactly what ``PUT /admin/users/{id}/role`` performs for
a role change: set the role, revoke that user's refresh tokens (a token minted
under the old role must not outlive it), then best-effort drop the cached
session so the new role takes effect immediately instead of after the 5-minute
user cache expires.

Usage::

    # 1) inspect the effect, writes nothing
    uv run python -m scripts.promote_user_to_admin --username alice

    # 2) apply it
    uv run python -m scripts.promote_user_to_admin --username alice --commit

    # by id instead
    uv run python -m scripts.promote_user_to_admin --user-id 4 --commit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.token_repository import revoke_all_refresh_tokens
from app.models.trading import User, UserRole

logger = logging.getLogger(__name__)

NO_LOGIN_METHOD_REASON = (
    "로그인 수단이 없는 계정입니다 (hashed_password 없음 + google_sub 없음). "
    "승격하면 아무도 로그인할 수 없는 admin이 생깁니다."
)


class PromotionRefused(Exception):
    """The target exists but must not be promoted."""


@dataclass(frozen=True, slots=True)
class PromotionResult:
    user_id: int
    username: str | None
    email: str | None
    previous_role: str
    new_role: str
    login_methods: tuple[str, ...]
    committed: bool
    revoked_refresh_tokens: int
    already_admin: bool


def _login_methods(user: User) -> tuple[str, ...]:
    methods: list[str] = []
    if user.hashed_password:
        methods.append("password")
    if user.google_sub:
        methods.append("google")
    return tuple(methods)


async def _resolve_target(
    db: AsyncSession, *, user_id: int | None, username: str | None
) -> User:
    if (user_id is None) == (username is None):
        raise ValueError("--user-id 또는 --username 중 정확히 하나를 지정해야 합니다.")
    stmt = (
        select(User).where(User.id == user_id)
        if user_id is not None
        else select(User).where(User.username == username)
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        target = f"id={user_id}" if user_id is not None else f"username={username!r}"
        raise PromotionRefused(f"사용자를 찾을 수 없습니다 ({target}).")
    return user


async def promote_user_to_admin(
    db: AsyncSession,
    *,
    user_id: int | None = None,
    username: str | None = None,
    commit: bool = False,
) -> PromotionResult:
    """Promote exactly one explicitly named user to ``admin``.

    With ``commit=False`` (the default) nothing is written: the session is
    rolled back before returning, so the returned :class:`PromotionResult`
    describes the effect the operator would get, not one they took.
    """
    user = await _resolve_target(db, user_id=user_id, username=username)
    methods = _login_methods(user)
    if not methods:
        raise PromotionRefused(
            f"id={user.id} username={user.username!r}: {NO_LOGIN_METHOD_REASON}"
        )

    # Read identity before any rollback: Session.rollback() expires every
    # loaded instance, and a lazy re-load on an AsyncSession would raise
    # MissingGreenlet.
    target_id = user.id
    target_username = user.username
    target_email = user.email
    previous_role = user.role.value
    already_admin = user.role is UserRole.admin

    if not commit:
        await db.rollback()
        return PromotionResult(
            user_id=target_id,
            username=target_username,
            email=target_email,
            previous_role=previous_role,
            new_role=UserRole.admin.value,
            login_methods=methods,
            committed=False,
            revoked_refresh_tokens=0,
            already_admin=already_admin,
        )

    revoked = 0
    if not already_admin:
        user.role = UserRole.admin
        revoked = await revoke_all_refresh_tokens(db, target_id)
    await db.commit()

    if not already_admin:
        # Cached sessions still carry the old role for up to the user-cache
        # TTL. Best-effort, exactly like PUT /admin/users/{id}/role.
        try:
            from app.auth.web_router import invalidate_user_cache

            await invalidate_user_cache(target_id)
        except Exception:
            logger.warning(
                "Failed to invalidate cache for user_id=%s", target_id, exc_info=True
            )

    return PromotionResult(
        user_id=target_id,
        username=target_username,
        email=target_email,
        previous_role=previous_role,
        new_role=UserRole.admin.value,
        login_methods=methods,
        committed=True,
        revoked_refresh_tokens=revoked,
        already_admin=already_admin,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--user-id", type=int, help="승격할 사용자 id")
    target.add_argument("--username", help="승격할 사용자 username")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="실제로 role을 admin으로 바꾼다. 기본값은 dry-run(무변경)이다.",
    )
    return parser


def _print_result(result: PromotionResult) -> None:
    mode = "APPLIED (committed)" if result.committed else "DRY-RUN (no write)"
    print(f"mode: {mode}")
    print(f"  user_id: {result.user_id}")
    print(f"  username: {result.username}")
    print(f"  email: {result.email}")
    print(f"  login_methods: {', '.join(result.login_methods)}")
    print(f"  role: {result.previous_role} -> {result.new_role}")
    if result.already_admin:
        print("  note: 이미 admin입니다. 변경 없음.")
    if result.committed:
        print(f"  revoked_refresh_tokens: {result.revoked_refresh_tokens}")
    else:
        print("\n--commit 없이는 아무것도 쓰지 않았습니다.\n")


async def _run(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            result = await promote_user_to_admin(
                db,
                user_id=args.user_id,
                username=args.username,
                commit=args.commit,
            )
        except PromotionRefused as exc:
            print(f"refused: {exc}")
            return 2
    _print_result(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
