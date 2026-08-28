from __future__ import annotations

import logging

from app.services.nhplug_symbol_master_service import sync_nhplug_symbol_master

logger = logging.getLogger(__name__)


async def run_nhplug_symbol_master_sync(
    *,
    dry_run: bool = False,
) -> dict[str, int | str | bool]:
    try:
        result = await sync_nhplug_symbol_master(dry_run=dry_run)
        payload: dict[str, int | str | bool] = {
            "status": "completed",
            **result,
        }
        if dry_run:
            payload["dry_run"] = True
        return payload
    except Exception as exc:
        logger.error("NH PLUG symbol master sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
            **({"dry_run": True} if dry_run else {}),
        }
