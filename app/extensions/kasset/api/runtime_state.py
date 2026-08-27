"""Owner-scoped PAPER policy plus the global emergency kill switch."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.models import AndroidRuntimeState, GlobalRuntimeState


class AndroidRuntimeStateService:
    async def get(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        for_update: bool = False,
    ) -> AndroidRuntimeState:
        stmt = select(AndroidRuntimeState).where(
            AndroidRuntimeState.owner_user_id == owner_user_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await db.execute(stmt)
        state = result.scalar_one_or_none()
        if state is not None:
            return state

        state = AndroidRuntimeState(owner_user_id=owner_user_id)
        db.add(state)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            result = await db.execute(
                select(AndroidRuntimeState).where(
                    AndroidRuntimeState.owner_user_id == owner_user_id
                )
            )
            state = result.scalar_one_or_none()
            if state is None:
                raise
            return state
        await db.refresh(state)
        return state

    async def get_global(
        self,
        db: AsyncSession,
        *,
        for_update: bool = False,
    ) -> GlobalRuntimeState:
        stmt = select(GlobalRuntimeState).where(GlobalRuntimeState.id == 1)
        if for_update:
            stmt = stmt.with_for_update()
        result = await db.execute(stmt)
        state = result.scalar_one_or_none()
        if state is not None:
            return state

        state = GlobalRuntimeState(id=1)
        db.add(state)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            result = await db.execute(
                select(GlobalRuntimeState).where(GlobalRuntimeState.id == 1)
            )
            state = result.scalar_one_or_none()
            if state is None:
                raise
            return state
        await db.refresh(state)
        return state

    async def set_global_kill_switch(
        self,
        db: AsyncSession,
        *,
        enabled: bool,
    ) -> GlobalRuntimeState:
        state = await self.get_global(db, for_update=True)
        state.kill_switch_enabled = enabled
        await db.commit()
        await db.refresh(state)
        return state

    async def set_user_kill_switch(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        enabled: bool,
    ) -> AndroidRuntimeState:
        state = await self.get(db, owner_user_id, for_update=True)
        state.kill_switch_enabled = enabled
        await db.commit()
        await db.refresh(state)
        return state

    async def set_trading_mode(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        mode: str,
    ) -> AndroidRuntimeState:
        if mode.strip().upper() != "PAPER":
            raise MobileApiError(
                409,
                "LIVE_TRADING_DISABLED",
                "이번 단계에서는 PAPER 거래 모드만 사용할 수 있습니다.",
            )
        state = await self.get(db, owner_user_id, for_update=True)
        state.trading_mode = "PAPER"
        await db.commit()
        await db.refresh(state)
        return state

    async def update_policy(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        max_order_ratio: Decimal | None,
        max_symbol_ratio: Decimal | None,
    ) -> AndroidRuntimeState:
        state = await self.get(db, owner_user_id, for_update=True)
        if max_order_ratio is not None:
            state.max_order_ratio = max_order_ratio
        if max_symbol_ratio is not None:
            state.max_symbol_ratio = max_symbol_ratio
        await db.commit()
        await db.refresh(state)
        return state

    async def assert_order_allowed(
        self,
        db: AsyncSession,
        owner_user_id: int,
    ) -> AndroidRuntimeState:
        if not settings.TRADING_ENABLED:
            raise MobileApiError(
                403, "TRADING_DISABLED", "서버에서 거래가 꺼져 있습니다."
            )
        global_state = await self.get_global(db)
        state = await self.get(db, owner_user_id)
        if global_state.kill_switch_enabled or state.kill_switch_enabled:
            raise MobileApiError(
                403, "KILL_SWITCH_ON", "거래 중지 상태라 주문할 수 없습니다."
            )
        return state


runtime_state = AndroidRuntimeStateService()
