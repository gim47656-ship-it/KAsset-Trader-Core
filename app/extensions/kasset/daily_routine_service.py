"""Owner-scoped persistence and read-only evidence for the daily AI routine."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import exists, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.daily_routine_schemas import (
    ROUTINE_KEYS,
    AvailableRoutine,
    DailyRoutineAlert,
    DailyRoutineResponse,
    RecommendationMarketScope,
    RoutineKey,
)
from app.extensions.kasset.api.paper_schemas import Quote
from app.extensions.kasset.api.schemas import WatchlistMarket
from app.extensions.kasset.models import (
    KAssetDailyRoutineSetting,
    KAssetRoutinePriceAlertEvent,
)
from app.models.news import NewsAnalysisResult, NewsArticle
from app.models.trading import Instrument, InstrumentType, UserWatchItem
from app.services.daily_candles.repository import (
    DailyCandlesRepository,
    MarketKey,
)
from app.services.news_summary_service import (
    complete_korean_analysis_conditions,
    complete_korean_analysis_exists,
)

logger = logging.getLogger(__name__)
_RAPID_CHANGE_THRESHOLD = Decimal("5")
_NEWS_WINDOW = timedelta(hours=24)
_NEWS_ALERT_LIMIT_PER_KIND = 10
_NEWS_SCAN_LIMIT = 200

_TOPIC_TERMS: tuple[str, ...] = (
    "trump",
    "트럼프",
    "white house",
    "백악관",
    "tariff",
    "관세",
    "policy",
    "정책",
    "regulation",
    "규제",
    "sanction",
    "제재",
    "interest rate",
    "금리",
    "federal reserve",
    "연준",
    "central bank",
    "중앙은행",
    "fiscal",
    "재정",
    "monetary",
    "통화정책",
    "trade war",
    "무역전쟁",
    "executive order",
    "행정명령",
    "treasury",
    "재무부",
    "corporate tax",
    "법인세",
    "congress",
    "의회",
)
_SOURCE_ALIASES: frozenset[str] = frozenset(
    {
        "reuters",
        "reuters business",
        "yahoo finance",
        "marketwatch",
        "cnbc",
        "bloomberg",
        "bloomberg news",
        "financial times",
        "ft",
        "the wall street journal",
        "wall street journal",
        "wsj",
        "barrons",
        "barron s",
        "fortune",
        "the economist",
        "economist",
        "investingcom",
        "investing.com",
        "business insider",
        "nikkei asia",
        "morningstar",
    }
)
_SOURCE_SQL_TERMS: tuple[str, ...] = tuple(
    value
    for value in _SOURCE_ALIASES
    if len(value) >= 4 and value not in {"investingcom"}
)
_SOURCE_SQL_EXACT: tuple[str, ...] = ("ft", "wsj")
_SPACE_RE = re.compile(r"\s+")
_SOURCE_PUNCT_RE = re.compile(r"[^a-z0-9.]+")

_AVAILABLE_ROUTINES: tuple[AvailableRoutine, ...] = (
    AvailableRoutine(
        key="RAPID_RISE",
        label="관심종목 급등",
        description="관심종목의 일간 등락률이 +5% 이상이면 알립니다.",
    ),
    AvailableRoutine(
        key="RAPID_FALL",
        label="관심종목 급락",
        description="관심종목의 일간 등락률이 -5% 이하이면 알립니다.",
    ),
    AvailableRoutine(
        key="TRUMP_POLICY",
        label="Trump·정책 뉴스",
        description="최근 24시간의 Trump 및 주요 정책 뉴스를 감시합니다.",
    ),
    AvailableRoutine(
        key="GLOBAL_FINANCIAL_NEWS",
        label="해외 금융매체 뉴스",
        description="최근 24시간의 주요 해외 금융매체 뉴스를 감시합니다.",
    ),
)

QuoteBatchLoader = Callable[[AsyncSession, str, Sequence[str]], Awaitable[list[Quote]]]


@dataclass(frozen=True, slots=True)
class _EffectiveSelection:
    routine_date: date
    inherited_from: date | None
    enabled_routines: tuple[RoutineKey, ...]
    recommendation_market_scope: RecommendationMarketScope
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class _WatchSymbol:
    symbol: str
    name: str
    market: WatchlistMarket


@dataclass(frozen=True, slots=True)
class _ValidatedNewsAnalysis:
    summary: str
    translated_title: str | None
    translated_excerpt: str | None


@dataclass(frozen=True, slots=True)
class _PriceObservation:
    """관심종목 하나의 현재 일간 등락률 관측."""

    market: WatchlistMarket
    symbol: str
    name: str
    rate: Decimal
    occurred_at: datetime
    source: str
    completed: bool
    session_state: str | None


async def _default_quote_loader(
    db: AsyncSession, market: str, symbols: Sequence[str]
) -> list[Quote]:
    return await krx_quotes.resolve_quotes(db, market=market, symbols=symbols)


def _aware(value: datetime, *, default_timezone=UTC) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=default_timezone)
    return value


def _current_instant(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return _aware(now).astimezone(UTC)


def _canonical_routines(values: Sequence[str]) -> tuple[RoutineKey, ...]:
    raw = list(values)
    if len(raw) != len(set(raw)) or any(value not in ROUTINE_KEYS for value in raw):
        raise ValueError("stored daily routine settings are invalid")
    selected = set(raw)
    return tuple(key for key in ROUTINE_KEYS if key in selected)


def _canonical_market_scope(value: str) -> RecommendationMarketScope:
    if value not in {"KR_ONLY", "US_ONLY", "KR_US"}:
        raise ValueError("stored recommendation market scope is invalid")
    return value  # type: ignore[return-value]


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _quote_occurred_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _rapid_kind(rate: Decimal | None) -> RoutineKey | None:
    if rate is None:
        return None
    if rate >= _RAPID_CHANGE_THRESHOLD:
        return "RAPID_RISE"
    if rate <= -_RAPID_CHANGE_THRESHOLD:
        return "RAPID_FALL"
    return None


def _rate_text(rate: Decimal) -> str:
    """등락률 wire 문자열. Numeric(10,4) 저장값도 화면과 같은 소수 2자리로 맞춘다."""
    return format(rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _provenance(observation: _PriceObservation) -> str:
    if observation.completed:
        return "저장된 최신 완료 일봉 종가"
    if observation.session_state == "CLOSED":
        return "장 마감 후 최신 시세 스냅샷"
    if observation.session_state is None:
        return "장 상태 미확인 최신 시세 스냅샷"
    return f"{observation.session_state} 형성 중 시세"


def _price_alert(
    *,
    kind: RoutineKey,
    market: WatchlistMarket,
    symbol: str,
    name: str,
    detected_rate: Decimal,
    detected_at: datetime,
    source: str,
    current_rate: Decimal | None,
    last_seen_at: datetime | None,
    provenance: str | None,
    routine_date: date,
) -> DailyRoutineAlert:
    """하루 동안 같은 id를 유지하는 가격 알림 한 건을 만든다.

    headline은 첫 포착 등락률로 고정하고, 지금 등락률은 별도 필드로 준다.
    등락률이 임계값 안으로 돌아왔거나 방향이 바뀌면 ``recovered``다.
    """

    direction = "급등" if kind == "RAPID_RISE" else "급락"
    if provenance is None:
        summary = (
            "오늘 처음 포착된 일간 등락률입니다. 원 시각과 출처를 그대로 표시합니다."
        )
    else:
        summary = (
            f"{provenance} 기준 일간 등락률입니다. 원 시각과 출처를 그대로 표시합니다."
        )
    return DailyRoutineAlert(
        id=f"price:{routine_date.isoformat()}:{kind}:{market}:{symbol}",
        kind=kind,
        headline=f"{name}({symbol}) {format(detected_rate, '+.2f')}% {direction}",
        summary=summary,
        symbol=symbol,
        market=market,
        source=source,
        url=None,
        occurred_at=detected_at,
        detected_rate_pct=_rate_text(detected_rate),
        current_rate_pct=_rate_text(current_rate) if current_rate is not None else None,
        recovered=current_rate is not None and _rapid_kind(current_rate) != kind,
        last_seen_at=last_seen_at,
    )


def _article_occurred_at(value: datetime) -> datetime:
    # news_articles timestamps are legacy KST wall-clock values without a zone.
    return _aware(value, default_timezone=KST)


def _normalized_text(value: str | None) -> str:
    return _SPACE_RE.sub(" ", (value or "").strip()).casefold()


def _normalized_source(value: str | None) -> str:
    cleaned = _SOURCE_PUNCT_RE.sub(" ", (value or "").casefold()).strip()
    return _SPACE_RE.sub(" ", cleaned)


def _is_allowed_financial_source(value: str | None) -> bool:
    normalized = _normalized_source(value)
    if normalized in _SOURCE_ALIASES:
        return True
    return any(
        normalized.startswith(f"{alias} ")
        for alias in _SOURCE_ALIASES
        if len(alias) >= 4
    )


def _matches_policy_topic(value: str) -> bool:
    normalized = _normalized_text(value)
    return any(term in normalized for term in _TOPIC_TERMS)


def _canonical_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip().casefold()
    if not parts.scheme or not parts.netloc:
        return value.strip().casefold()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, "", ""))


class DailyRoutineService:
    def __init__(self, *, quote_loader: QuoteBatchLoader | None = None) -> None:
        self._quote_loader = quote_loader or _default_quote_loader

    async def get(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        now: datetime | None = None,
    ) -> DailyRoutineResponse:
        instant = _current_instant(now)
        selection = await self._load_effective_selection(db, owner_user_id, now=instant)
        alerts = await self._alerts_for_selection(
            db, owner_user_id, selection=selection, now=instant
        )
        return self._response(selection, alerts)

    async def recommendation_markets(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        now: datetime | None = None,
    ) -> frozenset[MarketKey]:
        instant = _current_instant(now)
        selection = await self._load_effective_selection(
            db,
            owner_user_id,
            now=instant,
        )
        if selection.recommendation_market_scope == "KR_ONLY":
            return frozenset({"kr"})
        if selection.recommendation_market_scope == "US_ONLY":
            return frozenset({"us"})
        return frozenset({"kr", "us"})

    async def update(
        self,
        db: AsyncSession,
        owner_user_id: int,
        enabled_routines: Sequence[RoutineKey],
        recommendation_market_scope: RecommendationMarketScope | None = None,
        *,
        now: datetime | None = None,
    ) -> DailyRoutineResponse:
        instant = _current_instant(now)
        routine_date = instant.astimezone(KST).date()
        canonical = _canonical_routines(enabled_routines)
        if recommendation_market_scope is None:
            recommendation_market_scope = (
                await self._load_effective_selection(
                    db,
                    owner_user_id,
                    now=instant,
                )
            ).recommendation_market_scope
        statement = pg_insert(KAssetDailyRoutineSetting).values(
            owner_user_id=owner_user_id,
            routine_date=routine_date,
            enabled_routines=list(canonical),
            recommendation_market_scope=recommendation_market_scope,
            updated_at=instant,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[
                KAssetDailyRoutineSetting.owner_user_id,
                KAssetDailyRoutineSetting.routine_date,
            ],
            set_={
                "enabled_routines": list(canonical),
                "recommendation_market_scope": recommendation_market_scope,
                "updated_at": instant,
            },
        ).returning(KAssetDailyRoutineSetting.updated_at)
        updated_at = (await db.execute(statement)).scalar_one()
        await db.commit()

        selection = _EffectiveSelection(
            routine_date=routine_date,
            inherited_from=None,
            enabled_routines=canonical,
            recommendation_market_scope=recommendation_market_scope,
            updated_at=_aware(updated_at),
        )
        alerts = await self._alerts_for_selection(
            db, owner_user_id, selection=selection, now=instant
        )
        return self._response(selection, alerts)

    async def get_alerts(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        now: datetime | None = None,
    ) -> list[DailyRoutineAlert]:
        """Return today's effective alerts without changing any runtime state."""

        instant = _current_instant(now)
        selection = await self._load_effective_selection(db, owner_user_id, now=instant)
        return await self._alerts_for_selection(
            db, owner_user_id, selection=selection, now=instant
        )

    async def _load_effective_selection(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        now: datetime,
    ) -> _EffectiveSelection:
        today = now.astimezone(KST).date()
        row = await db.scalar(
            select(KAssetDailyRoutineSetting)
            .where(
                KAssetDailyRoutineSetting.owner_user_id == owner_user_id,
                KAssetDailyRoutineSetting.routine_date <= today,
            )
            .order_by(KAssetDailyRoutineSetting.routine_date.desc())
            .limit(1)
        )
        if row is None:
            return _EffectiveSelection(
                routine_date=today,
                inherited_from=None,
                enabled_routines=ROUTINE_KEYS,
                recommendation_market_scope="KR_US",
                updated_at=now,
            )
        return _EffectiveSelection(
            routine_date=today,
            inherited_from=(None if row.routine_date == today else row.routine_date),
            enabled_routines=_canonical_routines(row.enabled_routines),
            recommendation_market_scope=_canonical_market_scope(
                row.recommendation_market_scope
            ),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _response(
        selection: _EffectiveSelection,
        alerts: list[DailyRoutineAlert],
    ) -> DailyRoutineResponse:
        return DailyRoutineResponse(
            date=selection.routine_date,
            inherited_from=selection.inherited_from,
            enabled_routines=list(selection.enabled_routines),
            recommendation_market_scope=selection.recommendation_market_scope,
            available_routines=list(_AVAILABLE_ROUTINES),
            alerts=alerts,
            updated_at=selection.updated_at,
        )

    async def _alerts_for_selection(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        selection: _EffectiveSelection,
        now: datetime,
    ) -> list[DailyRoutineAlert]:
        enabled = frozenset(selection.enabled_routines)
        alerts: list[DailyRoutineAlert] = []
        if enabled & {"RAPID_RISE", "RAPID_FALL"}:
            alerts.extend(
                await self._load_price_alerts(db, owner_user_id, enabled, now=now)
            )
        if enabled & {"TRUMP_POLICY", "GLOBAL_FINANCIAL_NEWS"}:
            alerts.extend(await self._load_news_alerts(db, enabled, now=now))
        return sorted(
            alerts,
            key=lambda alert: (alert.occurred_at, alert.kind, alert.id),
            reverse=True,
        )

    @staticmethod
    def _quote_daily_change_percent(quote: object) -> Decimal | None:
        """Current snapshot vs previous close, including extended sessions."""

        price = _decimal(getattr(quote, "price", None))
        previous_close = _decimal(getattr(quote, "previous_close", None))
        if price is None or previous_close is None or previous_close == 0:
            return None
        return (price - previous_close) / previous_close * Decimal(100)

    @staticmethod
    async def _load_watch_symbols(
        db: AsyncSession, owner_user_id: int
    ) -> list[_WatchSymbol]:
        rows = (
            await db.execute(
                select(Instrument.symbol, Instrument.name, Instrument.type)
                .join(
                    UserWatchItem,
                    UserWatchItem.instrument_id == Instrument.id,
                )
                .where(
                    UserWatchItem.user_id == owner_user_id,
                    UserWatchItem.is_active.is_(True),
                    Instrument.is_active.is_(True),
                    Instrument.type.in_(
                        (
                            InstrumentType.equity_kr,
                            InstrumentType.equity_us,
                            InstrumentType.crypto,
                        )
                    ),
                )
                .order_by(UserWatchItem.id)
            )
        ).all()
        market_by_type: dict[InstrumentType, WatchlistMarket] = {
            InstrumentType.equity_kr: "KRX",
            InstrumentType.equity_us: "US",
            InstrumentType.crypto: "CRYPTO",
        }
        return [
            _WatchSymbol(
                symbol=str(symbol).strip().upper(),
                name=str(name),
                market=market_by_type[instrument_type],
            )
            for symbol, name, instrument_type in rows
        ]

    async def _load_price_observations(
        self, db: AsyncSession, owner_user_id: int
    ) -> list[_PriceObservation]:
        """관심종목 전체의 현재 일간 등락률을 읽는다. 임계값 판정은 하지 않는다."""

        watch_symbols = await self._load_watch_symbols(db, owner_user_id)
        by_market: dict[WatchlistMarket, list[_WatchSymbol]] = {
            "KRX": [],
            "US": [],
            "CRYPTO": [],
        }
        for item in watch_symbols:
            by_market[item.market].append(item)

        observations: list[_PriceObservation] = []
        quoted_markets: tuple[WatchlistMarket, ...] = ("KRX", "US")
        for market in quoted_markets:
            items = by_market[market]
            if not items:
                continue
            names = {item.symbol: item.name for item in items}
            quotes = await self._quote_loader(
                db, market, [item.symbol for item in items]
            )
            for quote in quotes:
                name = names.get(quote.symbol)
                if name is None:
                    continue
                rate = self._quote_daily_change_percent(quote)
                occurred_at = _quote_occurred_at(quote.as_of)
                if rate is None or occurred_at is None:
                    continue
                observations.append(
                    _PriceObservation(
                        market=market,
                        symbol=quote.symbol,
                        name=name,
                        rate=rate,
                        occurred_at=occurred_at,
                        source=quote.source,
                        completed=quote.source == krx_quotes.CANDLE_QUOTE_SOURCE,
                        session_state=quote.session,
                    )
                )

        crypto_items = by_market["CRYPTO"]
        if crypto_items:
            candles = await DailyCandlesRepository(session=db).fetch_recent_batch(
                market=MarketKey.CRYPTO,
                symbols=[item.symbol for item in crypto_items],
                partition=None,
                count=2,
            )
            for item in crypto_items:
                rows = candles.get(item.symbol, [])
                if len(rows) < 2:
                    continue
                previous = _decimal(rows[-2].close)
                current = _decimal(rows[-1].close)
                if previous is None or current is None or previous == 0:
                    continue
                latest = rows[-1]
                observations.append(
                    _PriceObservation(
                        market="CRYPTO",
                        symbol=item.symbol,
                        name=item.name,
                        rate=(
                            (current - previous) / previous * Decimal("100")
                        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                        occurred_at=_aware(latest.time_utc),
                        source=latest.source,
                        completed=True,
                        session_state=None,
                    )
                )
        return observations

    async def _load_price_alerts(
        self,
        db: AsyncSession,
        owner_user_id: int,
        enabled: frozenset[str],
        *,
        now: datetime,
    ) -> list[DailyRoutineAlert]:
        """오늘(KST) 포착된 ±5% 알림을 돌려준다.

        지금 임계값을 넘는 종목은 즉시 기록되고, 이미 기록된 종목은 등락률이
        임계값 안으로 돌아와도 ``recovered``로 표시된 채 하루 동안 남는다. 기록
        실패는 로그만 남기고 지금 넘는 종목을 그대로 돌려준다 — 알림 조회가
        이력 저장 때문에 실패하면 안 된다.
        """

        observations = await self._load_price_observations(db, owner_user_id)
        by_symbol = {(item.market, item.symbol): item for item in observations}
        live: dict[tuple[str, str, str], _PriceObservation] = {}
        for item in observations:
            kind = _rapid_kind(item.rate)
            if kind is not None and kind in enabled:
                live[(kind, item.market, item.symbol)] = item

        routine_date = now.astimezone(KST).date()
        try:
            events = await self._record_price_alert_events(
                db,
                owner_user_id,
                routine_date=routine_date,
                live=live,
                by_symbol=by_symbol,
                now=now,
            )
        except Exception:
            logger.exception(
                "kasset routine price alert history unavailable: owner_user_id=%s",
                owner_user_id,
            )
            # 실패한 트랜잭션을 정리해야 세션을 계속 쓸 수 있다. 가짜 세션처럼
            # rollback이 없거나 실패해도 알림 응답은 그대로 나가야 한다.
            with suppress(Exception):
                await db.rollback()
            events = []

        alerts: list[DailyRoutineAlert] = []
        seen: set[tuple[str, str, str]] = set()
        for event in events:
            kind = event.kind
            if kind not in enabled or kind not in ("RAPID_RISE", "RAPID_FALL"):
                continue
            key = (kind, event.market, event.symbol)
            seen.add(key)
            observation = by_symbol.get((event.market, event.symbol))
            current_rate = (
                observation.rate if observation is not None else event.last_rate_pct
            )
            alerts.append(
                _price_alert(
                    kind=kind,  # type: ignore[arg-type]
                    market=event.market,  # type: ignore[arg-type]
                    symbol=event.symbol,
                    name=event.name,
                    detected_rate=Decimal(event.detected_rate_pct),
                    detected_at=_aware(event.detected_at),
                    source=event.source,
                    current_rate=current_rate,
                    last_seen_at=(
                        observation.occurred_at
                        if observation is not None
                        else (
                            _aware(event.last_seen_at)
                            if event.last_seen_at is not None
                            else None
                        )
                    ),
                    provenance=(
                        _provenance(observation) if observation is not None else None
                    ),
                    routine_date=routine_date,
                )
            )
        for key, item in live.items():
            if key in seen:
                continue
            kind, _market, _symbol = key
            alerts.append(
                _price_alert(
                    kind=kind,  # type: ignore[arg-type]
                    market=item.market,
                    symbol=item.symbol,
                    name=item.name,
                    detected_rate=item.rate,
                    detected_at=item.occurred_at,
                    source=item.source,
                    current_rate=item.rate,
                    last_seen_at=item.occurred_at,
                    provenance=_provenance(item),
                    routine_date=routine_date,
                )
            )
        return alerts

    @staticmethod
    async def _record_price_alert_events(
        db: AsyncSession,
        owner_user_id: int,
        *,
        routine_date: date,
        live: dict[tuple[str, str, str], _PriceObservation],
        by_symbol: dict[tuple[str, str], _PriceObservation],
        now: datetime,
    ) -> list[KAssetRoutinePriceAlertEvent]:
        """지금 넘는 종목을 오늘 기록에 넣고, 오늘 기록 전체를 최신 등락률로 돌려준다."""

        if live:
            statement = pg_insert(KAssetRoutinePriceAlertEvent).values(
                [
                    {
                        "owner_user_id": owner_user_id,
                        "routine_date": routine_date,
                        "kind": kind,
                        "market": item.market,
                        "symbol": item.symbol,
                        "name": item.name,
                        "detected_rate_pct": item.rate,
                        "detected_at": item.occurred_at,
                        "source": item.source,
                        "last_rate_pct": item.rate,
                        "last_seen_at": item.occurred_at,
                        "updated_at": now,
                    }
                    for (kind, _market, _symbol), item in live.items()
                ]
            )
            # 첫 포착 값(detected_*)은 그대로 두고 최근 관측만 갱신한다.
            statement = statement.on_conflict_do_update(
                constraint="uq_kasset_routine_price_alert_event_day_key",
                set_={
                    "last_rate_pct": statement.excluded.last_rate_pct,
                    "last_seen_at": statement.excluded.last_seen_at,
                    "updated_at": now,
                },
            )
            await db.execute(statement)

        events = list(
            (
                await db.scalars(
                    select(KAssetRoutinePriceAlertEvent).where(
                        KAssetRoutinePriceAlertEvent.owner_user_id == owner_user_id,
                        KAssetRoutinePriceAlertEvent.routine_date == routine_date,
                    )
                )
            ).all()
        )
        # 임계값 안으로 돌아온 종목도 관심종목에 남아 있으면 최근 등락률을 따라간다.
        for event in events:
            if (event.kind, event.market, event.symbol) in live:
                continue
            observation = by_symbol.get((event.market, event.symbol))
            if observation is None:
                continue
            event.last_rate_pct = observation.rate
            event.last_seen_at = observation.occurred_at
            event.updated_at = now
        await db.commit()
        return events

    async def _load_news_alerts(
        self,
        db: AsyncSession,
        enabled: frozenset[str],
        *,
        now: datetime,
    ) -> list[DailyRoutineAlert]:
        cutoff = (now.astimezone(KST) - _NEWS_WINDOW).replace(tzinfo=None)
        relevance_conditions = []
        if "TRUMP_POLICY" in enabled:
            title_conditions = [
                NewsArticle.title.ilike(f"%{term}%") for term in _TOPIC_TERMS
            ]
            summary_condition = exists().where(
                NewsAnalysisResult.article_id == NewsArticle.id,
                or_(
                    *(
                        NewsAnalysisResult.summary.ilike(f"%{term}%")
                        for term in _TOPIC_TERMS
                    )
                ),
            )
            relevance_conditions.extend((*title_conditions, summary_condition))
        if "GLOBAL_FINANCIAL_NEWS" in enabled:
            relevance_conditions.extend(
                NewsArticle.source.ilike(f"%{term}%") for term in _SOURCE_SQL_TERMS
            )
            relevance_conditions.extend(
                NewsArticle.source.ilike(term) for term in _SOURCE_SQL_EXACT
            )
        if not relevance_conditions:
            return []

        rows = list(
            (
                await db.scalars(
                    select(NewsArticle)
                    .where(
                        NewsArticle.article_published_at.is_not(None),
                        NewsArticle.article_published_at >= cutoff,
                        or_(*relevance_conditions),
                        complete_korean_analysis_exists(),
                    )
                    .order_by(
                        NewsArticle.article_published_at.desc(),
                        NewsArticle.id.desc(),
                    )
                    .limit(_NEWS_SCAN_LIMIT)
                )
            ).all()
        )
        analyses = await self._load_validated_analyses(db, [row.id for row in rows])

        counts: dict[RoutineKey, int] = {
            "RAPID_RISE": 0,
            "RAPID_FALL": 0,
            "TRUMP_POLICY": 0,
            "GLOBAL_FINANCIAL_NEWS": 0,
        }
        seen_urls: set[str] = set()
        seen_titles: set[tuple[str, str]] = set()
        alerts: list[DailyRoutineAlert] = []
        for row in rows:
            published_at = row.article_published_at
            if published_at is None:
                continue
            validated_analysis = analyses.get(row.id)
            if validated_analysis is None:
                continue
            validated_summary = validated_analysis.summary
            evidence_text = " ".join(
                value for value in (row.title, validated_summary) if value
            )
            kind: RoutineKey | None = None
            if "TRUMP_POLICY" in enabled and _matches_policy_topic(evidence_text):
                kind = "TRUMP_POLICY"
            elif "GLOBAL_FINANCIAL_NEWS" in enabled and _is_allowed_financial_source(
                row.source
            ):
                kind = "GLOBAL_FINANCIAL_NEWS"
            if kind is None or counts[kind] >= _NEWS_ALERT_LIMIT_PER_KIND:
                continue

            url_key = _canonical_url(row.url)
            title_key = (_normalized_text(row.title), _normalized_source(row.source))
            if (url_key and url_key in seen_urls) or title_key in seen_titles:
                continue
            if url_key:
                seen_urls.add(url_key)
            seen_titles.add(title_key)
            counts[kind] += 1

            alerts.append(
                DailyRoutineAlert(
                    id=f"news:{row.id}",
                    kind=kind,
                    headline=row.title,
                    summary=validated_summary,
                    translated_title=validated_analysis.translated_title,
                    translated_excerpt=validated_analysis.translated_excerpt,
                    symbol=(row.stock_symbol or None),
                    source=row.source,
                    url=row.url,
                    occurred_at=_article_occurred_at(published_at),
                )
            )
        return alerts

    @staticmethod
    async def _load_validated_analyses(
        db: AsyncSession, article_ids: Sequence[int]
    ) -> dict[int, _ValidatedNewsAnalysis]:
        if not article_ids:
            return {}
        rows = (
            await db.execute(
                select(
                    NewsAnalysisResult.article_id,
                    NewsAnalysisResult.summary,
                    NewsAnalysisResult.translated_title,
                    NewsAnalysisResult.translated_excerpt,
                )
                .join(NewsArticle, NewsArticle.id == NewsAnalysisResult.article_id)
                .where(
                    NewsAnalysisResult.article_id.in_(article_ids),
                    *complete_korean_analysis_conditions(),
                )
                .order_by(
                    NewsAnalysisResult.article_id,
                    NewsAnalysisResult.created_at.desc(),
                    NewsAnalysisResult.id.desc(),
                )
            )
        ).all()
        analyses: dict[int, _ValidatedNewsAnalysis] = {}
        for article_id, summary, translated_title, translated_excerpt in rows:
            normalized_summary = _SPACE_RE.sub(" ", (summary or "").strip())
            if normalized_summary and article_id not in analyses:
                analyses[int(article_id)] = _ValidatedNewsAnalysis(
                    summary=normalized_summary,
                    translated_title=(
                        _SPACE_RE.sub(" ", translated_title.strip())
                        if translated_title
                        else None
                    ),
                    translated_excerpt=(
                        _SPACE_RE.sub(" ", translated_excerpt.strip())
                        if translated_excerpt
                        else None
                    ),
                )
        return analyses


daily_routine_service = DailyRoutineService()

__all__ = ["DailyRoutineService", "daily_routine_service"]
