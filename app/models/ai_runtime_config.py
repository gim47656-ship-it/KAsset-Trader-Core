"""AI route 정책 singleton.

한 행만 존재하며(``id = 1``) ``route_policy``에는 lane별 **route ID 순서 배열**만
담는다. provider 이름, model 문자열, base URL, API key, subscription 명령은
어떤 컬럼에도 저장하지 않는다. route ID는 요청 시점의 서버 설정으로 resolve되므로
설정이 바뀌어도 저장된 정책이 낡은 model 문자열을 되살리지 않는다.

``revision``은 낙관적 잠금용 단조 증가 카운터다. 갱신은 ``SELECT ... FOR UPDATE``
로 이 행을 잡고 ``revision``이 기대값과 같을 때만 수행한다.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base

AI_RUNTIME_CONFIG_TABLE = "kasset_ai_runtime_config"

#: singleton 행의 유일한 기본키 값.
AI_RUNTIME_CONFIG_ID = 1


class AiRuntimeConfig(Base):
    """운영자가 저장한 AI route 정책 한 행."""

    __tablename__ = AI_RUNTIME_CONFIG_TABLE
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    route_policy: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = [
    "AI_RUNTIME_CONFIG_ID",
    "AI_RUNTIME_CONFIG_TABLE",
    "AiRuntimeConfig",
]
