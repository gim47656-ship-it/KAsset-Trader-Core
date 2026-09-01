#!/usr/bin/env python3
"""Upbit execution WebSocket monitor."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.logging_config import configure_dependency_log_levels
from app.monitoring.sentry import capture_exception, init_sentry
from app.monitoring.trade_notifier import get_trade_notifier
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger.normalizers import _redact_sensitive_keys
from app.services.execution_ledger.repository import (
    ExecutionLedgerRepository,
    UpsertStatus,
)
from app.services.fill_enrichment import fetch_fill_enrichment
from app.services.fill_notification import (
    FillOrder,
    is_fill_notifiable,
    normalize_upbit_fill,
)
from app.services.order_proposals import OrderProposalsService
from app.services.upbit_websocket import UpbitMyOrderWebSocket

logger = logging.getLogger(__name__)
VALID_MONITOR_MODES = {"upbit"}

# Default heartbeat configuration
DEFAULT_HEARTBEAT_PATH = "/tmp/websocket_monitor_heartbeat.json"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0
DEFAULT_HEALTH_LOG_INTERVAL_SECONDS = 300.0
DEFAULT_RECONNECT_DELAY_SECONDS = 5.0


class UnifiedWebSocketMonitor:
    """Upbit 체결 이벤트를 수신해 체결 장부와 알림으로 전달합니다."""

    def __init__(self, mode: str = "upbit"):
        if mode not in VALID_MONITOR_MODES:
            raise ValueError(
                f"Invalid mode '{mode}'. Expected one of: {sorted(VALID_MONITOR_MODES)}"
            )

        self.mode = mode
        self.is_running = False
        self.upbit_ws: UpbitMyOrderWebSocket | None = None
        self._health_log_interval_seconds = float(
            os.environ.get(
                "WS_MONITOR_HEALTH_LOG_INTERVAL_SECONDS",
                str(DEFAULT_HEALTH_LOG_INTERVAL_SECONDS),
            )
        )
        self._next_health_log_at = 0.0
        self._started_at_monotonic: float | None = None
        self.fills_forwarded = 0
        self.last_agent_success_at: str | None = None
        self.upbit_messages_received = 0
        self.upbit_execution_events_received = 0
        self.upbit_last_message_at: str | None = None
        self.upbit_last_execution_at: str | None = None
        self._setup_signal_handlers()

        # Heartbeat configuration from environment
        self._heartbeat_path = os.environ.get(
            "WS_MONITOR_HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH
        )
        self._heartbeat_interval_seconds = float(
            os.environ.get(
                "WS_MONITOR_HEARTBEAT_INTERVAL_SECONDS",
                str(DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
            )
        )
        self._reconnect_delay_seconds = float(
            os.environ.get(
                "WS_MONITOR_RECONNECT_DELAY_SECONDS",
                str(DEFAULT_RECONNECT_DELAY_SECONDS),
            )
        )
        self._last_heartbeat_at = 0.0

    def _setup_signal_handlers(self):
        """SIGINT/SIGTERM 시그널 핸들러 설정"""
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        logger.info("Signal handlers installed (SIGINT, SIGTERM)")

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """시그널 수신 시 graceful shutdown"""
        sig_name = signal.Signals(signum).name
        logger.info(f"Received signal {sig_name} ({signum}), initiating shutdown...")
        self.is_running = False

    def _write_heartbeat(self, is_running: bool | None = None) -> None:
        """
        Write heartbeat file atomically.

        Args:
            is_running: Override for is_running status. If None, uses self.is_running.
        """
        import time

        if is_running is None:
            is_running = self.is_running

        upbit_connected = bool(self.upbit_ws and self.upbit_ws.is_connected)

        data = {
            "updated_at_unix": time.time(),
            "mode": self.mode,
            "is_running": is_running,
            "upbit_connected": upbit_connected,
        }

        # Atomic write: write to temp file, then rename
        heartbeat_path = Path(self._heartbeat_path)
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = heartbeat_path.with_suffix(".tmp")

        try:
            with open(temp_path, "w") as f:
                json.dump(data, f)
            temp_path.replace(heartbeat_path)
        except OSError as e:
            logger.warning("Failed to write heartbeat file: %s", e)

    async def _on_upbit_order(self, order_data: dict[str, Any]) -> None:
        """
        Upbit 주문/체결 이벤트 처리

        state가 ``trade`` 또는 ``done``인 체결을 처리합니다.
        """
        received_at = datetime.now(UTC).isoformat()
        self.upbit_messages_received += 1
        self.upbit_last_message_at = received_at
        state = str(order_data.get("state") or "")
        if state not in {"trade", "done"}:
            logger.debug(f"Upbit non-trade state ignored: {state}")
            return
        self.upbit_execution_events_received += 1
        self.upbit_last_execution_at = received_at

        try:
            if state == "done":
                # ``done`` carries order-level cumulative executed_volume, not
                # a new trade delta. The preceding ``trade`` event owns the
                # execution-ledger row; terminal evidence only closes the rung.
                if not await self._has_committed_upbit_execution_fill(order_data):
                    logger.warning(
                        "Upbit terminal projection skipped without committed fill: "
                        "order_id=%s",
                        order_data.get("uuid"),
                    )
                    return
                await self._project_upbit_proposal_fill(order_data)
                logger.info(
                    "Upbit terminal fill evidence processed: order_id=%s",
                    order_data.get("uuid"),
                )
                return
            fill_order = normalize_upbit_fill(order_data)
            upsert_status = await self._record_execution_ledger_fill(
                order_data,
                fill_order,
                correlation_id=str(order_data.get("uuid") or "n/a"),
            )
            proposal_rung_fill = False
            if upsert_status is not None:
                proposal_rung_fill = await self._project_upbit_proposal_fill(order_data)
            duplicate_ledger_fill = upsert_status in {"updated", "unchanged"}
            recover_suppressed_small_alert = (
                duplicate_ledger_fill
                and proposal_rung_fill
                and not is_fill_notifiable(fill_order)
            )
            if not duplicate_ledger_fill or recover_suppressed_small_alert:
                await self._send_fill_notification(
                    fill_order,
                    proposal_rung_fill=proposal_rung_fill,
                )
            else:
                logger.info(
                    "Upbit fill notification skipped for duplicate ledger row: symbol=%s status=%s",
                    fill_order.symbol,
                    upsert_status,
                )
            logger.info(
                f"Upbit fill processed: {fill_order.symbol} {fill_order.side} "
                f"{fill_order.filled_qty}@{fill_order.filled_price}"
            )
        except Exception as e:
            logger.error(f"Upbit fill processing error: {e}", exc_info=True)

    async def _has_committed_upbit_execution_fill(
        self, order_data: dict[str, Any]
    ) -> bool:
        """Check the durable ledger before applying cumulative terminal evidence."""
        broker_order_id = str(order_data.get("uuid") or "").strip()
        if not broker_order_id:
            return False
        venue = str(order_data.get("venue") or "upbit_krw")
        async with AsyncSessionLocal() as db:
            return await ExecutionLedgerRepository(db).has_fill_for_order(
                broker="upbit",
                account_mode="live",
                venue=venue,
                broker_order_id=broker_order_id,
            )

    async def _project_upbit_proposal_fill(self, order_data: dict[str, Any]) -> bool:
        """Best-effort projection of committed Upbit evidence into one rung."""
        state = str(order_data.get("state") or "")
        terminal_state = {
            "trade": "partially_filled",
            "done": "filled",
        }.get(state)
        if terminal_state is None:
            return False

        broker_order_id = str(order_data.get("uuid") or "").strip() or None
        identifier = str(order_data.get("identifier") or "").strip() or None
        try:
            filled_qty = Decimal(str(order_data.get("executed_volume") or "0"))
            if filled_qty <= 0:
                logger.info(
                    "Upbit proposal rung projection skipped: missing cumulative fill "
                    "order_id=%s identifier=%s state=%s",
                    broker_order_id,
                    identifier,
                    state,
                )
                return False
            async with AsyncSessionLocal() as db:
                rung = await OrderProposalsService(db).record_fill_evidence(
                    idempotency_key=identifier,
                    broker_order_id=broker_order_id,
                    filled_qty=filled_qty,
                    terminal_state=terminal_state,
                    now=datetime.now(UTC),
                    account_mode="upbit",
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - ledger commit remains authoritative
            logger.error(
                "Upbit proposal rung projection failed: order_id=%s identifier=%s "
                "state=%s error=%s",
                broker_order_id,
                identifier,
                state,
                exc,
                exc_info=True,
            )
            return False

        if rung is None:
            logger.info(
                "Upbit proposal rung projection found no matching proposal rung: "
                "order_id=%s identifier=%s state=%s",
                broker_order_id,
                identifier,
                state,
            )
            return False
        logger.info(
            "Upbit proposal rung projected: order_id=%s identifier=%s state=%s "
            "rung_state=%s cumulative_filled_qty=%s",
            broker_order_id,
            identifier,
            state,
            rung.state,
            filled_qty,
        )
        return True

    @staticmethod
    def _ledger_symbol(order: FillOrder) -> str:
        symbol = order.symbol.strip().upper()
        if order.market_type == "crypto":
            for prefix in ("KRW-", "USDT-"):
                if symbol.startswith(prefix):
                    return symbol[len(prefix) :]
        return symbol

    @staticmethod
    def _ledger_side(order: FillOrder) -> str:
        return "sell" if order.side in {"ask", "sell"} else "buy"

    @staticmethod
    def _ledger_instrument_type(order: FillOrder) -> str:
        if order.market_type == "us":
            return "equity_us"
        if order.market_type == "crypto":
            return "crypto"
        return "equity_kr"

    @staticmethod
    def _ledger_fill_seq(event: dict[str, Any], order: FillOrder) -> int:
        for key in ("fill_seq", "ccld_seq", "ccld_seq_no", "trade_seq"):
            value = event.get(key)
            if value not in (None, ""):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
        seed = "|".join(
            str(part or "")
            for part in (
                event.get("trade_uuid"),
                event.get("trade_id"),
                event.get("uuid"),
                order.order_id,
                order.symbol,
                order.filled_at,
                order.filled_qty,
                order.filled_price,
            )
        )
        return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) & 2_147_483_647

    async def _record_execution_ledger_fill(
        self,
        event: dict[str, Any],
        order: FillOrder,
        *,
        correlation_id: str | None = None,
    ) -> UpsertStatus | None:
        """Durably upsert one websocket fill before downstream notification.

        The existing execution-ledger commit flag is the explicit activation gate;
        when disabled we preserve notification behavior and avoid surprise writes.
        """
        if not settings.EXECUTION_LEDGER_COMMIT_ENABLED:
            logger.info(
                "Execution ledger websocket insert skipped: commit flag disabled symbol=%s broker=upbit",
                order.symbol,
            )
            return None

        currency = order.currency or ("USD" if order.market_type == "us" else "KRW")
        venue = str(event.get("venue") or "upbit_krw")
        broker_order_id = str(
            order.order_id or correlation_id or event.get("order_id") or ""
        )
        if not broker_order_id:
            broker_order_id = f"websocket-{self._ledger_fill_seq(event, order)}"

        fill = ExecutionLedgerUpsert(
            broker="upbit",
            account_mode="live",
            venue=venue,
            instrument_type=self._ledger_instrument_type(order),  # type: ignore[arg-type]
            symbol=self._ledger_symbol(order),
            raw_symbol=order.symbol,
            side=self._ledger_side(order),  # type: ignore[arg-type]
            broker_order_id=broker_order_id,
            fill_seq=self._ledger_fill_seq(event, order),
            filled_qty=Decimal(str(order.filled_qty)),
            filled_price=Decimal(str(order.filled_price)),
            filled_notional=Decimal(str(order.filled_amount))
            if order.filled_amount
            else None,
            fee_amount=None,
            fee_currency=currency,
            filled_at=order.filled_at,  # type: ignore[arg-type]
            currency=currency,  # type: ignore[arg-type]
            correlation_id=correlation_id,
            source="websocket",
            raw_payload_json=_redact_sensitive_keys(event),
        )
        async with AsyncSessionLocal() as db:
            status, row_id = await ExecutionLedgerRepository(db).upsert_fill(fill)
            await db.commit()
        logger.info(
            "Execution ledger websocket upsert committed: broker=upbit symbol=%s order_id=%s fill_seq=%s status=%s row_id=%s",
            fill.symbol,
            fill.broker_order_id,
            fill.fill_seq,
            status,
            row_id,
        )
        return status

    async def _send_fill_notification(
        self,
        order: FillOrder,
        *,
        correlation_id: str | None = None,
        proposal_rung_fill: bool = False,
    ) -> None:
        """체결 알림: 통화 임계 → best-effort 보강 → TradeNotifier (fire-and-forget)."""
        if not proposal_rung_fill and not is_fill_notifiable(order):
            logger.info(
                "Fill notification skipped: below threshold symbol=%s amount=%s currency=%s",
                order.symbol,
                order.filled_amount,
                order.currency,
            )
            return

        enrichment = None
        try:
            enrichment = await fetch_fill_enrichment(order)
        except Exception:
            logger.warning(
                "Fill enrichment error (fail-open): symbol=%s",
                order.symbol,
                exc_info=True,
            )

        from app.core.portfolio_links import build_position_detail_url

        detail_url = build_position_detail_url(order.symbol, order.market_type)

        logger.info(
            "Fill notification send start: correlation_id=%s symbol=%s account=%s amount=%s",
            correlation_id,
            order.symbol,
            order.account,
            order.filled_amount,
        )
        try:
            ok = await get_trade_notifier().notify_fill(
                order,
                enrichment=enrichment,
                detail_url=detail_url,
            )
            if ok:
                self.fills_forwarded += 1
                self.last_agent_success_at = datetime.now(UTC).isoformat()
                logger.info(
                    "Fill notification sent: correlation_id=%s symbol=%s result=success",
                    correlation_id,
                    order.symbol,
                )
            else:
                logger.warning(
                    "Fill notification not delivered: correlation_id=%s symbol=%s",
                    correlation_id,
                    order.symbol,
                )
        except Exception as e:
            logger.error(
                "Fill notification error: correlation_id=%s symbol=%s error=%s",
                correlation_id,
                order.symbol,
                e,
                exc_info=True,
            )

    async def _start_upbit_supervisor(self) -> None:
        """
        Upbit WebSocket supervisor loop with auto-reconnect.

        When connection closes and is_running=True, reconnects after delay.
        Only exits when is_running=False (stop signal).
        """
        while self.is_running:
            try:
                self.upbit_ws = UpbitMyOrderWebSocket(
                    on_order_callback=self._on_upbit_order,
                    verify_ssl=True,
                )
                logger.info("Connecting to Upbit WebSocket...")
                await self.upbit_ws.connect_and_subscribe()
                if self.upbit_ws.is_connected is not True:
                    raise RuntimeError("Upbit WebSocket connection not established")
                logger.info("Upbit WebSocket connected")

                # Connection closed normally - check if we should reconnect
                if self.is_running:
                    logger.warning(
                        "Upbit WebSocket connection closed, reconnecting in %.1fs...",
                        self._reconnect_delay_seconds,
                    )
                    await asyncio.sleep(self._reconnect_delay_seconds)
                else:
                    logger.info("Upbit WebSocket exiting (stop signal)")
                    break
            except Exception as e:
                logger.error("Upbit WebSocket error: %s", e, exc_info=True)
                if self.is_running:
                    logger.info(
                        "Reconnecting Upbit in %.1fs...", self._reconnect_delay_seconds
                    )
                    await asyncio.sleep(self._reconnect_delay_seconds)
                else:
                    raise

    async def _start_upbit(self) -> None:
        """Upbit WebSocket 시작 (supervisor wrapper)."""
        await self._start_upbit_supervisor()

    def _log_health_status(self, *, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if not force and now < self._next_health_log_at:
            return

        self._next_health_log_at = now + self._health_log_interval_seconds

        upbit_connected = bool(self.upbit_ws and self.upbit_ws.is_connected)
        connected = upbit_connected
        uptime = 0.0
        if self._started_at_monotonic is not None:
            uptime = round(now - self._started_at_monotonic, 1)
        runtime_snapshot = self._current_runtime_stats_snapshot()
        logger.info(
            "Upbit WebSocket health: connected=%s uptime=%s "
            "messages_received=%s execution_events_received=%s fills_forwarded=%s "
            "last_message_at=%s last_execution_at=%s last_agent_success_at=%s",
            connected,
            uptime,
            runtime_snapshot["messages_received"],
            runtime_snapshot["execution_events_received"],
            self.fills_forwarded,
            runtime_snapshot["last_message_at"],
            runtime_snapshot["last_execution_at"],
            self.last_agent_success_at,
        )

    async def start(self) -> None:
        """통합 모니터링 시작"""
        logger.info("Starting Unified WebSocket Monitor (mode=%s)...", self.mode)
        self.is_running = True
        self._started_at_monotonic = asyncio.get_running_loop().time()

        # Write initial heartbeat
        self._write_heartbeat()

        task_map: dict[str, asyncio.Task[Any]] = {
            "upbit": asyncio.create_task(self._start_upbit(), name="upbit-websocket")
        }

        failure: RuntimeError | None = None

        try:
            while self.is_running:
                done, _ = await asyncio.wait(
                    set(task_map.values()),
                    timeout=self._heartbeat_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Update heartbeat periodically
                self._write_heartbeat()

                if not done:
                    self._log_health_status()
                    continue

                for name, task in task_map.items():
                    if task not in done:
                        continue

                    if task.cancelled():
                        failure = RuntimeError(
                            f"{name} task was cancelled unexpectedly"
                        )
                        logger.error("%s task cancelled unexpectedly", name)
                    else:
                        exc = task.exception()
                        if exc:
                            failure = RuntimeError(f"{name} task failed: {exc}")
                            logger.error("%s task failed: %s", name, exc, exc_info=exc)
                        else:
                            # Task completed normally - supervisor exited
                            # This means stop was requested, which is fine
                            if self.is_running:
                                failure = RuntimeError(
                                    f"{name} supervisor exited unexpectedly"
                                )
                                logger.error("%s supervisor exited unexpectedly", name)

                    if failure:
                        self.is_running = False
                        break
        except asyncio.CancelledError:
            logger.info("Main loop cancelled")
            raise
        finally:
            for task in task_map.values():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:  # NOSONAR python:S7497 — cleanup loop, cancellation already handled by task.cancel()
                    pass
                except Exception as exc:
                    logger.debug("Child task cleanup ignored error: %s", exc)

            self._log_health_status(force=True)
            self._write_heartbeat(is_running=False)

        if failure is not None:
            raise failure

    async def stop(self) -> None:
        """통합 모니터링 정지"""
        logger.info("Stopping Unified WebSocket Monitor...")
        self.is_running = False

        # Write heartbeat to indicate stopped
        self._write_heartbeat(is_running=False)

        if self.upbit_ws:
            try:
                await self.upbit_ws.disconnect()
            except Exception as e:
                logger.warning(f"Failed to stop Upbit WebSocket cleanly: {e}")

        logger.info("Upbit WebSocket Monitor stopped")

    def _current_runtime_stats_snapshot(self) -> dict[str, int | str | None]:
        return {
            "messages_received": self.upbit_messages_received,
            "execution_events_received": self.upbit_execution_events_received,
            "last_message_at": self.upbit_last_message_at,
            "last_execution_at": self.upbit_last_execution_at,
        }


async def main(mode: str = "upbit") -> None:
    """메인 엔트리포인트"""
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    configure_dependency_log_levels()
    service_name = "auto-trader-upbit-ws"
    init_sentry(service_name=service_name)

    has_telegram = bool(settings.telegram_token and settings.telegram_chat_id)
    has_discord = any(
        [
            settings.discord_webhook_us,
            settings.discord_webhook_kr,
            settings.discord_webhook_crypto,
            settings.discord_webhook_alerts,
        ]
    )

    if has_telegram or has_discord:
        try:
            trade_notifier = get_trade_notifier()
            trade_notifier.configure(
                bot_token=settings.telegram_token or "",
                chat_ids=settings.telegram_chat_ids
                if settings.telegram_chat_ids
                else [],
                enabled=True,
                discord_webhook_us=settings.discord_webhook_us,
                discord_webhook_kr=settings.discord_webhook_kr,
                discord_webhook_crypto=settings.discord_webhook_crypto,
                discord_webhook_alerts=settings.discord_webhook_alerts,
            )
            logger.info(
                "Trade notifier configured: telegram=%s discord=%s",
                has_telegram,
                has_discord,
            )
        except Exception as e:
            logger.warning("Failed to configure trade notifier: %s", e, exc_info=True)
    else:
        logger.info("Trade notifier disabled: no Telegram or Discord configured")

    monitor = UnifiedWebSocketMonitor(mode=mode)

    try:
        await monitor.start()
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down...")
    except Exception as e:
        capture_exception(e, process="websocket_monitor")
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await monitor.stop()
        try:
            trade_notifier = get_trade_notifier()
            await trade_notifier.shutdown()
        except Exception as e:
            logger.warning("Failed to shutdown trade notifier: %s", e, exc_info=True)
        logger.info("Unified WebSocket Monitor exited gracefully")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unified websocket monitor")
    parser.add_argument(
        "--mode",
        choices=sorted(VALID_MONITOR_MODES),
        default="upbit",
        help="Run the Upbit websocket backend",
    )
    args = parser.parse_args()
    asyncio.run(main(mode=args.mode))
