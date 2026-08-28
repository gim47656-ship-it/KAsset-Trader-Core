"""Owner-scoped instructions for KAsset AI prompts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.nickname import ensure_user_nickname
from app.models.trading import User


async def build_owner_address_instruction(
    db: AsyncSession,
    owner_user_id: int,
) -> str:
    """Load the owner nickname and return the user-address prompt instruction."""

    user = await db.scalar(select(User).where(User.id == owner_user_id))
    if user is None:
        raise ValueError("AI prompt owner does not exist")
    nickname = await ensure_user_nickname(db, user)
    return f"사용자를 '{nickname}님'으로 부른다."


__all__ = ["build_owner_address_instruction"]
