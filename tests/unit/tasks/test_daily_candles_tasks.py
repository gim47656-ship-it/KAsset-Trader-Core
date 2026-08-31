"""Tests for daily candle TaskIQ cron task registration.

Schedule access: task.labels['schedule'] is the correct attribute for this project's
TaskIQ version (AsyncTaskiqDecoratedTask). Verified via:
  uv run python -c "from app.tasks import us_candles_tasks; t = ...; print(t.labels)"
  => {'schedule': [{'cron': '*/10 * * * *', 'cron_offset': 'Asia/Seoul'}]}
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.daily_candles.repository import MarketKey


def test_cron_schedules_are_registered():
    from app.tasks import daily_candles_tasks

    assert hasattr(daily_candles_tasks, "sync_kr_daily_task")
    assert hasattr(daily_candles_tasks, "sync_us_daily_task")
    assert hasattr(daily_candles_tasks, "sync_crypto_daily_task")


def test_cron_schedules_use_asia_seoul_timezone():
    """Verify all three tasks use Asia/Seoul cron_offset, matching project convention."""
    from app.tasks import daily_candles_tasks

    for attr_name in (
        "sync_kr_daily_task",
        "sync_us_daily_task",
        "sync_crypto_daily_task",
    ):
        task = getattr(daily_candles_tasks, attr_name)
        # task.labels is the correct attribute on AsyncTaskiqDecoratedTask
        schedule = task.labels.get("schedule") if hasattr(task, "labels") else None
        assert schedule is not None, f"{attr_name} missing schedule"
        assert any(entry.get("cron_offset") == "Asia/Seoul" for entry in schedule), (
            f"{attr_name} missing Asia/Seoul cron_offset"
        )


def test_cron_expressions_match_spec():
    """Guard against typos in cron strings.

    A typo like '30 16 * * 1-6' (accidentally including Saturday for KR)
    would not be caught by the timezone check alone.
    """
    from app.tasks import daily_candles_tasks

    expected = {
        "sync_kr_daily_task": "30 16 * * 1-5",
        "sync_us_daily_task": "0 7 * * 2-6",
        "sync_crypto_daily_task": "0 9 * * *",
    }
    for attr_name, expected_cron in expected.items():
        task = getattr(daily_candles_tasks, attr_name)
        schedule = task.labels.get("schedule")
        assert schedule, f"{attr_name} missing schedule"
        assert schedule[0]["cron"] == expected_cron, (
            f"{attr_name} cron mismatch: {schedule[0]['cron']!r} != {expected_cron!r}"
        )


@pytest.mark.asyncio
async def test_kr_daily_sync_persists_both_broad_market_benchmarks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import daily_candles

    benchmark_result = SimpleNamespace(
        rows_upserted=61,
        fallback_used=False,
        skipped_reason=None,
    )
    service = SimpleNamespace(
        sync_market_universe=AsyncMock(
            return_value={
                "market": "kr",
                "targets_total": 1,
                "rows_upserted": 252,
                "fallback_count": 0,
                "skipped": 0,
            }
        ),
        sync_benchmark=AsyncMock(side_effect=(benchmark_result, benchmark_result)),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        daily_candles,
        "_build_default_service",
        AsyncMock(return_value=service),
    )

    result = await daily_candles.run_daily_candles_sync("kr")

    assert result["status"] == "ok"
    assert result["benchmark_targets_total"] == 2
    assert result["benchmark_rows_upserted"] == 122
    assert [call.kwargs for call in service.sync_benchmark.await_args_list] == [
        {
            "market": MarketKey.KR,
            "horizon_bars": 400,
            "symbol": "KOSPI",
        },
        {
            "market": MarketKey.KR,
            "horizon_bars": 400,
            "symbol": "KOSDAQ",
        },
    ]
    service.close.assert_awaited_once()
