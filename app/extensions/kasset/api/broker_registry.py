"""Android-facing broker catalog built from existing Core capabilities."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings, validate_toss_api_config
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.schemas import Broker, BrokerCapabilities


class AndroidBrokerRegistry:
    """Expose stable provider names without changing Core broker enums."""

    async def list_brokers(
        self,
        db: AsyncSession,
        owner_user_id: int,
    ) -> list[Broker]:
        del db, owner_user_id
        return [self._paper(), self._toss()]

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
    def _toss() -> Broker:
        """Toss is the server-side market data source, never an app order path.

        Credentials live in the server environment, so the app never registers
        them, and the catalog must not advertise an order capability the
        Android contract keeps refusing.
        """

        return Broker(
            provider="TOSS",
            display_name="토스증권",
            connected=not validate_toss_api_config(settings),
            implemented=True,
            requires_credential=False,
            supported_modes=["LIVE_READ_ONLY"],
            mode="LIVE_READ_ONLY",
            capabilities=BrokerCapabilities(
                domestic_stock=True,
                us_stock=True,
                foreign_stock=True,
                rest=True,
                read_only=True,
            ),
        )


broker_registry = AndroidBrokerRegistry()
