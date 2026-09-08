"""Normalised 1-minute bar fetchers for the Toss backfill source.

Every source returns the same shape so the collector can treat sources
interchangeably:

    {minute_kst_naive: {"open","high","low","close","volume","value"}}

`value` is **not** uniformly meaningful across sources — see VALUE_SEMANTICS.
Callers must not compare it blindly.

The 09:00-20:00 KST fetch freeze is enforced *here*, in code, rather than left
to operator discipline: `assert_fetch_window_open()` is called by every fetcher.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any

KST = timezone(timedelta(hours=9))

#: Regular KRX session. NXT bars are discarded in Phase 1 by instruction.
SESSION_OPEN = dtime(9, 0)
SESSION_CLOSE = dtime(15, 30)

#: Fetching is forbidden while the regular session or NXT is open.
FREEZE_START = dtime(9, 0)
FREEZE_END = dtime(20, 0)

#: What each source's `value` field actually is. Discovered during Stage A prep.
VALUE_SEMANTICS: dict[str, str] = {
    # NOT broker-reported: app/services/brokers/toss/candles.py computes
    # value = close * volume, so this is the repo's own arithmetic.
    "toss": "synthesised_close_times_volume",
}

PACE_SECONDS: dict[str, float] = {"toss": 0.3}
ROWS_RETURNED = "ROWS_RETURNED"
EMPTY_RESPONSE = "EMPTY_RESPONSE"
EMPTY_RESPONSE_PLACEHOLDER = "EMPTY_RESPONSE_PLACEHOLDER"
AUTH_STALE_TOKEN = "AUTH_STALE_TOKEN"
PROVIDER_REJECTED = "PROVIDER_REJECTED"
MALFORMED_RESPONSE = "MALFORMED_RESPONSE"


class FetchWindowClosed(RuntimeError):
    """Raised when a fetch is attempted inside the 09:00-20:00 KST freeze."""


class BackfillSourceResponseError(RuntimeError):
    """Value-redacted source failure with an operator-facing reason code."""

    def __init__(
        self,
        *,
        reason_code: str,
        source: str,
        retry_disposition: str,
        provider_code: int | str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.source = source
        self.retry_disposition = retry_disposition
        self.provider_code = provider_code
        super().__init__(
            f"{source} response failure reason_code={reason_code} "
            f"provider_code={provider_code!r} retry={retry_disposition}"
        )


def now_kst() -> datetime:
    return datetime.now(KST)


def assert_fetch_window_open(*, override_now: datetime | None = None) -> None:
    n = override_now or now_kst()
    if FREEZE_START <= n.time() < FREEZE_END:
        # 2026-08-04 운영자 승인: 주간 백필 가동. dbrole 적용으로
        # public.* write가 차단된다는 전제에서 exact "true"만 허용한다.
        if os.getenv("BACKFILL_DAYTIME_APPROVED") == "true":
            return
        raise FetchWindowClosed(
            f"fetch frozen 09:00-20:00 KST (regular session + NXT); now {n:%H:%M:%S} KST"
        )


class Pacer:
    """Serial per-source pacer. One instance per source stream."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.interval = PACE_SECONDS[source]
        self.calls = 0
        self._last = 0.0

    async def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.interval:
            await asyncio.sleep(self.interval - delta)
        self._last = time.monotonic()
        self.calls += 1

    def snapshot(self) -> dict[str, float | int | str]:
        """Value-redacted state recorded when a pipe stops."""
        return {
            "source": self.source,
            "calls": self.calls,
            "interval_seconds": self.interval,
            "seconds_since_last_request": max(0.0, time.monotonic() - self._last),
        }


def in_regular_session(ts: datetime) -> bool:
    """Regular-session bars only; 15:30 closing bar included, NXT discarded."""
    return SESSION_OPEN <= ts.time() <= SESSION_CLOSE


# --------------------------------------------------------------------------
# Toss (live read host) — /api/v1/candles interval=1m
# --------------------------------------------------------------------------


async def fetch_toss_minutes(
    *,
    client: Any,
    symbol: str,
    pacer: Pacer,
    count: int = 400,
    before: str | None = None,
    max_pages: int = 3,
) -> tuple[dict[datetime, dict[str, float]], dict[str, Any]]:
    assert_fetch_window_open()
    out: dict[datetime, dict[str, float]] = {}
    meta: dict[str, Any] = {
        "pages": 0,
        "rows_raw": 0,
        "next_before": None,
        "outcome_code": None,
    }

    cursor = before
    for _ in range(max_pages):
        await pacer.wait()
        page = await client.candles(
            symbol, interval="1m", count=min(count, 200), before=cursor
        )
        meta["pages"] += 1
        meta["rows_raw"] += len(page.candles)
        if not page.candles:
            meta["outcome_code"] = EMPTY_RESPONSE
            break
        meta["outcome_code"] = ROWS_RETURNED
        for c in page.candles:
            ts = datetime.fromisoformat(str(c.timestamp).replace("Z", "+00:00"))
            ts = ts.astimezone(KST).replace(tzinfo=None) if ts.tzinfo else ts
            close = float(c.close_price)
            volume = float(c.volume)
            out[ts.replace(second=0, microsecond=0)] = {
                "open": float(c.open_price),
                "high": float(c.high_price),
                "low": float(c.low_price),
                "close": close,
                # synthesised, not broker-reported — see VALUE_SEMANTICS
                "value": close * volume,
                "volume": volume,
            }
        cursor = page.next_before
        meta["next_before"] = cursor
        if not cursor or len(out) >= count:
            break

    return out, meta
