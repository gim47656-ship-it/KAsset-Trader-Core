"""TaskIQ cron entries for the durable daily candle store.

Schedules (Asia/Seoul):
- KR: 16:30 KST Mon-Fri (1h after KOSPI close).
- US: 07:00 KST Tue-Sat (~1h after NYSE close on the corresponding US trading day).
- Crypto: manual invocation only; no recurring schedule.

Cron times are offset from intraday sync (which runs every 10 minutes)
to keep KIS rate-limit keys uncontended.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.daily_candles import run_daily_candles_sync

logger = logging.getLogger(__name__)


@broker.task(
    task_name="candles.daily.kr.sync",
    schedule=[{"cron": "30 16 * * 1-5", "cron_offset": "Asia/Seoul"}],
)
async def sync_kr_daily_task() -> dict[str, object]:
    result = await run_daily_candles_sync(market="kr")
    # 주문 없는 스윙 SHADOW 관측은 기본 off이며 KR 일봉 동기화가 성공한 날만 이어 돈다.
    # observer 실패는 일봉 동기화 결과를 바꾸지 않고 swing_shadow 항목과 run 행에 남는다.
    if result.get("status") == "ok" and settings.KASSET_SWING_SHADOW_ENABLED:
        from app.extensions.kasset.automation.swing_shadow_service import (
            run_swing_shadow_after_daily_sync,
        )

        result["swing_shadow"] = await run_swing_shadow_after_daily_sync()
    return result


@broker.task(
    task_name="candles.daily.us.sync",
    schedule=[{"cron": "0 7 * * 2-6", "cron_offset": "Asia/Seoul"}],
)
async def sync_us_daily_task() -> dict[str, object]:
    return await run_daily_candles_sync(market="us")


@broker.task(
    task_name="candles.daily.crypto.sync",
)
async def sync_crypto_daily_task() -> dict[str, object]:
    return await run_daily_candles_sync(market="crypto")
