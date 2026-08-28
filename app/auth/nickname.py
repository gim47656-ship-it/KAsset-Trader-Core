"""Nickname generation and persistence helpers."""

from __future__ import annotations

import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading import User

ADJECTIVES: tuple[str, ...] = (
    "침착한",
    "용감한",
    "밝은",
    "다정한",
    "신중한",
    "활기찬",
    "차분한",
    "재빠른",
    "성실한",
    "영리한",
    "따뜻한",
    "든든한",
)
ANIMALS: tuple[str, ...] = (
    "수달",
    "여우",
    "판다",
    "호랑이",
    "토끼",
    "사슴",
    "고래",
    "독수리",
    "펭귄",
    "다람쥐",
    "해달",
    "부엉이",
)


def generate_random_nickname() -> str:
    """Return a Korean adjective-animal nickname with a two-digit suffix."""

    return (
        f"{secrets.choice(ADJECTIVES)}{secrets.choice(ANIMALS)}"
        f"{secrets.randbelow(100):02d}"
    )


def normalize_nickname(value: str) -> str:
    """Strip and validate a user-selected nickname."""

    nickname = value.strip()
    if not 1 <= len(nickname) <= 16:
        raise ValueError("nickname must contain between 1 and 16 characters")
    return nickname


async def ensure_user_nickname(db: AsyncSession, user: User) -> str:
    """Persist a generated nickname for a legacy user when it is missing."""

    if user.nickname is None:
        user.nickname = generate_random_nickname()
        await db.commit()
        await db.refresh(user)
    return user.nickname


__all__ = [
    "ADJECTIVES",
    "ANIMALS",
    "ensure_user_nickname",
    "generate_random_nickname",
    "normalize_nickname",
]
