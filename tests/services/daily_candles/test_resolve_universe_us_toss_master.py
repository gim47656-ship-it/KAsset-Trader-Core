from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.services.daily_candles.repository import DailyCandlesRepository
from app.services.daily_candles.sync_service import DailyCandleSyncService

_KNOWN = "ZZTOSSKNOWN"
_UNKNOWN = "ZZTOSSUNKN"


@pytest.mark.asyncio
async def test_us_universe_targets_skip_symbols_missing_from_toss_master(db_session):
    """운영 재현: Toss 마스터에 없는 active 심볼 2,254개가 매일 ``stock-not-found``를 냈다.

    같은 세션의 미커밋 행만 사용하고 끝에 rollback하므로 다른 워커의 유니버스 행과
    간섭하지 않는다. 특정 두 심볼의 포함/제외만 단언하고 전체 개수는 보지 않는다.
    """

    await db_session.execute(
        text(
            "INSERT INTO public.us_symbol_universe"
            " (symbol, exchange, is_active, toss_master_updated_at)"
            " VALUES (:known, 'NASD', TRUE, now()), (:unknown, 'NASD', TRUE, NULL)"
        ),
        {"known": _KNOWN, "unknown": _UNKNOWN},
    )
    unused = AsyncMock()
    service = DailyCandleSyncService(
        repository=DailyCandlesRepository(session=db_session),
        toss_kr_fetcher=unused,
        toss_us_fetcher=unused,
        yahoo_us_fetcher=unused,
        upbit_crypto_fetcher=unused,
    )
    try:
        targets = await service._resolve_universe(market="us")
    finally:
        await db_session.rollback()

    symbols = {target.symbol for target in targets}
    assert _KNOWN in symbols
    assert _UNKNOWN not in symbols
