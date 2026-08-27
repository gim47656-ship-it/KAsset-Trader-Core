"""Android-facing broker catalog built from existing Core capabilities."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.credential_vault import credential_vault
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper import iso_z
from app.extensions.kasset.api.schemas import Broker, BrokerCapabilities
from app.services.brokers.nhplug.gating import mock_enabled


class AndroidBrokerRegistry:
    """Expose stable provider names without changing Core broker enums."""

    async def list_brokers(
        self,
        db: AsyncSession,
        owner_user_id: int,
    ) -> list[Broker]:
        return [
            self._paper(),
            await self._nh(db, owner_user_id),
            self._preparing("KIS", "한국투자증권"),
            self._preparing("TOSS", "토스증권"),
            self._preparing("KB", "KB증권"),
        ]

    async def get_broker(
        self,
        db: AsyncSession,
        owner_user_id: int,
        provider: str,
    ) -> Broker:
        normalized = provider.strip().upper()
        for broker in await self.list_brokers(db, owner_user_id):
            if broker.provider == normalized:
                return broker
        raise MobileApiError(404, "BROKER_NOT_FOUND", "지원하지 않는 브로커입니다.")

    @staticmethod
    def _paper() -> Broker:
        return Broker(
            provider="PAPER",
            display_name="연습(Paper)",
            connected=True,
            implemented=True,
            requires_credential=False,
            supported_modes=["PAPER"],
            mode="PAPER",
            capabilities=BrokerCapabilities(
                domestic_stock=True,
                us_stock=True,
                foreign_stock=True,
                rest=True,
                paper_trading=True,
                market_order=True,
                limit_order=True,
                read_only=False,
            ),
        )

    @staticmethod
    async def _nh(db: AsyncSession, owner_user_id: int) -> Broker:
        record = await credential_vault.record(db, owner_user_id, "NH")
        credential_id: str | None = None
        credential_readable = False
        app_key_masked: str | None = None
        account_no_masked: str | None = None
        last_verified_at: str | None = None
        if record is not None:
            credential_id = record.id
            last_verified_at = (
                iso_z(record.last_verified_at)
                if record.last_verified_at is not None
                else None
            )
            try:
                revealed = await credential_vault.reveal_nh(db, owner_user_id)
            except MobileApiError:
                revealed = None
            if revealed is not None:
                credential_readable = True
                app_key_masked = credential_vault.mask(revealed.app_key)
                account_no_masked = credential_vault.mask(revealed.account_no)
        return Broker(
            provider="NH",
            display_name="NH투자증권 PLUG",
            connected=(
                last_verified_at is not None and credential_readable and mock_enabled()
            ),
            credential_id=credential_id,
            app_key_masked=app_key_masked,
            account_no_masked=account_no_masked,
            last_verified_at=last_verified_at,
            implemented=True,
            requires_credential=True,
            supported_modes=["MOCK_READ_ONLY"],
            mode="MOCK_READ_ONLY",
            capabilities=BrokerCapabilities(
                domestic_stock=True,
                rest=True,
                paper_trading=True,
                read_only=True,
            ),
        )

    @staticmethod
    def _preparing(provider: str, display_name: str) -> Broker:
        return Broker(
            provider=provider,
            display_name=display_name,
            connected=False,
            implemented=False,
            requires_credential=True,
            supported_modes=[],
            mode="PREPARING",
            capabilities=BrokerCapabilities(),
        )


broker_registry = AndroidBrokerRegistry()
