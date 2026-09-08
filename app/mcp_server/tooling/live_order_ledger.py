"""ROB-407 — ``review.live_order_ledger`` 과거 행 감사 읽기.

이 원장에 기록하던 주문 경로는 KIS 해외(us)와 Upbit(crypto) 둘뿐이었고 두 공급자
모두 제거됐다. 따라서 accepted-only 기록(``_record_live_order``)과 체결 증거 기반
reconcile(``live_reconcile_orders_impl``)은 되물을 브로커가 없어 함께 삭제했다.
이미 저장된 행은 그대로 두고 보고서 감사 읽기만 남긴다. Toss live 주문은
``toss_live_order_ledger_service``가 별도로 담당하므로 영향이 없다.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.review import LiveOrderLedger

logger = logging.getLogger(__name__)


async def list_live_orders_by_report_item_uuid(
    report_item_uuid: uuid.UUID,
) -> list[dict[str, Any]]:
    """ROB-473 — live US/crypto orders linked to a report item (audit).

    ROB-554 — projects via the shared LinkedOrderView so this audit helper,
    the web bundle, and the MCP bundle share one field mapping.
    """
    from app.services.investment_reports.linked_orders import project_live_order

    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(LiveOrderLedger)
                    .where(LiveOrderLedger.report_item_uuid == report_item_uuid)
                    .order_by(LiveOrderLedger.id.desc())
                )
            )
            .scalars()
            .all()
        )
    return [project_live_order(r).model_dump(mode="json") for r in rows]


__all__ = ["list_live_orders_by_report_item_uuid"]
