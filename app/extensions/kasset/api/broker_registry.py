"""Android-facing broker catalog built from existing Core capabilities."""

from app.extensions.kasset.api.schemas import Broker, BrokerCapabilities


class AndroidBrokerRegistry:
    """Expose stable provider names without changing Core broker enums."""

    def list_brokers(self) -> list[Broker]:
        return [
            Broker(
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
            ),
            Broker(
                provider="NH",
                display_name="NH투자증권 PLUG",
                connected=False,
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
            ),
            Broker(
                provider="KIS",
                display_name="한국투자증권",
                connected=False,
                implemented=False,
                requires_credential=True,
                supported_modes=[],
                mode="PREPARING",
                capabilities=BrokerCapabilities(),
            ),
            Broker(
                provider="TOSS",
                display_name="토스증권",
                connected=False,
                implemented=False,
                requires_credential=True,
                supported_modes=[],
                mode="PREPARING",
                capabilities=BrokerCapabilities(),
            ),
            Broker(
                provider="KB",
                display_name="KB증권",
                connected=False,
                implemented=False,
                requires_credential=True,
                supported_modes=[],
                mode="PREPARING",
                capabilities=BrokerCapabilities(),
            ),
        ]


broker_registry = AndroidBrokerRegistry()
