"""Broker 공용 pre-send live-mutation 중단 신호.

실제 mutation HTTP 직전에 freshness, 유효성, market-session 정책을 다시
검사하는 callback과 fail-closed 오류를 provider 의존 없이 공유한다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

# A callback invoked immediately before each real broker mutation.
PreSendHook = Callable[[], Awaitable[None]]


class PreSendFreshnessError(RuntimeError):
    """The live mutation is no longer allowed at its HTTP send boundary."""

    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        self.reason_codes = tuple(reason_codes)
        super().__init__(",".join(self.reason_codes) or "pre_send_freshness")
