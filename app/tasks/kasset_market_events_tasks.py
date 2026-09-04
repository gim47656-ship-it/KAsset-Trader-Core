"""TaskIQ schedule for event-driven KAsset market analysis."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.extensions.kasset.automation.vertical_slice import (
    run_ai_recommendation_cycle_once,
)
from app.extensions.kasset.models import AndroidPaperAccount
from app.jobs.google_news_rss_ingestion import run_google_news_rss_ingestion
from app.models.ai_recommendations import AIRecommendation
from app.models.paper_trading import PaperAccount, PaperPosition
from app.models.symbol_master import SymbolMaster
from app.models.trading import (
    Instrument,
    InstrumentType,
    User,
    UserRole,
    UserWatchItem,
)

NEWS_TARGET_LIMIT = 50
NEWS_RECOMMENDATION_LOOKBACK = timedelta(days=7)
_CYCLE_LOCK_NAMESPACE = 1_263_498_067
_CYCLE_LOCK_KEY = 1
# Same namespace, distinct key: a push run and an analysis cycle are unrelated
# and must not block each other.
_PUSH_LOCK_KEY = 2
# Equity snapshots never gate an order, so they must not queue behind the
# analysis cycle or the push sweep.
_SNAPSHOT_LOCK_KEY = 3
_KST = ZoneInfo("Asia/Seoul")

logger = logging.getLogger(__name__)


@broker.task(
    task_name="kasset_market_events.run",
    # 현지 정규장 안에서 10분마다 후보를 점검한다. 09시대 장전 tick,
    # 휴장·반일장·장전/시간외의 최종 차단은 exchange calendar runtime gate가 맡는다.
    schedule=[
        {"cron": "*/10 9-15 * * 1-5", "cron_offset": "Asia/Seoul"},
        {"cron": "*/10 9-15 * * 1-5", "cron_offset": "America/New_York"},
    ],
)
async def kasset_market_events_run() -> dict[str, object]:
    """Run the canonical screener→ensemble→AI recommendation producer once."""

    async with _cycle_single_flight() as acquired:
        if not acquired:
            logger.info("kasset AI recommendation cycle skipped: cycle_already_running")
            return {
                "enabled": True,
                "owners": [],
                "candidateCount": 0,
                "recommendationCount": 0,
                "skipped": "cycle_already_running",
            }
        return await run_ai_recommendation_cycle_once()


@broker.task(
    task_name="kasset_watchlist_candles.sync",
    schedule=[
        {"cron": "5 9-16 * * 1-5", "cron_offset": "Asia/Seoul"},
        # 15:31 이후 전체 1분봉 로테이션과 20:00 수집 종료 뒤 당일을 확정한다.
        {"cron": "5 20 * * 1-5", "cron_offset": "Asia/Seoul"},
    ],
)
async def kasset_watchlist_candles_sync() -> dict[str, object]:
    """Toss 일봉 동기화 뒤 KR 관심종목의 최근 완료 정규장 일봉을 보정한다.

    이벤트 스캔은 ``kr_candles_1d``를 읽지만 이 배포에는 KIS 보유종목 기반
    적재가 없으므로 관심종목 목록 자체를 일봉 대상 유니버스로 사용한다.
    """
    if not settings.KASSET_MARKET_EVENTS_ENABLED:
        return {"enabled": False, "synced": []}

    symbols = await _watchlist_kr_symbols()
    synced: list[dict[str, object]] = []
    for symbol in symbols:
        try:
            synced.append(await _sync_watchlist_symbol(symbol))
        except Exception as exc:  # noqa: BLE001 - one symbol must not stop the rest
            synced.append({"symbol": symbol, "error": str(exc)})
    regular_override = await _override_recent_completed_kr_regular(symbols)
    return {
        "enabled": True,
        "synced": synced,
        "regularOverride": regular_override,
    }


@broker.task(
    task_name="kasset.news.google.kr.sync",
    schedule=[{"cron": "20 */3 * * *", "cron_offset": "Asia/Seoul"}],
)
async def kasset_google_news_kr_sync() -> dict[str, object]:
    """Refresh Google News for the bounded KR PAPER portfolio universe."""

    return await _sync_google_news_market("kr")


@broker.task(
    task_name="kasset.news.google.us.sync",
    schedule=[{"cron": "50 */3 * * *", "cron_offset": "Asia/Seoul"}],
)
async def kasset_google_news_us_sync() -> dict[str, object]:
    """Refresh Google News for the bounded US PAPER portfolio universe."""

    return await _sync_google_news_market("us")


@broker.task(
    task_name="kasset.push.price_alerts",
    # 기존 10분 스위프가 그날의 미발송 ±5% 알림과 기한이 된 체결 알림
    # 재시도를 함께 처리한다. 설정이 없으면 외부 요청 없이 끝난다.
    schedule=[{"cron": "*/10 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def kasset_price_alert_push() -> dict[str, object]:
    """가격 알림과 기한이 된 체결 알림 재시도를 등록 기기에 전달한다."""

    if not settings.KASSET_FCM_ENABLED:
        return {"enabled": False, "reason": "disabled"}

    from app.extensions.kasset.fcm_push_service import dispatch_scheduled_pushes

    async with _advisory_single_flight(_PUSH_LOCK_KEY) as acquired:
        if not acquired:
            logger.info("kasset FCM push skipped: push_already_running")
            return {"enabled": True, "skipped": "push_already_running"}
        async with _session() as session:
            return await dispatch_scheduled_pushes(session)


@broker.task(
    task_name="kasset.paper.daily_snapshot",
    # KR 정규장 마감(15:30 KST) 직후 하루치 계좌 자산을 확정한다. 같은 KST 날짜에
    # 다시 돌면 그날 행을 갱신할 뿐 새 행을 만들지 않으므로 재실행이 안전하다.
    schedule=[{"cron": "45 15 * * 1-5", "cron_offset": "Asia/Seoul"}],
)
async def kasset_paper_daily_snapshot() -> dict[str, object]:
    """활성 PAPER 계좌의 통화별 자산과 전일 대비 수익률을 하루 한 번 남긴다.

    15:45 KST 시점의 USD 자산은 직전 미국 정규 세션 종가를 기준으로 평가한다.
    """

    if not settings.KASSET_MARKET_EVENTS_ENABLED:
        return {"enabled": False, "accounts": []}

    async with _advisory_single_flight(_SNAPSHOT_LOCK_KEY) as acquired:
        if not acquired:
            logger.info("kasset paper snapshot skipped: snapshot_already_running")
            return {"enabled": True, "skipped": "snapshot_already_running"}
        return await _record_paper_daily_snapshots()


async def _record_paper_daily_snapshots() -> dict[str, object]:
    from app.services.paper_trading_service import PaperTradingService

    async with _session() as session:
        account_ids = [
            int(row)
            for row in (
                await session.execute(
                    select(PaperAccount.id)
                    .where(PaperAccount.is_active.is_(True))
                    .order_by(PaperAccount.id)
                )
            )
            .scalars()
            .all()
        ]
        service = PaperTradingService(session)
        accounts: list[dict[str, object]] = []
        for account_id in account_ids:
            try:
                snapshot = await service.record_daily_snapshot(account_id)
            except Exception as exc:  # noqa: BLE001 - 한 계좌 실패가 나머지를 막지 않는다
                await session.rollback()
                logger.exception(
                    "kasset paper snapshot failed account_id=%s: %s", account_id, exc
                )
                accounts.append(
                    {
                        "accountId": account_id,
                        "status": "failed",
                        "error": type(exc).__name__,
                    }
                )
                continue
            accounts.append(
                {
                    "accountId": account_id,
                    "status": "recorded",
                    "snapshotDate": snapshot.snapshot_date.isoformat(),
                    "equityKrw": _decimal_text(snapshot.equity_krw),
                    "equityUsd": _decimal_text(snapshot.equity_usd),
                    "dailyReturnKrwPct": _decimal_text(snapshot.daily_return_krw_pct),
                    "dailyReturnUsdPct": _decimal_text(snapshot.daily_return_usd_pct),
                    "valuationCompleteKrw": bool(snapshot.valuation_complete_krw),
                    "valuationCompleteUsd": bool(snapshot.valuation_complete_usd),
                }
            )
    logger.info("kasset paper daily snapshot done: accounts=%s", accounts)
    return {"enabled": True, "accounts": accounts}


def _decimal_text(value: object) -> str | None:
    return None if value is None else format(Decimal(str(value)), "f")


async def _sync_google_news_market(market: str) -> dict[str, object]:
    if not settings.KASSET_GOOGLE_NEWS_SCHEDULE_ENABLED:
        return {
            "enabled": False,
            "market": market,
            "symbol_count": 0,
        }
    symbols = await _news_target_symbols(market)
    if not symbols:
        return {
            "status": "skipped",
            "market": market,
            "symbol_count": 0,
            "reason": "no_active_targets",
        }
    return await run_google_news_rss_ingestion(
        market=market,
        stock_symbols=symbols,
    )


async def _news_target_symbols(market: str) -> list[str]:
    """Prioritize held, watched, then recently recommended active symbols."""

    if market not in {"kr", "us"}:
        raise ValueError(f"unsupported news market: {market}")
    source_market = "KRX" if market == "kr" else "US"
    instrument_type = (
        InstrumentType.equity_kr if market == "kr" else InstrumentType.equity_us
    )
    cutoff = datetime.now(UTC) - NEWS_RECOMMENDATION_LOOKBACK

    async with _session() as session:
        held = (
            (
                await session.execute(
                    select(SymbolMaster.symbol)
                    .join(
                        PaperPosition,
                        and_(
                            PaperPosition.symbol == SymbolMaster.symbol,
                            PaperPosition.instrument_type == instrument_type,
                        ),
                    )
                    .join(
                        AndroidPaperAccount,
                        AndroidPaperAccount.paper_account_id
                        == PaperPosition.account_id,
                    )
                    .join(User, User.id == AndroidPaperAccount.owner_user_id)
                    .where(
                        SymbolMaster.market == source_market,
                        SymbolMaster.is_active.is_(True),
                        PaperPosition.quantity > 0,
                        User.role == UserRole.trader,
                        User.is_active.is_(True),
                    )
                    .distinct()
                    .order_by(SymbolMaster.symbol)
                    .limit(NEWS_TARGET_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        watched = (
            (
                await session.execute(
                    select(SymbolMaster.symbol)
                    .join(
                        Instrument,
                        and_(
                            Instrument.symbol == SymbolMaster.symbol,
                            Instrument.type == instrument_type,
                        ),
                    )
                    .join(
                        UserWatchItem,
                        UserWatchItem.instrument_id == Instrument.id,
                    )
                    .join(User, User.id == UserWatchItem.user_id)
                    .where(
                        SymbolMaster.market == source_market,
                        SymbolMaster.is_active.is_(True),
                        Instrument.is_active.is_(True),
                        UserWatchItem.is_active.is_(True),
                        User.role == UserRole.trader,
                        User.is_active.is_(True),
                    )
                    .distinct()
                    .order_by(SymbolMaster.symbol)
                    .limit(NEWS_TARGET_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        recommended = (
            (
                await session.execute(
                    select(SymbolMaster.symbol)
                    .join(
                        AIRecommendation,
                        and_(
                            AIRecommendation.symbol == SymbolMaster.symbol,
                            AIRecommendation.market == source_market,
                        ),
                    )
                    .join(User, User.id == AIRecommendation.owner_user_id)
                    .where(
                        SymbolMaster.market == source_market,
                        SymbolMaster.is_active.is_(True),
                        AIRecommendation.created_at >= cutoff,
                        User.role == UserRole.trader,
                        User.is_active.is_(True),
                    )
                    .order_by(AIRecommendation.created_at.desc())
                    .limit(NEWS_TARGET_LIMIT)
                )
            )
            .scalars()
            .all()
        )

    prioritized = dict.fromkeys([*held, *watched, *recommended])
    return [str(symbol) for symbol in prioritized][:NEWS_TARGET_LIMIT]


async def _watchlist_kr_symbols() -> list[str]:
    async with _session() as session:
        rows = await session.execute(
            select(Instrument.symbol)
            .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
            .join(User, User.id == UserWatchItem.user_id)
            .where(
                UserWatchItem.is_active.is_(True),
                Instrument.is_active.is_(True),
                Instrument.type == InstrumentType.equity_kr,
                User.role == UserRole.trader,
                User.is_active.is_(True),
            )
            .distinct()
            .order_by(Instrument.symbol)
        )
        return [str(symbol) for symbol in rows.scalars().all()]


async def _sync_watchlist_symbol(symbol: str) -> dict[str, object]:
    from app.services.daily_candles.converters import frame_to_rows
    from app.services.daily_candles.repository import (
        DailyCandlesRepository,
        MarketKey,
    )
    from app.services.market_data.toss_ohlcv import fetch_daily_toss_frame

    frame = await fetch_daily_toss_frame(symbol=symbol, count=60)
    rows = frame_to_rows(frame, symbol=symbol, partition="KRX", source="toss")
    async with _session() as session:
        repository = DailyCandlesRepository(session=session)
        upserted = await repository.upsert_rows(market=MarketKey.KR, rows=rows)
        await session.commit()
    return {"symbol": symbol, "rows": len(rows), "upserted": upserted}


async def _override_recent_completed_kr_regular(
    symbols: list[str],
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """최근 완료 거래일을 정규장 집계로 보정하고 실패 시 기존 일봉을 보존한다."""

    from app.services.daily_candles.kr_regular_daily import (
        latest_completed_kr_session_date,
    )
    from app.services.daily_candles.sync_service import _build_default_service

    target_date = latest_completed_kr_session_date(now or datetime.now(_KST))
    if target_date is None:
        logger.warning("KR 정규장 일봉 override 건너뜀: 완료 거래일 확인 실패")
        return {
            "status": "skipped",
            "reason": "completed_session_unknown",
            "rows_upserted": 0,
        }
    if not symbols:
        return {
            "status": "noop",
            "session_date": target_date.isoformat(),
            "targets_total": 0,
            "rows_upserted": 0,
        }

    service = None
    try:
        service = await _build_default_service()
        result = await service.override_kr_regular_daily(
            symbols=symbols,
            session_date=target_date,
        )
        summary = result.as_dict()
        logger.info("KR 정규장 일봉 override 완료: %s", summary)
        return summary
    except Exception as exc:  # noqa: BLE001 - 보정 실패는 기존 일봉을 보존해야 한다
        logger.exception(
            "KR 정규장 일봉 override 실패 date=%s: %s",
            target_date,
            exc,
        )
        return {
            "status": "failed",
            "session_date": target_date.isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
            "rows_upserted": 0,
        }
    finally:
        if service is not None:
            # 보정 cleanup이 기존 일봉 sync를 실패시키면 안 된다.
            try:
                await service.close()
            except Exception:  # noqa: BLE001
                logger.exception("KR 정규장 일봉 override service close 실패")


@asynccontextmanager
async def _advisory_single_flight(key: int) -> AsyncIterator[bool]:
    """Serialize one scheduled job across every TaskIQ worker process."""

    async with _session() as session:
        acquired = bool(
            await session.scalar(
                text("SELECT pg_try_advisory_lock(:namespace, :key)"),
                {"namespace": _CYCLE_LOCK_NAMESPACE, "key": key},
            )
        )
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await session.scalar(
                        text("SELECT pg_advisory_unlock(:namespace, :key)"),
                        {"namespace": _CYCLE_LOCK_NAMESPACE, "key": key},
                    )
                except Exception:
                    # Connection close also releases session advisory locks. Log the
                    # failed explicit cleanup without masking the completed cycle.
                    logger.exception("kasset advisory unlock failed: key=%s", key)


def _cycle_single_flight() -> AbstractAsyncContextManager[bool]:
    return _advisory_single_flight(_CYCLE_LOCK_KEY)


def _session() -> AbstractAsyncContextManager[AsyncSession]:
    return cast(
        AbstractAsyncContextManager[AsyncSession],
        cast(object, AsyncSessionLocal()),
    )


__all__ = [
    "kasset_google_news_kr_sync",
    "kasset_google_news_us_sync",
    "kasset_market_events_run",
    "kasset_paper_daily_snapshot",
    "kasset_price_alert_push",
    "kasset_watchlist_candles_sync",
]
