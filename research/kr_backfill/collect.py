"""Stage B collector — the Toss source stream into research history.

Destination is ``research.kr_candles_1m``. Production ``public.kr_candles_1m``
is read only for latency probing and is never written.

Write discipline (all enforced here, not by convention):

* ``ON CONFLICT (time_utc, symbol, venue) DO NOTHING`` — existing rows always
  win. No DELETE, no UPDATE, no table other than ``research.kr_candles_1m``.
* regular-session bars only (09:00-15:30 KST); NXT bars discarded, ``venue='KRX'``.
* the 09:00-20:00 KST freeze halts fetching at a checkpoint; it does not abort.
* each source keeps an independent checkpoint. A failed source never has its
  remaining symbols reassigned — the split table is the only provenance record,
  and silent reassignment would destroy it.
* every page appends to progress.jsonl and flushes immediately.

Abort conditions (stop the stream, report, do not "work around"):
conflict ratio far from expectation · query latency regression vs baseline ·
repeated 429s · any change to a witness table outside kr_candles_1m.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

try:
    from .sources import (  # noqa: E402
        AUTH_STALE_TOKEN,
        EMPTY_RESPONSE,
        KST,
        FetchWindowClosed,
        Pacer,
        assert_fetch_window_open,
        fetch_toss_minutes,
        in_regular_session,
        now_kst,
    )
except ImportError:  # pragma: no cover - direct CLI execution
    from sources import (  # noqa: E402
        AUTH_STALE_TOKEN,
        EMPTY_RESPONSE,
        KST,
        FetchWindowClosed,
        Pacer,
        assert_fetch_window_open,
        fetch_toss_minutes,
        in_regular_session,
        now_kst,
    )

VENUE = "KRX"

#: Destination is research, NOT production. Changed 2026-08-03 by the storage
#: decision in herdr-inbox/answer-codexmock-research-db-1805.md: production
#: public.kr_candles_1m keeps its 90-day retention and is never backfilled.
TARGET_TABLE = "research.kr_candles_1m"

UPSERT_SQL = """
INSERT INTO research.kr_candles_1m
    (symbol, time_utc, session_date_kst, venue, session_segment, source,
     open, high, low, close, volume, value, retrieved_at, batch_id)
SELECT * FROM UNNEST($1::text[], $2::timestamptz[], $3::date[], $4::text[],
                     $5::text[], $6::text[],
                     $7::numeric[], $8::numeric[], $9::numeric[],
                     $10::numeric[], $11::numeric[], $12::numeric[],
                     $13::timestamptz[], $14::text[])
ON CONFLICT (time_utc, symbol, venue) DO NOTHING
"""

#: Latency regression threshold vs the Stage A baseline median (2.127 ms).
LATENCY_ABORT_FACTOR = 5.0
MAX_CONSECUTIVE_429 = 3
DB_LATENCY_SAMPLES_PER_WINDOW = 15
DB_LATENCY_CONSECUTIVE_WINDOWS = 2
DB_LATENCY_RAW_HARD_STOP_MS = 100.0
DB_INSERT_CONFLICT_ABORT_RATIO = 0.005
CURSOR_OVERLAP_ABORT_RATIO = 0.40
EXIT_SUCCESS = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_TOTAL_FAILURE = 2

# DB 권한으로 대체됨(role auto_trader_kr_backfill, 2026-08-04 적용).
# 행수 감시는 프로덕션 동시 쓰기와 구분 불가.


@dataclass
class StreamStats:
    source: str
    symbols_total: int = 0
    symbols_done: int = 0
    calls: int = 0
    rows_fetched: int = 0
    rows_kept: int = 0
    rows_filtered_cursor_overlap: int = 0
    rows_inserted: int = 0
    rows_skipped_conflict: int = 0
    rows_skipped_conflict_preseed: int = 0
    empty_responses: int = 0
    empty_symbols: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped_reason: str | None = None


class AbortStream(RuntimeError):
    pass


class HardRatioAbort(AbortStream):
    pass


def enforce_hard_ratio_guards(stats: StreamStats) -> None:
    """Abort on contaminated inserts or a broken cursor within this collector batch."""

    guarded_conflicts = (
        stats.rows_skipped_conflict - stats.rows_skipped_conflict_preseed
    )
    if guarded_conflicts < 0:
        raise HardRatioAbort("preseed conflicts exceed total DB insert conflicts")
    conflict_denominator = stats.rows_inserted + guarded_conflicts
    if conflict_denominator:
        conflict_ratio = guarded_conflicts / conflict_denominator
        if conflict_ratio > DB_INSERT_CONFLICT_ABORT_RATIO:
            raise HardRatioAbort(
                "DB insert conflict ratio "
                f"{conflict_ratio:.6f} > hard limit "
                f"{DB_INSERT_CONFLICT_ABORT_RATIO:.6f} "
                "(scope=collector_batch, "
                f"skipped_guarded={guarded_conflicts}, "
                f"skipped_preseed={stats.rows_skipped_conflict_preseed}, "
                f"inserted={stats.rows_inserted}, "
                f"denominator={conflict_denominator})"
            )

    overlap_denominator = stats.rows_kept + stats.rows_filtered_cursor_overlap
    if overlap_denominator:
        overlap_ratio = stats.rows_filtered_cursor_overlap / overlap_denominator
        if overlap_ratio > CURSOR_OVERLAP_ABORT_RATIO:
            raise HardRatioAbort(
                "cursor overlap ratio "
                f"{overlap_ratio:.6f} > hard limit "
                f"{CURSOR_OVERLAP_ABORT_RATIO:.6f} "
                "(scope=collector_batch, "
                f"filtered={stats.rows_filtered_cursor_overlap}, "
                f"kept={stats.rows_kept}, denominator={overlap_denominator})"
            )


def classify_resume_preseed_conflicts(
    *,
    first_page: bool,
    checkpoint_was_partial: bool,
    rows_kept: int,
    rows_inserted: int,
    rows_skipped_conflict: int,
    rows_verified_between_checkpoint_and_restart: int,
) -> int:
    """Identify only an atomic insert committed before its checkpoint on shutdown."""

    if (
        first_page
        and checkpoint_was_partial
        and rows_kept > 0
        and rows_inserted == 0
        and rows_skipped_conflict == rows_kept
        and rows_verified_between_checkpoint_and_restart == rows_skipped_conflict
    ):
        return rows_skipped_conflict
    return 0


class ProgressLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", buffering=1)  # line buffered

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", now_kst().isoformat())
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


class Checkpoint:
    """One file per source. Written after every page."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            self.data = json.loads(path.read_text())

    def get(self, symbol: str) -> dict[str, Any]:
        return self.data.setdefault(
            symbol,
            {
                "done": False,
                "oldest_reached": None,
                "rows_inserted": 0,
                "rows_skipped": 0,
            },
        )

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, default=str))
        tmp.replace(self.path)


def dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def insert_bars(
    pool: asyncpg.Pool,
    symbol: str,
    bars: dict[datetime, dict[str, float]],
    *,
    source: str,
    batch_id: str,
) -> tuple[int, int]:
    """Returns (inserted, skipped_conflict). Never updates an existing row.

    Counts are measured by before/after row counts rather than inferred from the
    statement tag, so a silent partial insert cannot be reported as a success.
    """
    if not bars:
        return 0, 0
    items = sorted(bars.items())
    times = [t.replace(tzinfo=KST) for t, _ in items]
    session_dates = [t.date() for t, _ in items]
    # Backfill fetches the regular session only, so the segment is known; the
    # collector filters non-regular bars out before reaching here.
    segments = ["KRX_REGULAR"] * len(items)
    now = datetime.now(KST)
    cols = {
        k: [float(v[k]) for _, v in items]
        for k in ("open", "high", "low", "close", "volume", "value")
    }
    async with pool.acquire() as conn:
        async with conn.transaction():
            count_sql = (
                "SELECT count(*) FROM research.kr_candles_1m "
                "WHERE symbol=$1 AND venue=$2 AND time_utc = ANY($3::timestamptz[])"
            )
            before = await conn.fetchval(count_sql, symbol, VENUE, times)
            await conn.execute(
                UPSERT_SQL,
                [symbol] * len(items),
                times,
                session_dates,
                [VENUE] * len(items),
                segments,
                [source.upper()] * len(items),
                cols["open"],
                cols["high"],
                cols["low"],
                cols["close"],
                cols["volume"],
                cols["value"],
                [now] * len(items),
                [batch_id] * len(items),
            )
            after = await conn.fetchval(count_sql, symbol, VENUE, times)
    inserted = after - before
    return inserted, len(items) - inserted


async def count_existing_bars(
    pool: asyncpg.Pool, symbol: str, timestamps: list[datetime]
) -> int:
    """Read-only proof that cursor-overlap rows are already persisted."""
    if not timestamps:
        return 0
    times = [timestamp.replace(tzinfo=KST) for timestamp in timestamps]
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM research.kr_candles_1m "
                "WHERE symbol=$1 AND venue=$2 AND time_utc = ANY($3::timestamptz[])",
                symbol,
                VENUE,
                times,
            )
        )


async def count_resume_preseed_bars(
    pool: asyncpg.Pool,
    symbol: str,
    timestamps: list[datetime],
    *,
    checkpoint_saved_at: datetime,
    collector_started_at: datetime,
    current_batch_id: str,
) -> int:
    """Prove rows committed in the checkpoint-to-restart crash window."""

    if not timestamps:
        return 0
    times = [timestamp.replace(tzinfo=KST) for timestamp in timestamps]
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM research.kr_candles_1m "
                "WHERE symbol=$1 AND venue=$2 "
                "AND time_utc = ANY($3::timestamptz[]) "
                "AND retrieved_at > $4 AND retrieved_at <= $5 "
                "AND batch_id <> $6",
                symbol,
                VENUE,
                times,
                checkpoint_saved_at,
                collector_started_at,
                current_batch_id,
            )
        )


def filter_page_to_cursor(
    bars: dict[datetime, dict[str, float]], cursor_dt: datetime
) -> tuple[dict[datetime, dict[str, float]], int]:
    """Exclude a provider's repeated tail newer than the exact checkpoint."""
    eligible = {
        timestamp: values
        for timestamp, values in bars.items()
        if timestamp <= cursor_dt
    }
    return eligible, len(bars) - len(eligible)


def filter_insert_domain(
    bars: dict[datetime, dict[str, float]], start_dt: datetime, end_dt: datetime
) -> dict[datetime, dict[str, float]]:
    """Keep only regular-session rows inside the approved collection window."""
    return {
        timestamp: values
        for timestamp, values in bars.items()
        if in_regular_session(timestamp) and start_dt <= timestamp <= end_dt
    }


class Guard:
    """Latency and 429 abort-condition monitor shared by all streams."""

    def __init__(self, pool: asyncpg.Pool, baseline_median_ms: float, log: ProgressLog):
        self.pool = pool
        self.baseline = baseline_median_ms
        self.log = log
        self.consecutive_429: dict[str, int] = {}
        self.latency_p95_breach_streak: dict[str, int] = {}

    @staticmethod
    def _nearest_rank_p95(samples: list[float]) -> float:
        if not samples:
            raise ValueError("latency samples must not be empty")
        rank = max(1, (95 * len(samples) + 99) // 100)
        return sorted(samples)[rank - 1]

    async def check(self, source: str) -> None:
        async with self.pool.acquire() as c:
            samples: list[float] = []
            for _ in range(DB_LATENCY_SAMPLES_PER_WINDOW):
                t0 = time.perf_counter()
                await c.fetch(
                    "SELECT time, close FROM public.kr_candles_1m "
                    "WHERE symbol=$1 AND time >= $2 ORDER BY time DESC LIMIT 500",
                    "005930",
                    datetime.now(KST) - timedelta(days=1),
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000
                samples.append(elapsed_ms)
                if elapsed_ms > DB_LATENCY_RAW_HARD_STOP_MS:
                    self.log.write(
                        {
                            "event": "latency_probe",
                            "source": source,
                            "window_complete": False,
                            "sample_count": len(samples),
                            "p95_ms": round(self._nearest_rank_p95(samples), 3),
                            "raw_max_ms": round(max(samples), 3),
                            "baseline_ms": self.baseline,
                            "breach_streak": self.latency_p95_breach_streak.get(
                                source, 0
                            ),
                            "hard_stop": "raw_gt_100ms",
                        }
                    )
                    raise AbortStream(
                        f"query latency raw {elapsed_ms:.2f}ms > "
                        f"{DB_LATENCY_RAW_HARD_STOP_MS:.1f}ms"
                    )
        med = statistics.median(samples)
        p95 = self._nearest_rank_p95(samples)
        threshold_ms = self.baseline * LATENCY_ABORT_FACTOR
        streak = (
            self.latency_p95_breach_streak.get(source, 0) + 1
            if p95 > threshold_ms
            else 0
        )
        self.latency_p95_breach_streak[source] = streak
        self.log.write(
            {
                "event": "latency_probe",
                "source": source,
                "window_complete": True,
                "sample_count": len(samples),
                "median_ms": round(med, 3),
                "p95_ms": round(p95, 3),
                "raw_max_ms": round(max(samples), 3),
                "baseline_ms": self.baseline,
                "threshold_ms": round(threshold_ms, 3),
                "breach_streak": streak,
            }
        )
        if streak >= DB_LATENCY_CONSECUTIVE_WINDOWS:
            raise AbortStream(
                f"query latency p95 {p95:.2f}ms > {LATENCY_ABORT_FACTOR}x "
                f"baseline {self.baseline}ms for "
                f"{DB_LATENCY_CONSECUTIVE_WINDOWS} consecutive windows"
            )

    def note_429(self, source: str, is_429: bool) -> None:
        if is_429:
            self.consecutive_429[source] = self.consecutive_429.get(source, 0) + 1
            if self.consecutive_429[source] >= MAX_CONSECUTIVE_429:
                raise AbortStream(f"{source}: {MAX_CONSECUTIVE_429} consecutive 429s")
        else:
            self.consecutive_429[source] = 0


async def run_stream(
    source: str,
    symbols: list[str],
    client: Any,
    pool: asyncpg.Pool,
    ckpt: Checkpoint,
    log: ProgressLog,
    guard: Guard,
    start_date: date,
    end_date: date,
    batch_id: str,
) -> StreamStats:
    pipe_id = source
    stats = StreamStats(source=pipe_id, symbols_total=len(symbols))
    pacer = Pacer(source)
    start_dt = datetime.combine(start_date, datetime.min.time())
    collector_started_at = now_kst()
    checkpoint_saved_at = (
        datetime.fromtimestamp(ckpt.path.stat().st_mtime, tz=KST)
        if ckpt.path.exists()
        else None
    )

    for symbol in symbols:
        st = ckpt.get(symbol)
        if st.get("done"):
            stats.symbols_done += 1
            continue

        cursor_dt = (
            datetime.fromisoformat(st["oldest_reached"])
            if st.get("oldest_reached")
            else datetime.combine(end_date, datetime.max.time().replace(microsecond=0))
        )
        toss_cursor: str | None = st.get("toss_cursor")

        try:
            while cursor_dt > start_dt:
                request_context = {
                    "symbol": symbol,
                    "cursor": cursor_dt.isoformat(),
                    "source": source,
                }
                try:
                    assert_fetch_window_open()
                except FetchWindowClosed as exc:
                    stats.stopped_reason = f"market_hours_freeze: {exc}"
                    log.write(
                        {
                            "event": "paused_market_hours",
                            "source": source,
                            "surface": pipe_id,
                            "symbol": symbol,
                            "cursor": cursor_dt,
                            "last_request": request_context,
                            "pacer": pacer.snapshot(),
                        }
                    )
                    ckpt.save()
                    return stats

                try:
                    bars, meta = await fetch_toss_minutes(
                        client=client,
                        symbol=symbol,
                        pacer=pacer,
                        count=200,
                        before=toss_cursor,
                        max_pages=1,
                    )
                    toss_cursor = meta.get("next_before")
                    guard.note_429(pipe_id, False)
                except Exception as exc:  # noqa: BLE001
                    msg = f"{type(exc).__name__}: {exc}"
                    reason_code = str(getattr(exc, "reason_code", type(exc).__name__))
                    stats.calls = pacer.calls
                    is_429 = "429" in msg or "TooManyRequests" in msg
                    stats.errors.append(f"{symbol}: {msg}")
                    log.write(
                        {
                            "event": "fetch_error",
                            "source": source,
                            "surface": pipe_id,
                            "symbol": symbol,
                            "error": msg,
                            "reason_code": reason_code,
                            "retry_disposition": getattr(
                                exc, "retry_disposition", "NONE"
                            ),
                            "last_request": request_context,
                            "pacer": pacer.snapshot(),
                        }
                    )
                    guard.note_429(pipe_id, is_429)
                    if reason_code == AUTH_STALE_TOKEN:
                        stats.stopped_reason = (
                            "AUTH_STALE_TOKEN after one read-only retry"
                        )
                        ckpt.save()
                        return stats
                    break

                stats.calls = pacer.calls
                if not bars:
                    reason_code = str(meta.get("outcome_code") or EMPTY_RESPONSE)
                    stats.empty_responses += 1
                    stats.empty_symbols.append(symbol)
                    log.write(
                        {
                            "event": "empty_response",
                            "source": source,
                            "surface": pipe_id,
                            "symbol": symbol,
                            "reason_code": reason_code,
                            "calls_cumulative": pacer.calls,
                        }
                    )
                    st["done"] = True
                    break

                oldest = min(bars)
                range_candidates = filter_insert_domain(
                    bars,
                    start_dt,
                    datetime.combine(end_date, datetime.max.time()),
                )
                keep, cursor_overlap = filter_page_to_cursor(
                    range_candidates, cursor_dt
                )
                overlap_timestamps = [
                    timestamp for timestamp in range_candidates if timestamp > cursor_dt
                ]
                cursor_overlap_verified = await count_existing_bars(
                    pool, symbol, overlap_timestamps
                )
                if cursor_overlap_verified != cursor_overlap:
                    raise AbortStream(
                        "cursor overlap is not fully persisted: "
                        f"source={source} symbol={symbol} "
                        f"filtered={cursor_overlap} "
                        f"existing={cursor_overlap_verified}"
                    )
                first_page = stats.rows_fetched == 0
                checkpoint_was_partial = (
                    bool(st.get("oldest_reached"))
                    and int(st.get("rows_inserted") or 0) > 0
                )
                stats.rows_fetched += len(bars)
                stats.rows_kept += len(keep)
                stats.rows_filtered_cursor_overlap += cursor_overlap
                ins, skip = await insert_bars(
                    pool, symbol, keep, source=source, batch_id=batch_id
                )
                stats.rows_inserted += ins
                stats.rows_skipped_conflict += skip
                preseed_verified = 0
                if (
                    first_page
                    and checkpoint_was_partial
                    and checkpoint_saved_at is not None
                    and len(keep) > 0
                    and ins == 0
                    and skip == len(keep)
                ):
                    preseed_verified = await count_resume_preseed_bars(
                        pool,
                        symbol,
                        list(keep),
                        checkpoint_saved_at=checkpoint_saved_at,
                        collector_started_at=collector_started_at,
                        current_batch_id=batch_id,
                    )
                preseed_conflicts = classify_resume_preseed_conflicts(
                    first_page=first_page,
                    checkpoint_was_partial=checkpoint_was_partial,
                    rows_kept=len(keep),
                    rows_inserted=ins,
                    rows_skipped_conflict=skip,
                    rows_verified_between_checkpoint_and_restart=preseed_verified,
                )
                stats.rows_skipped_conflict_preseed += preseed_conflicts
                st["rows_inserted"] = st.get("rows_inserted", 0) + ins
                st["rows_skipped"] = st.get("rows_skipped", 0) + skip
                st["rows_skipped_preseed"] = (
                    st.get("rows_skipped_preseed", 0) + preseed_conflicts
                )

                log.write(
                    {
                        "event": "page",
                        "source": source,
                        "surface": pipe_id,
                        "symbol": symbol,
                        "range": [oldest.isoformat(), max(bars).isoformat()],
                        "rows_fetched": len(bars),
                        "rows_kept": len(keep),
                        "rows_filtered_cursor_overlap": cursor_overlap,
                        "rows_filtered_cursor_overlap_verified": (
                            cursor_overlap_verified
                        ),
                        "rows_inserted": ins,
                        "rows_skipped_conflict": skip,
                        "rows_skipped_conflict_preseed": preseed_conflicts,
                        "rows_skipped_conflict_preseed_verified": preseed_verified,
                        "calls_cumulative": pacer.calls,
                    }
                )

                if oldest >= cursor_dt:  # no backward progress; stop this symbol
                    st["done"] = True
                    ckpt.save()
                    enforce_hard_ratio_guards(stats)
                    break
                cursor_dt = oldest - timedelta(minutes=1)
                st["oldest_reached"] = cursor_dt.isoformat()
                st["toss_cursor"] = toss_cursor
                ckpt.save()
                enforce_hard_ratio_guards(stats)

                if source == "toss" and not toss_cursor:
                    st["done"] = True
                    break
            else:
                st["done"] = True
        except AbortStream as exc:
            stats.stopped_reason = str(exc)
            log.write(
                {
                    "event": "abort",
                    "source": source,
                    "surface": pipe_id,
                    "reason": str(exc),
                    "last_request": request_context,
                    "pacer": pacer.snapshot(),
                }
            )
            ckpt.save()
            return stats

        if st.get("done"):
            stats.symbols_done += 1
        ckpt.save()
        await guard.check(pipe_id)

    return stats


def exit_code_for_results(results: list[StreamStats | BaseException]) -> int:
    """Return 0=all useful+clean, 1=partial, 2=no useful successful work."""
    any_useful_work = False
    any_failure = False
    for result in results:
        if isinstance(result, BaseException):
            any_failure = True
            continue
        useful = result.rows_fetched > 0
        any_useful_work = any_useful_work or useful
        clean = (
            useful
            and not result.errors
            and result.stopped_reason is None
            and result.empty_responses == 0
        )
        if not clean:
            any_failure = True

    if not any_useful_work:
        return EXIT_TOTAL_FAILURE
    if any_failure:
        return EXIT_PARTIAL_FAILURE
    return EXIT_SUCCESS


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split-csv", required=True, type=Path)
    ap.add_argument("--job-dir", required=True, type=Path)
    ap.add_argument("--start-date", required=True)
    ap.add_argument("--end-date", required=True)
    ap.add_argument("--sources", default="toss")
    ap.add_argument("--baseline-median-ms", type=float, default=2.127)
    ap.add_argument("--limit-symbols", type=int, default=None)
    ap.add_argument(
        "--confirm-write",
        action="store_true",
        help="required; without it nothing is fetched or written",
    )
    args = ap.parse_args()

    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)

    wanted = [s.strip() for s in args.sources.split(",") if s.strip()]

    assignment: dict[str, list[str]] = {s: [] for s in wanted}
    with args.split_csv.open() as fh:
        for r in csv.DictReader(fh):
            if r["source"] in assignment:
                assignment[r["source"]].append(r["ticker"])
    if args.limit_symbols:
        assignment = {k: v[: args.limit_symbols] for k, v in assignment.items()}

    if not args.confirm_write:
        print(
            json.dumps(
                {
                    "status": "DRY_RUN_NO_WRITE",
                    "target_table": TARGET_TABLE,
                    "window": [args.start_date, args.end_date],
                    "symbols_per_source": {k: len(v) for k, v in assignment.items()},
                },
                indent=2,
            )
        )
        return 0

    assert_fetch_window_open()

    batch_id = f"kr-backfill-p1-toss-{now_kst():%Y%m%dT%H%M%S}"

    unsupported = [src for src in wanted if src != "toss"]
    if unsupported:
        raise ValueError(f"unsupported backfill sources: {unsupported}")

    from app.services.brokers.toss.client import TossReadClient

    clients = {"toss": TossReadClient.from_settings()}
    pool = await asyncpg.create_pool(dsn(), min_size=2, max_size=6)
    log = ProgressLog(args.job_dir / "events" / "progress.jsonl")
    guard = Guard(pool, args.baseline_median_ms, log)

    log.write(
        {
            "event": "stage_b_start",
            "target_table": TARGET_TABLE,
            "batch_id": batch_id,
            "window": [args.start_date, args.end_date],
            "symbols_per_source": {k: len(v) for k, v in assignment.items()},
        }
    )

    try:
        results = await asyncio.gather(
            *[
                run_stream(
                    src,
                    assignment[src],
                    clients[src],
                    pool,
                    Checkpoint(args.job_dir / "events" / f"checkpoint_{src}.json"),
                    log,
                    guard,
                    start_date,
                    end_date,
                    batch_id,
                )
                for src in wanted
            ],
            return_exceptions=True,
        )
    finally:
        await pool.close()
        for c in clients.values():
            close = getattr(c, "aclose", None) or getattr(c, "close", None)
            if close:
                try:
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:  # noqa: BLE001, S110
                    pass

    summary = []
    for r in results:
        if isinstance(r, Exception):
            summary.append({"error": f"{type(r).__name__}: {r}"})
        else:
            summary.append(r.__dict__)
    log.write({"event": "stage_b_end", "summary": summary})
    (args.job_dir / "events" / "stage_b_summary.json").write_text(
        json.dumps(summary, indent=2, default=str, ensure_ascii=False) + "\n"
    )
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    log.close()
    return exit_code_for_results(results)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
