"""Tests for unified WebSocket monitor."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.services.fill_notification import FillOrder


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AGENT_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_GATEWAY_URL", "http://agent/hooks/agent")
    monkeypatch.setattr(settings, "AGENT_GATEWAY_TOKEN", "test-token")


class TestUnifiedWebSocketMonitor:
    """통합 WebSocket 모니터 테스트"""

    @pytest.mark.asyncio
    async def test_on_upbit_trade_sends_notification(self, mock_settings: None) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        send_mock = AsyncMock()
        monitor._send_fill_notification = send_mock

        await monitor._on_upbit_order(
            {
                "code": "KRW-BTC",
                "ask_bid": "BID",
                "trade_price": 50_000_000,
                "trade_volume": 0.1,
                "state": "trade",
                "trade_timestamp": 1_700_000_000_000,
            }
        )

        send_mock.assert_awaited_once()
        fill_order = send_mock.call_args.args[0]
        assert isinstance(fill_order, FillOrder)
        assert fill_order.symbol == "KRW-BTC"
        assert fill_order.side == "bid"

    @pytest.mark.asyncio
    async def test_on_upbit_non_trade_ignored(self, mock_settings: None) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        send_mock = AsyncMock()
        monitor._send_fill_notification = send_mock

        await monitor._on_upbit_order(
            {
                "code": "KRW-BTC",
                "ask_bid": "BID",
                "trade_price": 50_000_000,
                "trade_volume": 0.1,
                "state": "wait",
            }
        )

        send_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_upbit_trade_records_ledger_before_notification(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        call_order: list[str] = []
        monitor = UnifiedWebSocketMonitor()

        async def record_side_effect(*args, **kwargs):
            call_order.append("ledger")

        async def send_side_effect(*args, **kwargs):
            call_order.append("notify")

        record_mock = AsyncMock(side_effect=record_side_effect)
        send_mock = AsyncMock(side_effect=send_side_effect)
        monitor._record_execution_ledger_fill = record_mock
        monitor._send_fill_notification = send_mock

        event = {
            "code": "KRW-BTC",
            "uuid": "upbit-order-1",
            "ask_bid": "BID",
            "trade_price": 50_000_000,
            "trade_volume": 0.1,
            "state": "trade",
            "trade_timestamp": 1_700_000_000_000,
        }
        await monitor._on_upbit_order(event)

        record_mock.assert_awaited_once()
        send_mock.assert_awaited_once()
        ledger_args = record_mock.await_args.args
        ledger_kwargs = record_mock.await_args.kwargs
        assert ledger_args[0] is event
        assert isinstance(ledger_args[1], FillOrder)
        assert ledger_kwargs == {"broker": "upbit", "correlation_id": "upbit-order-1"}
        assert call_order == ["ledger", "notify"]

    @pytest.mark.asyncio
    async def test_on_upbit_trade_projects_committed_fill_before_notification(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        call_order: list[str] = []
        monitor = UnifiedWebSocketMonitor(mode="upbit")

        async def record_side_effect(*args, **kwargs):
            call_order.append("ledger")
            return "inserted"

        async def project_side_effect(*args, **kwargs):
            call_order.append("projection")
            return True

        async def send_side_effect(*args, **kwargs):
            call_order.append("notify")

        monitor._record_execution_ledger_fill = AsyncMock(
            side_effect=record_side_effect
        )
        monitor._project_upbit_proposal_fill = AsyncMock(
            side_effect=project_side_effect
        )
        monitor._send_fill_notification = AsyncMock(side_effect=send_side_effect)

        event = {
            "code": "KRW-BTC",
            "uuid": "upbit-order-rob868",
            "identifier": "rob868-proposal-rung",
            "ask_bid": "BID",
            "trade_price": 92_800_000,
            "trade_volume": 0.0003,
            "executed_volume": "0.0003",
            "state": "trade",
            "trade_timestamp": 1_752_409_595_000,
        }

        await monitor._on_upbit_order(event)

        monitor._project_upbit_proposal_fill.assert_awaited_once_with(event)
        assert monitor._send_fill_notification.await_args is not None
        assert monitor._send_fill_notification.await_args.kwargs == {
            "proposal_rung_fill": True
        }
        assert call_order == ["ledger", "projection", "notify"]

    @pytest.mark.asyncio
    async def test_on_upbit_done_projects_terminal_fill(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor._record_execution_ledger_fill = AsyncMock(return_value="inserted")
        monitor._has_committed_upbit_execution_fill = AsyncMock(return_value=True)
        monitor._project_upbit_proposal_fill = AsyncMock(return_value=True)
        monitor._send_fill_notification = AsyncMock()
        event = {
            "code": "KRW-BTC",
            "uuid": "upbit-order-done",
            "identifier": "rob868-proposal-done",
            "ask_bid": "BID",
            "price": "92800000",
            "executed_volume": "0.0003",
            "state": "done",
            "timestamp": 1_752_409_596_000,
        }

        await monitor._on_upbit_order(event)

        monitor._record_execution_ledger_fill.assert_not_awaited()
        monitor._has_committed_upbit_execution_fill.assert_awaited_once_with(event)
        monitor._project_upbit_proposal_fill.assert_awaited_once_with(event)
        monitor._send_fill_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_upbit_done_without_committed_fill_skips_terminal_projection(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor._record_execution_ledger_fill = AsyncMock()
        monitor._has_committed_upbit_execution_fill = AsyncMock(return_value=False)
        monitor._project_upbit_proposal_fill = AsyncMock()
        event = {
            "code": "KRW-BTC",
            "uuid": "upbit-order-no-ledger",
            "identifier": "rob868-proposal-no-ledger",
            "state": "done",
            "executed_volume": "0.0003",
        }

        await monitor._on_upbit_order(event)

        monitor._record_execution_ledger_fill.assert_not_awaited()
        monitor._project_upbit_proposal_fill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_upbit_duplicate_projects_idempotently_and_notifies_once(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor._record_execution_ledger_fill = AsyncMock(
            side_effect=["inserted", "unchanged"]
        )
        monitor._project_upbit_proposal_fill = AsyncMock(side_effect=[True, False])
        monitor._send_fill_notification = AsyncMock()
        event = {
            "code": "KRW-BTC",
            "uuid": "upbit-order-duplicate-proposal",
            "identifier": "rob868-proposal-duplicate",
            "ask_bid": "BID",
            "trade_price": 92_800_000,
            "executed_volume": "0.0003",
            "state": "trade",
            "trade_timestamp": 1_752_409_595_000,
        }

        await monitor._on_upbit_order(event)
        await monitor._on_upbit_order(event)

        assert monitor._project_upbit_proposal_fill.await_count == 2
        monitor._send_fill_notification.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_upbit_duplicate_recovers_projection_and_small_alert(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor._record_execution_ledger_fill = AsyncMock(
            side_effect=["inserted", "unchanged"]
        )
        monitor._project_upbit_proposal_fill = AsyncMock(side_effect=[False, True])
        notification_attempts: list[bool] = []

        async def capture_notification(*args, **kwargs) -> None:
            notification_attempts.append(bool(kwargs.get("proposal_rung_fill")))

        monitor._send_fill_notification = AsyncMock(side_effect=capture_notification)
        event = {
            "code": "KRW-BTC",
            "uuid": "upbit-order-projection-retry",
            "identifier": "rob868-proposal-projection-retry",
            "ask_bid": "BID",
            "trade_price": 92_800_000,
            "volume": "0.0003",
            "executed_volume": "0.0003",
            "state": "trade",
            "trade_uuid": "upbit-trade-projection-retry",
            "trade_timestamp": 1_752_409_595_000,
        }

        await monitor._on_upbit_order(event)
        await monitor._on_upbit_order(event)

        assert notification_attempts == [False, True]

    @pytest.mark.asyncio
    async def test_on_upbit_projection_recovery_does_not_repeat_large_fill_alert(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor._record_execution_ledger_fill = AsyncMock(
            side_effect=["inserted", "unchanged"]
        )
        monitor._project_upbit_proposal_fill = AsyncMock(side_effect=[False, True])
        notification_attempts: list[bool] = []

        async def capture_notification(*args, **kwargs) -> None:
            notification_attempts.append(bool(kwargs.get("proposal_rung_fill")))

        monitor._send_fill_notification = AsyncMock(side_effect=capture_notification)
        event = {
            "code": "KRW-BTC",
            "uuid": "upbit-order-large-projection-retry",
            "identifier": "rob868-proposal-large-projection-retry",
            "ask_bid": "BID",
            "trade_price": 92_800_000,
            "volume": "0.001",
            "executed_volume": "0.001",
            "state": "trade",
            "trade_uuid": "upbit-trade-large-projection-retry",
            "trade_timestamp": 1_752_409_595_000,
        }

        await monitor._on_upbit_order(event)
        await monitor._on_upbit_order(event)

        assert notification_attempts == [False]

    @pytest.mark.asyncio
    async def test_on_upbit_trade_skips_notification_for_unchanged_ledger_row(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        record_mock = AsyncMock(return_value="unchanged")
        send_mock = AsyncMock()
        monitor._record_execution_ledger_fill = record_mock
        monitor._project_upbit_proposal_fill = AsyncMock(return_value=False)
        monitor._send_fill_notification = send_mock

        event = {
            "code": "KRW-BTC",
            "uuid": "upbit-order-duplicate",
            "ask_bid": "BID",
            "trade_price": 50_000_000,
            "trade_volume": 0.1,
            "state": "trade",
            "trade_timestamp": 1_700_000_000_000,
        }
        await monitor._on_upbit_order(event)

        record_mock.assert_awaited_once()
        send_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_record_execution_ledger_fill_is_inert_until_commit_gate(
        self, mock_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import websocket_monitor as mod
        from websocket_monitor import UnifiedWebSocketMonitor

        monkeypatch.setattr(settings, "EXECUTION_LEDGER_COMMIT_ENABLED", False)

        class FailingSession:
            async def __aenter__(self) -> object:  # pragma: no cover
                raise AssertionError("disabled websocket ledger path must not open DB")

            async def __aexit__(
                self, exc_type: object, exc: object, tb: object
            ) -> None:
                return None

        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: FailingSession())
        monitor = UnifiedWebSocketMonitor()

        status = await monitor._record_execution_ledger_fill(
            {"uuid": "upbit-order-disabled"},
            FillOrder(
                symbol="KRW-BTC",
                side="bid",
                filled_price=70_000,
                filled_qty=1,
                filled_amount=70_000,
                filled_at="2026-05-12T00:01:09Z",
                account="upbit",
                order_id="upbit-order-disabled",
                market_type="crypto",
                currency="KRW",
            ),
            correlation_id="corr-disabled",
        )

        assert status is None

    @pytest.mark.asyncio
    async def test_record_execution_ledger_fill_upserts_when_commit_gate_enabled(
        self, mock_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import websocket_monitor as mod
        from websocket_monitor import UnifiedWebSocketMonitor

        monkeypatch.setattr(settings, "EXECUTION_LEDGER_COMMIT_ENABLED", True)
        captured: dict[str, object] = {}

        class FakeSession:
            async def __aenter__(self) -> object:
                return self

            async def __aexit__(
                self, exc_type: object, exc: object, tb: object
            ) -> None:
                return None

            async def commit(self) -> None:
                captured["committed"] = True

        class FakeRepository:
            def __init__(self, db: object) -> None:
                captured["db"] = db

            async def upsert_fill(self, fill: object) -> tuple[str, int]:
                captured["fill"] = fill
                return "inserted", 42

        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr(mod, "ExecutionLedgerRepository", FakeRepository)
        monitor = UnifiedWebSocketMonitor()

        event = {
            "uuid": "upbit-order-1",
            "trade_uuid": "upbit-trade-1",
            "trade_timestamp": 1_747_008_069_000,
            "token": "secret",
        }
        order = FillOrder(
            symbol="KRW-BTC",
            side="bid",
            filled_price=1_959_000,
            filled_qty=1,
            filled_amount=1_959_000,
            filled_at="2026-05-12T00:01:09Z",
            account="upbit",
            order_id="upbit-order-1",
            market_type="crypto",
            currency="KRW",
        )
        status = await monitor._record_execution_ledger_fill(
            event,
            order,
            correlation_id=event["uuid"],
        )

        fill = captured["fill"]
        assert status == "inserted"
        assert captured["committed"] is True
        assert fill.broker == "upbit"
        assert fill.account_mode == "live"
        assert fill.venue == "upbit_krw"
        assert fill.instrument_type == "crypto"
        assert fill.symbol == "BTC"
        assert fill.side == "buy"
        assert fill.broker_order_id == "upbit-order-1"
        assert fill.fill_seq == monitor._ledger_fill_seq(event, order)
        assert str(fill.filled_qty) == "1"
        assert str(fill.filled_price) == "1959000"
        assert fill.currency == "KRW"
        assert fill.correlation_id == "upbit-order-1"
        assert fill.source == "websocket"
        assert fill.raw_payload_json["uuid"] == "upbit-order-1"
        assert fill.raw_payload_json["trade_uuid"] == "upbit-trade-1"
        assert fill.raw_payload_json["token"] == "[REDACTED]"

    @pytest.mark.asyncio
    async def test_has_committed_upbit_execution_fill_uses_exact_scope(
        self,
        mock_settings: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import websocket_monitor as mod
        from websocket_monitor import UnifiedWebSocketMonitor

        captured: dict[str, object] = {}

        class FakeSession:
            async def __aenter__(self) -> object:
                captured["session"] = self
                return self

            async def __aexit__(
                self, exc_type: object, exc: object, tb: object
            ) -> None:
                return None

        class FakeRepository:
            def __init__(self, db: object) -> None:
                assert db is captured["session"]

            async def has_fill_for_order(self, **kwargs: object) -> bool:
                captured["lookup"] = kwargs
                return True

        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr(mod, "ExecutionLedgerRepository", FakeRepository)
        monitor = UnifiedWebSocketMonitor(mode="upbit")

        exists = await monitor._has_committed_upbit_execution_fill(
            {"uuid": "upbit-order-durable", "venue": "upbit_krw"}
        )

        assert exists is True
        assert captured["lookup"] == {
            "broker": "upbit",
            "account_mode": "live",
            "venue": "upbit_krw",
            "broker_order_id": "upbit-order-durable",
        }

        unused_session_factory = MagicMock()
        monkeypatch.setattr(mod, "AsyncSessionLocal", unused_session_factory)
        assert not await monitor._has_committed_upbit_execution_fill({"uuid": ""})
        unused_session_factory.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "terminal_state"),
        [("trade", "partially_filled"), ("done", "filled")],
    )
    async def test_project_upbit_proposal_fill_uses_independent_committed_session(
        self,
        mock_settings: None,
        monkeypatch: pytest.MonkeyPatch,
        state: str,
        terminal_state: str,
    ) -> None:
        import websocket_monitor as mod
        from websocket_monitor import UnifiedWebSocketMonitor

        captured: dict[str, object] = {}

        class FakeSession:
            async def __aenter__(self) -> object:
                captured["session"] = self
                return self

            async def __aexit__(
                self, exc_type: object, exc: object, tb: object
            ) -> None:
                return None

            async def commit(self) -> None:
                captured["committed"] = True

        class FakeService:
            def __init__(self, db: object) -> None:
                assert db is captured["session"]

            async def record_fill_evidence(self, **kwargs: object) -> object:
                captured["evidence"] = kwargs
                return MagicMock(state=terminal_state)

        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr(mod, "OrderProposalsService", FakeService, raising=False)
        monitor = UnifiedWebSocketMonitor(mode="upbit")

        matched = await monitor._project_upbit_proposal_fill(
            {
                "state": state,
                "uuid": "upbit-order-id",
                "identifier": "proposal-client-id",
                "executed_volume": "0.0003",
            }
        )

        evidence = captured["evidence"]
        assert matched is True
        assert captured["committed"] is True
        assert evidence["idempotency_key"] == "proposal-client-id"
        assert evidence["broker_order_id"] == "upbit-order-id"
        assert evidence["filled_qty"] == Decimal("0.0003")
        assert evidence["terminal_state"] == terminal_state
        assert evidence["account_mode"] == "upbit"

    @pytest.mark.asyncio
    async def test_project_upbit_proposal_fill_logs_missing_match(
        self,
        mock_settings: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import websocket_monitor as mod
        from websocket_monitor import UnifiedWebSocketMonitor

        class FakeSession:
            async def __aenter__(self) -> object:
                return self

            async def __aexit__(
                self, exc_type: object, exc: object, tb: object
            ) -> None:
                return None

            async def commit(self) -> None:
                return None

        fake_service = MagicMock()
        fake_service.record_fill_evidence = AsyncMock(return_value=None)
        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr(
            mod,
            "OrderProposalsService",
            lambda _db: fake_service,
            raising=False,
        )
        caplog.set_level("INFO")

        matched = await UnifiedWebSocketMonitor(
            mode="upbit"
        )._project_upbit_proposal_fill(
            {
                "state": "trade",
                "uuid": "missing-order",
                "identifier": "missing-identifier",
                "executed_volume": "0.1",
            }
        )

        assert matched is False
        assert "no matching proposal rung" in caplog.text

    @pytest.mark.asyncio
    async def test_project_upbit_proposal_fill_skips_non_positive_cumulative_quantity(
        self,
        mock_settings: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import websocket_monitor as mod
        from websocket_monitor import UnifiedWebSocketMonitor

        session_factory = MagicMock()
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_factory)
        caplog.set_level("INFO")

        matched = await UnifiedWebSocketMonitor(
            mode="upbit"
        )._project_upbit_proposal_fill(
            {
                "state": "done",
                "uuid": "upbit-order-zero",
                "identifier": "proposal-zero",
                "executed_volume": "0",
            }
        )

        assert matched is False
        session_factory.assert_not_called()
        assert "missing cumulative fill" in caplog.text

    @pytest.mark.asyncio
    async def test_project_upbit_proposal_fill_logs_and_swallows_db_error(
        self,
        mock_settings: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import websocket_monitor as mod
        from websocket_monitor import UnifiedWebSocketMonitor

        class FailingSession:
            async def __aenter__(self) -> object:
                raise RuntimeError("proposal db unavailable")

            async def __aexit__(
                self, exc_type: object, exc: object, tb: object
            ) -> None:
                return None

        monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: FailingSession())
        caplog.set_level("ERROR")

        matched = await UnifiedWebSocketMonitor(
            mode="upbit"
        )._project_upbit_proposal_fill(
            {
                "state": "trade",
                "uuid": "upbit-order-error",
                "identifier": "proposal-error",
                "executed_volume": "0.1",
            }
        )

        assert matched is False
        assert "Upbit proposal rung projection failed" in caplog.text
        assert "proposal db unavailable" in caplog.text

    @pytest.mark.asyncio
    async def test_start_stops_when_child_task_fails(self, mock_settings: None) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()

        async def fail_upbit() -> None:
            raise RuntimeError("boom")

        monitor._start_upbit = fail_upbit  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="upbit task failed"):
            await monitor.start()

        assert monitor.is_running is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_start_reraises_cancelled_error_after_cleanup(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        started = asyncio.Event()

        async def wait_forever() -> None:
            started.set()
            await asyncio.Future()

        monitor._start_upbit = wait_forever  # type: ignore[method-assign]

        task = asyncio.create_task(monitor.start())
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.parametrize("mode", ["kis", "both", "invalid"])
    def test_removed_or_invalid_mode_raises_value_error(
        self, mock_settings: None, mode: str
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        with pytest.raises(ValueError, match="Invalid mode"):
            UnifiedWebSocketMonitor(mode=mode)

    @pytest.mark.asyncio
    async def test_stop_cleans_up_resources(self, mock_settings: None) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        upbit_disconnect = AsyncMock()

        monitor.upbit_ws = MagicMock()
        monitor.upbit_ws.disconnect = upbit_disconnect

        await monitor.stop()

        upbit_disconnect.assert_awaited_once()
        assert monitor.is_running is False

    @pytest.mark.asyncio
    async def test_send_fill_notification_calls_notify_fill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        fake_notifier = AsyncMock()
        fake_notifier.notify_fill = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "websocket_monitor.get_trade_notifier", lambda: fake_notifier
        )
        monkeypatch.setattr(
            "websocket_monitor.fetch_fill_enrichment", AsyncMock(return_value=None)
        )

        await monitor._send_fill_notification(
            FillOrder(
                symbol="KRW-BTC",
                side="bid",
                filled_price=92_800_000.0,
                filled_qty=0.001,
                filled_amount=92_800.0,
                filled_at="2026-06-14T09:31:02",
                account="upbit",
                market_type="crypto",
                currency="KRW",
            )
        )
        fake_notifier.notify_fill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_fill_notification_skips_below_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        fake_notifier = AsyncMock()
        fake_notifier.notify_fill = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "websocket_monitor.get_trade_notifier", lambda: fake_notifier
        )
        monkeypatch.setattr(
            "websocket_monitor.fetch_fill_enrichment", AsyncMock(return_value=None)
        )

        await monitor._send_fill_notification(
            FillOrder(
                symbol="KRW-BTC",
                side="bid",
                filled_price=68_500_000.0,
                filled_qty=0.0001,
                filled_amount=6850.0,  # below 50,000 KRW
                filled_at="2026-06-14T09:31:02",
                account="upbit",
                market_type="crypto",
                currency="KRW",
            )
        )
        fake_notifier.notify_fill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_fill_notification_sends_small_proposal_rung_fill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        fake_notifier = AsyncMock()
        fake_notifier.notify_fill = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "websocket_monitor.get_trade_notifier", lambda: fake_notifier
        )
        monkeypatch.setattr(
            "websocket_monitor.fetch_fill_enrichment", AsyncMock(return_value=None)
        )

        await monitor._send_fill_notification(
            FillOrder(
                symbol="KRW-BTC",
                side="bid",
                filled_price=92_800_000.0,
                filled_qty=0.0003,
                filled_amount=27_840.0,
                filled_at="2026-07-13T21:39:55+09:00",
                account="upbit",
                order_id="upbit-small-rung",
                market_type="crypto",
                currency="KRW",
            ),
            proposal_rung_fill=True,
        )

        fake_notifier.notify_fill.assert_awaited_once()
        assert monitor.fills_forwarded == 1

    @pytest.mark.asyncio
    async def test_enrichment_failure_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        fake_notifier = AsyncMock()
        fake_notifier.notify_fill = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "websocket_monitor.get_trade_notifier", lambda: fake_notifier
        )
        monkeypatch.setattr(
            "websocket_monitor.fetch_fill_enrichment",
            AsyncMock(side_effect=RuntimeError("boom")),
        )

        await monitor._send_fill_notification(
            FillOrder(
                symbol="KRW-BTC",
                side="bid",
                filled_price=68500.0,
                filled_qty=10.0,
                filled_amount=685000.0,
                filled_at="2026-06-14T09:31:02",
                account="upbit",
                market_type="crypto",
                currency="KRW",
            )
        )
        fake_notifier.notify_fill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upbit_consumer_updates_health_counters(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor._record_execution_ledger_fill = AsyncMock(return_value="unchanged")
        monitor._project_upbit_proposal_fill = AsyncMock(return_value=False)
        monitor._send_fill_notification = AsyncMock()

        await monitor._on_upbit_order({"state": "wait", "code": "KRW-BTC"})
        await monitor._on_upbit_order(
            {
                "state": "trade",
                "code": "KRW-BTC",
                "uuid": "upbit-health-order",
                "trade_price": 92_800_000,
                "executed_volume": "0.0003",
            }
        )

        assert monitor.upbit_messages_received == 2
        assert monitor.upbit_execution_events_received == 1
        assert monitor.upbit_last_message_at is not None
        assert monitor.upbit_last_execution_at is not None

    @pytest.mark.asyncio
    async def test_log_health_status_uses_upbit_consumer_counters(
        self,
        mock_settings: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor.is_running = True
        monitor._started_at_monotonic = asyncio.get_running_loop().time() - 5
        monitor.upbit_ws = MagicMock(is_connected=True)
        monitor.upbit_messages_received = 7
        monitor.upbit_execution_events_received = 3
        monitor.upbit_last_message_at = "2026-07-13T12:39:55+00:00"
        monitor.upbit_last_execution_at = "2026-07-13T12:39:56+00:00"

        caplog.set_level("INFO")
        monitor._log_health_status(force=True)

        assert "messages_received=7" in caplog.text
        assert "execution_events_received=3" in caplog.text
        assert "last_message_at=2026-07-13T12:39:55+00:00" in caplog.text
        assert "last_execution_at=2026-07-13T12:39:56+00:00" in caplog.text

    @pytest.mark.asyncio
    async def test_log_health_status_throttles_when_not_forced(
        self, mock_settings: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        monitor._next_health_log_at = asyncio.get_running_loop().time() + 60

        caplog.set_level("INFO")
        monitor._log_health_status(force=False)

        assert "Upbit WebSocket health" not in caplog.text

    @pytest.mark.asyncio
    async def test_main_configures_and_shuts_down_trade_notifier(
        self, mock_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import websocket_monitor

        monitor = MagicMock()
        monitor.start = AsyncMock(return_value=None)
        monitor.stop = AsyncMock(return_value=None)

        notifier = MagicMock()
        notifier.configure = MagicMock()
        notifier.shutdown = AsyncMock(return_value=None)

        monkeypatch.setattr(settings, "telegram_token", "telegram-token")
        monkeypatch.setattr(settings, "telegram_chat_id", "123456")
        # Set Discord webhooks to None/empty to test Telegram-only config
        monkeypatch.setattr(settings, "discord_webhook_us", None)
        monkeypatch.setattr(settings, "discord_webhook_kr", None)
        monkeypatch.setattr(settings, "discord_webhook_crypto", None)
        monkeypatch.setattr(settings, "discord_webhook_alerts", None)
        monkeypatch.setattr(websocket_monitor, "init_sentry", lambda **_: None)
        monkeypatch.setattr(
            websocket_monitor,
            "UnifiedWebSocketMonitor",
            lambda mode="upbit": monitor,
        )
        monkeypatch.setattr(
            websocket_monitor,
            "get_trade_notifier",
            lambda: notifier,
            raising=False,
        )

        await websocket_monitor.main(mode="upbit")

        notifier.configure.assert_called_once_with(
            bot_token="telegram-token",
            chat_ids=["123456"],
            enabled=True,
            discord_webhook_us=None,
            discord_webhook_kr=None,
            discord_webhook_crypto=None,
            discord_webhook_alerts=None,
        )
        notifier.shutdown.assert_awaited_once()
        monitor.start.assert_awaited_once()
        monitor.stop.assert_awaited_once()


class TestHeartbeat:
    """Tests for heartbeat file writing."""

    def test_write_heartbeat_creates_file(self, mock_settings: None, tmp_path) -> None:
        """Test that _write_heartbeat creates heartbeat file."""
        from websocket_monitor import UnifiedWebSocketMonitor

        heartbeat_file = tmp_path / "heartbeat.json"
        monitor = UnifiedWebSocketMonitor()
        monitor._heartbeat_path = str(heartbeat_file)
        monitor._write_heartbeat()

        assert heartbeat_file.exists()

    def test_write_heartbeat_content(self, mock_settings: None, tmp_path) -> None:
        """Test that _write_heartbeat writes correct content."""
        import json
        import time

        from websocket_monitor import UnifiedWebSocketMonitor

        heartbeat_file = tmp_path / "heartbeat.json"
        monitor = UnifiedWebSocketMonitor()
        monitor._heartbeat_path = str(heartbeat_file)
        monitor._write_heartbeat()

        with open(heartbeat_file) as f:
            data = json.load(f)

        assert "updated_at_unix" in data
        assert data["mode"] == "upbit"
        assert data["is_running"] is False  # Default
        assert data["upbit_connected"] is False
        assert "kis_connected" not in data
        # Verify timestamp is recent
        assert time.time() - data["updated_at_unix"] < 2

    def test_write_heartbeat_with_override(self, mock_settings: None, tmp_path) -> None:
        """Test that _write_heartbeat respects is_running override."""
        import json

        from websocket_monitor import UnifiedWebSocketMonitor

        heartbeat_file = tmp_path / "heartbeat.json"
        monitor = UnifiedWebSocketMonitor()
        monitor._heartbeat_path = str(heartbeat_file)
        monitor._write_heartbeat(is_running=True)

        with open(heartbeat_file) as f:
            data = json.load(f)

        assert data["is_running"] is True

    def test_write_heartbeat_mode_upbit(self, mock_settings: None, tmp_path) -> None:
        """Test heartbeat shows correct connection status for upbit mode."""
        import json

        from websocket_monitor import UnifiedWebSocketMonitor

        heartbeat_file = tmp_path / "heartbeat.json"
        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor._heartbeat_path = str(heartbeat_file)
        monitor._write_heartbeat()

        with open(heartbeat_file) as f:
            data = json.load(f)

        assert data["mode"] == "upbit"
        assert data["upbit_connected"] is False
        assert "kis_connected" not in data

    def test_write_heartbeat_creates_parent_dir(
        self, mock_settings: None, tmp_path
    ) -> None:
        """Test that _write_heartbeat creates parent directories."""
        import json

        from websocket_monitor import UnifiedWebSocketMonitor

        heartbeat_file = tmp_path / "nested" / "dir" / "heartbeat.json"
        monitor = UnifiedWebSocketMonitor()
        monitor._heartbeat_path = str(heartbeat_file)
        monitor._write_heartbeat()

        assert heartbeat_file.exists()
        with open(heartbeat_file) as f:
            data = json.load(f)
        assert "updated_at_unix" in data


class TestAutoReconnect:
    """Tests for auto-reconnect supervisor behavior."""

    def test_reconnect_delay_configurable(self, mock_settings: None) -> None:
        """Test that reconnect delay is configurable."""
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor._reconnect_delay_seconds = 5.0

        assert monitor._reconnect_delay_seconds == pytest.approx(5.0)

    def test_heartbeat_path_configurable(
        self, mock_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that heartbeat path is configurable via environment."""
        from websocket_monitor import UnifiedWebSocketMonitor

        monkeypatch.setenv("WS_MONITOR_HEARTBEAT_PATH", "/custom/heartbeat.json")
        monitor = UnifiedWebSocketMonitor()
        assert monitor._heartbeat_path == "/custom/heartbeat.json"

    def test_heartbeat_interval_configurable(
        self, mock_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that heartbeat interval is configurable via environment."""
        from websocket_monitor import UnifiedWebSocketMonitor

        monkeypatch.setenv("WS_MONITOR_HEARTBEAT_INTERVAL_SECONDS", "30")
        monitor = UnifiedWebSocketMonitor()
        assert monitor._heartbeat_interval_seconds == pytest.approx(30.0)

    def test_health_log_interval_defaults_to_five_minutes(
        self, mock_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monkeypatch.delenv("WS_MONITOR_HEALTH_LOG_INTERVAL_SECONDS", raising=False)
        monitor = UnifiedWebSocketMonitor()
        assert monitor._health_log_interval_seconds == pytest.approx(300.0)

    @pytest.mark.asyncio
    async def test_supervisor_exits_on_stop_before_start(
        self, mock_settings: None
    ) -> None:
        """Test that supervisor exits immediately when is_running is False."""
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor.is_running = False

        # Should exit immediately without attempting connection
        await monitor._start_upbit_supervisor()
        # No assertion needed - just verify it returns cleanly

    @pytest.mark.asyncio
    async def test_upbit_supervisor_reconnects_when_connection_not_established(
        self,
        mock_settings: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import websocket_monitor
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor.is_running = True

        class FakeUpbitWs:
            def __init__(self, *args, **kwargs):
                self.is_connected = False
                self.connect_and_subscribe = AsyncMock(return_value=None)

        fake_ws = FakeUpbitWs()

        def fake_factory(*args, **kwargs):
            return fake_ws

        async def stop_after_first_sleep(_: float) -> None:
            monitor.is_running = False

        monkeypatch.setattr(websocket_monitor, "UpbitMyOrderWebSocket", fake_factory)
        monkeypatch.setattr(websocket_monitor.asyncio, "sleep", stop_after_first_sleep)
        caplog.set_level("INFO")

        await monitor._start_upbit_supervisor()

        fake_ws.connect_and_subscribe.assert_awaited_once()
        assert "Upbit WebSocket connected" not in caplog.text
        assert "Reconnecting Upbit" in caplog.text
