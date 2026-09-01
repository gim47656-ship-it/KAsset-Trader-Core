from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal, get_db
from app.core.timezone import KST
from app.extensions.kasset.api.ai_briefing import build_mobile_ai_briefing
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.paper_schemas import Quote
from app.extensions.kasset.daily_routine_service import DailyRoutineService
from app.extensions.kasset.models import (
    AndroidPaperOrder,
    KAssetDailyRoutineSetting,
)
from app.models.ai_recommendations import AIRecommendation
from app.models.news import NewsAnalysisResult, NewsArticle, Sentiment
from app.models.trading import (
    Exchange,
    Instrument,
    InstrumentType,
    User,
    UserRole,
    UserWatchItem,
)


@pytest_asyncio.fixture
async def routine_data(db_session: AsyncSession) -> AsyncIterator[dict[str, object]]:
    suffix = uuid4().hex[:10].upper()
    users = [
        User(
            username=f"routine-a-{suffix.lower()}",
            email=f"routine-a-{suffix.lower()}@example.com",
            role=UserRole.trader,
            is_active=True,
        ),
        User(
            username=f"routine-b-{suffix.lower()}",
            email=f"routine-b-{suffix.lower()}@example.com",
            role=UserRole.trader,
            is_active=True,
        ),
    ]
    exchange = Exchange(
        code=f"RT{suffix}",
        name="Routine Test Exchange",
        tz="Asia/Seoul",
    )
    db_session.add_all([*users, exchange])
    await db_session.flush()

    instruments = [
        Instrument(
            exchange_id=exchange.id,
            symbol=f"R{index}{suffix}",
            name=f"루틴 종목 {index}",
            type=InstrumentType.equity_kr,
            base_currency="KRW",
            is_active=True,
        )
        for index in range(3)
    ]
    db_session.add_all(instruments)
    await db_session.flush()
    user_ids = [user.id for user in users]
    instrument_ids = [instrument.id for instrument in instruments]
    exchange_id = exchange.id
    db_session.add_all(
        UserWatchItem(
            user_id=users[0].id,
            instrument_id=instrument.id,
            notify_cooldown=timedelta(hours=1),
            is_active=True,
        )
        for instrument in instruments
    )
    await db_session.commit()

    article_ids: list[int] = []
    try:
        yield {
            "users": users,
            "exchange": exchange,
            "instruments": instruments,
            "article_ids": article_ids,
            "suffix": suffix,
        }
    finally:
        await db_session.rollback()
        if article_ids:
            await db_session.execute(
                delete(NewsArticle).where(NewsArticle.id.in_(article_ids))
            )
        # Rollback expires ORM instances; use the scalar ids captured before commit.
        await db_session.execute(
            delete(KAssetDailyRoutineSetting).where(
                KAssetDailyRoutineSetting.owner_user_id.in_(user_ids)
            )
        )
        await db_session.execute(
            delete(UserWatchItem).where(UserWatchItem.user_id.in_(user_ids))
        )
        await db_session.execute(
            delete(Instrument).where(Instrument.id.in_(instrument_ids))
        )
        await db_session.execute(delete(Exchange).where(Exchange.id == exchange_id))
        await db_session.execute(delete(User).where(User.id.in_(user_ids)))
        await db_session.commit()


@pytest.mark.asyncio
async def test_kst_midnight_inherits_latest_empty_setting_and_keeps_users_isolated(
    db_session: AsyncSession,
    routine_data: dict[str, object],
) -> None:
    owner_a, owner_b = routine_data["users"]
    service = DailyRoutineService()
    before_midnight = datetime(2026, 8, 29, 14, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 8, 29, 15, 1, tzinfo=UTC)

    await service.update(db_session, owner_a.id, [], now=before_midnight)
    await service.update(
        db_session,
        owner_b.id,
        ["RAPID_RISE"],
        "US_ONLY",
        now=before_midnight,
    )

    inherited_a = await service.get(db_session, owner_a.id, now=after_midnight)
    inherited_b = await service.get(db_session, owner_b.id, now=after_midnight)

    assert inherited_a.date == date(2026, 8, 30)
    assert inherited_a.inherited_from == date(2026, 8, 29)
    assert inherited_a.enabled_routines == []
    assert inherited_b.inherited_from == date(2026, 8, 29)
    assert inherited_b.enabled_routines == ["RAPID_RISE"]
    assert inherited_a.recommendation_market_scope == "KR_US"
    assert inherited_b.recommendation_market_scope == "US_ONLY"
    assert await service.recommendation_markets(
        db_session,
        owner_b.id,
        now=after_midnight,
    ) == frozenset({"us"})

    stored_today = await service.update(
        db_session,
        owner_a.id,
        ["TRUMP_POLICY"],
        "KR_ONLY",
        now=after_midnight,
    )
    assert stored_today.inherited_from is None
    assert stored_today.enabled_routines == ["TRUMP_POLICY"]
    assert stored_today.recommendation_market_scope == "KR_ONLY"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(KAssetDailyRoutineSetting)
            .where(KAssetDailyRoutineSetting.owner_user_id == owner_a.id)
        )
        == 2
    )


@pytest.mark.asyncio
async def test_concurrent_puts_atomically_keep_one_owner_date_row(
    db_session: AsyncSession,
    routine_data: dict[str, object],
) -> None:
    owner = routine_data["users"][1]
    moment = datetime(2026, 8, 29, 5, 0, tzinfo=UTC)

    async def put(enabled: list[str]) -> None:
        async with AsyncSessionLocal() as session:
            await DailyRoutineService().update(
                session,
                owner.id,
                enabled,
                now=moment,
            )

    await asyncio.gather(put([]), put(["RAPID_RISE"]))
    await db_session.rollback()
    rows = (
        await db_session.scalars(
            select(KAssetDailyRoutineSetting).where(
                KAssetDailyRoutineSetting.owner_user_id == owner.id,
                KAssetDailyRoutineSetting.routine_date == date(2026, 8, 29),
            )
        )
    ).all()

    assert len(rows) == 1
    assert rows[0].enabled_routines in ([], ["RAPID_RISE"])


@pytest.mark.asyncio
async def test_api_put_contract_rejects_unknown_duplicate_and_extra_fields(
    db_session: AsyncSession,
    routine_data: dict[str, object],
) -> None:
    owner = routine_data["users"][1]
    app = FastAPI()
    install_android_compat_api(app)

    async def db_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def session_override() -> object:
        return SimpleNamespace(user=owner)

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_mobile_session] = session_override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://kasset.test",
    ) as client:
        accepted = await client.put(
            "/api/v1/ai/daily-routine",
            json={
                "enabledRoutines": [],
                "recommendationMarketScope": "KR_ONLY",
            },
        )
        unknown = await client.put(
            "/api/v1/ai/daily-routine",
            json={"enabledRoutines": ["UNKNOWN"]},
        )
        duplicate = await client.put(
            "/api/v1/ai/daily-routine",
            json={"enabledRoutines": ["RAPID_RISE", "RAPID_RISE"]},
        )
        invalid_scope = await client.put(
            "/api/v1/ai/daily-routine",
            json={
                "enabledRoutines": [],
                "recommendationMarketScope": "CRYPTO",
            },
        )
        extra = await client.put(
            "/api/v1/ai/daily-routine",
            json={"enabledRoutines": [], "enabled": True},
        )
        preserved = await client.put(
            "/api/v1/ai/daily-routine",
            json={"enabledRoutines": ["RAPID_FALL"]},
        )
        loaded = await client.get("/api/v1/ai/daily-routine")

    assert accepted.status_code == 200
    body = accepted.json()
    assert set(body) == {
        "date",
        "inheritedFrom",
        "enabledRoutines",
        "recommendationMarketScope",
        "availableRoutines",
        "alerts",
        "updatedAt",
    }
    assert body["inheritedFrom"] is None
    assert body["enabledRoutines"] == []
    assert body["recommendationMarketScope"] == "KR_ONLY"
    assert [item["key"] for item in body["availableRoutines"]] == [
        "RAPID_RISE",
        "RAPID_FALL",
        "TRUMP_POLICY",
        "GLOBAL_FINANCIAL_NEWS",
    ]
    assert preserved.status_code == 200
    assert preserved.json()["recommendationMarketScope"] == "KR_ONLY"
    assert loaded.status_code == 200
    assert loaded.json()["enabledRoutines"] == ["RAPID_FALL"]
    assert loaded.json()["recommendationMarketScope"] == "KR_ONLY"
    assert all(
        response.status_code == 422
        for response in (unknown, duplicate, invalid_scope, extra)
    )


@pytest.mark.asyncio
async def test_watchlist_price_alerts_include_exact_five_percent_boundaries_and_provenance(
    db_session: AsyncSession,
    routine_data: dict[str, object],
) -> None:
    owner = routine_data["users"][0]
    instruments: Sequence[Instrument] = routine_data["instruments"]
    quote_time = datetime(2026, 8, 29, 2, 30, tzinfo=UTC)
    rates = ("5.00", "-5.00", "4.99")
    prices = ("105.00", "95.00", "104.99")

    async def quotes(
        _db: AsyncSession, market: str, symbols: Sequence[str]
    ) -> list[Quote]:
        assert market == "KRX"
        return [
            Quote(
                broker="PAPER",
                market="KRX",
                symbol=symbol,
                name=None,
                currency="KRW",
                price=price,
                previous_close="100",
                change_amount="0",
                change_rate="0",
                session="AFTER_MARKET",
                regular_close="100",
                session_change_amount=rate,
                session_change_rate=rate,
                as_of=quote_time.isoformat(),
                source="TOSS_OPENAPI",
            )
            for symbol, rate, price in zip(symbols, rates, prices, strict=True)
        ]

    moment = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)
    db_session.add(
        KAssetDailyRoutineSetting(
            owner_user_id=owner.id,
            routine_date=moment.astimezone(KST).date(),
            enabled_routines=["RAPID_RISE", "RAPID_FALL"],
            updated_at=moment,
        )
    )
    await db_session.commit()
    service = DailyRoutineService(quote_loader=quotes)

    first = await service.get(db_session, owner.id, now=moment)
    second = await service.get(db_session, owner.id, now=moment)

    assert {(alert.symbol, alert.kind) for alert in first.alerts} == {
        (instruments[0].symbol, "RAPID_RISE"),
        (instruments[1].symbol, "RAPID_FALL"),
    }
    assert [alert.id for alert in first.alerts] == [alert.id for alert in second.alerts]
    assert all(alert.occurred_at == quote_time for alert in first.alerts)
    assert all(alert.source == "TOSS_OPENAPI" for alert in first.alerts)
    assert all(
        alert.summary is not None and "AFTER_MARKET 형성 중 시세" in alert.summary
        for alert in first.alerts
    )
    assert all(alert.translated_title is None for alert in first.alerts)
    assert all(alert.translated_excerpt is None for alert in first.alerts)
    wire_alerts = first.model_dump(by_alias=True, mode="json")["alerts"]
    assert all(alert["translatedTitle"] is None for alert in wire_alerts)
    assert all(alert["translatedExcerpt"] is None for alert in wire_alerts)


def _news_article(
    *,
    suffix: str,
    ordinal: int,
    title: str,
    source: str,
    published_at: datetime,
    content: str | None = "원문에 확인된 본문",
    url_suffix: str | None = None,
) -> NewsArticle:
    return NewsArticle(
        url=f"https://news.example.com/{suffix}/{url_suffix or ordinal}",
        title=title,
        source=source,
        article_content=content,
        summary=None,
        feed_source="test_feed",
        market="us",
        is_analyzed=False,
        article_published_at=published_at,
        scraped_at=published_at,
        created_at=published_at,
        updated_at=None,
    )


def _analysis(
    article_id: int,
    *,
    summary: str,
    created_at: datetime,
    translated_title: str | None = None,
    translated_excerpt: str | None = None,
) -> NewsAnalysisResult:
    return NewsAnalysisResult(
        article_id=article_id,
        model_name="validated-test-model",
        sentiment=Sentiment.NEUTRAL,
        sentiment_score=None,
        summary=summary,
        translated_title=translated_title,
        translated_excerpt=translated_excerpt,
        key_points=[summary],
        topics=None,
        price_impact=None,
        price_impact_score=None,
        confidence=90,
        analysis_quality="high",
        prompt="validated prompt",
        raw_response="validated response",
        processing_time_ms=1,
        created_at=created_at,
        updated_at=None,
    )


@pytest.mark.asyncio
async def test_news_alerts_apply_24h_source_topic_dedup_summary_and_caps_read_only(
    db_session: AsyncSession,
    routine_data: dict[str, object],
) -> None:
    owner = routine_data["users"][1]
    suffix = routine_data["suffix"]
    article_ids: list[int] = routine_data["article_ids"]
    moment = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)
    wall_now = moment.astimezone(KST).replace(tzinfo=None)
    db_session.add(
        KAssetDailyRoutineSetting(
            owner_user_id=owner.id,
            routine_date=moment.astimezone(KST).date(),
            enabled_routines=["TRUMP_POLICY", "GLOBAL_FINANCIAL_NEWS"],
            updated_at=moment,
        )
    )

    articles = [
        _news_article(
            suffix=suffix,
            ordinal=1,
            title="CNBC market bulletin",
            source="CNBC",
            published_at=wall_now - timedelta(minutes=1),
            content=None,
        ),
        _news_article(
            suffix=suffix,
            ordinal=2,
            title="Duplicate market bulletin",
            source="Reuters",
            published_at=wall_now - timedelta(minutes=2),
            url_suffix="duplicate-a?tracking=1",
        ),
        _news_article(
            suffix=suffix,
            ordinal=3,
            title="Duplicate market bulletin",
            source="Reuters",
            published_at=wall_now - timedelta(minutes=3),
            url_suffix="duplicate-b",
        ),
        *[
            _news_article(
                suffix=suffix,
                ordinal=10 + index,
                title=f"Global market update {index}",
                source="Yahoo Finance",
                published_at=wall_now - timedelta(minutes=4 + index),
            )
            for index in range(12)
        ],
        _news_article(
            suffix=suffix,
            ordinal=30,
            title="Trump tariff policy changes",
            source="Local Wire",
            published_at=wall_now - timedelta(minutes=20),
        ),
        _news_article(
            suffix=suffix,
            ordinal=31,
            title="Markets await a decision",
            source="Local Wire",
            published_at=wall_now - timedelta(minutes=21),
            content=(
                "The Federal Reserve described its policy decision and the current "
                "market conditions in the stored article body."
            ),
        ),
        _news_article(
            suffix=suffix,
            ordinal=32,
            title="Old Reuters item",
            source="Reuters",
            published_at=wall_now - timedelta(hours=25),
        ),
        _news_article(
            suffix=suffix,
            ordinal=33,
            title="Local sports result",
            source="Unknown Blog",
            published_at=wall_now - timedelta(minutes=5),
        ),
        _news_article(
            suffix=suffix,
            ordinal=34,
            title="Reuters item without Korean analysis",
            source="Reuters",
            published_at=wall_now - timedelta(seconds=30),
        ),
    ]
    db_session.add_all(articles)
    await db_session.flush()
    article_ids.extend(article.id for article in articles)
    # 완료 한국어 분석 fixture. Korean-gate 음성 케이스 한 건만 비워 두고 나머지는
    # 모두 채운다. 그래야 "Old Reuters item"은 24h cutoff로만, "Local sports
    # result"는 출처 allowlist로만 걸러진다는 사실이 각각 독립적으로 검증된다.
    generic_analysis_articles = [
        article
        for article in articles
        if article.title
        not in {
            # 아래 두 건은 전용 분석 fixture를 따로 넣는다.
            "CNBC market bulletin",
            "Markets await a decision",
            # Korean-gate 음성 케이스: 완료 한국어 분석이 없어야 한다.
            "Reuters item without Korean analysis",
        }
    ]
    db_session.add_all(
        [
            _analysis(
                articles[0].id,
                summary="CNBC 제목에 적힌 시장 소식입니다.",
                created_at=wall_now,
                translated_title="CNBC 시장 소식",
                translated_excerpt=None,
            ),
            _analysis(
                next(
                    article.id
                    for article in articles
                    if article.title == "Markets await a decision"
                ),
                summary="이전 연준 정책 결정 요약입니다.",
                translated_title="이전 시장 결정 번역 제목",
                translated_excerpt="이전 시장 결정 번역 발췌입니다.",
                created_at=wall_now - timedelta(minutes=1),
            ),
            _analysis(
                next(
                    article.id
                    for article in articles
                    if article.title == "Markets await a decision"
                ),
                summary="연준 정책 결정이 원문에 명시되었습니다.",
                created_at=wall_now,
                translated_title="시장은 정책 결정을 기다린다",
                translated_excerpt=(
                    "연방준비제도는 저장된 기사 본문에서 정책 결정과 현재 시장 "
                    "여건을 설명했다."
                ),
            ),
            *[
                _analysis(
                    article.id,
                    summary=f"한국어 뉴스 요약 {article.id}입니다.",
                    translated_title=f"한국어 번역 제목 {article.id}",
                    translated_excerpt=None,
                    created_at=wall_now,
                )
                for article in generic_analysis_articles
            ],
        ]
    )
    await db_session.commit()

    before_recommendations = await db_session.scalar(
        select(func.count()).select_from(AIRecommendation)
    )
    before_orders = await db_session.scalar(
        select(func.count()).select_from(AndroidPaperOrder)
    )
    response = await DailyRoutineService().get(db_session, owner.id, now=moment)
    context = await build_mobile_ai_briefing(
        db_session,
        owner_user_id=owner.id,
        market="us",
        now=moment,
    )
    after_recommendations = await db_session.scalar(
        select(func.count()).select_from(AIRecommendation)
    )
    after_orders = await db_session.scalar(
        select(func.count()).select_from(AndroidPaperOrder)
    )

    global_alerts = [
        alert for alert in response.alerts if alert.kind == "GLOBAL_FINANCIAL_NEWS"
    ]
    trump_alerts = [alert for alert in response.alerts if alert.kind == "TRUMP_POLICY"]
    assert len(global_alerts) == 10
    assert (
        sum(alert.headline == "Duplicate market bulletin" for alert in global_alerts)
        == 1
    )
    title_only = next(
        alert for alert in global_alerts if alert.headline == "CNBC market bulletin"
    )
    assert title_only.summary == "CNBC 제목에 적힌 시장 소식입니다."
    assert title_only.translated_title == "CNBC 시장 소식"
    assert title_only.translated_excerpt is None
    assert {alert.headline for alert in trump_alerts} == {
        "Trump tariff policy changes",
        "Markets await a decision",
    }
    translated_alert = next(
        alert for alert in trump_alerts if alert.headline == "Markets await a decision"
    )
    assert translated_alert.summary == "연준 정책 결정이 원문에 명시되었습니다."
    assert translated_alert.translated_title == "시장은 정책 결정을 기다린다"
    assert translated_alert.translated_excerpt == (
        "연방준비제도는 저장된 기사 본문에서 정책 결정과 현재 시장 여건을 설명했다."
    )
    assert translated_alert.url is not None
    assert translated_alert.url.endswith("/31")
    wire_alerts = {
        alert["headline"]: alert
        for alert in response.model_dump(by_alias=True, mode="json")["alerts"]
    }
    assert wire_alerts["Markets await a decision"]["translatedTitle"] == (
        "시장은 정책 결정을 기다린다"
    )
    assert wire_alerts["Markets await a decision"]["translatedExcerpt"] == (
        "연방준비제도는 저장된 기사 본문에서 정책 결정과 현재 시장 여건을 설명했다."
    )
    alert_headlines = {alert.headline for alert in response.alerts}
    # 완료 한국어 분석이 있어도 24h cutoff에서 걸린다.
    assert "Old Reuters item" not in alert_headlines
    # 완료 한국어 분석이 있어도 출처 allowlist에서 걸린다.
    assert "Local sports result" not in alert_headlines
    # 완료 한국어 분석만 없어서 Korean gate에서 걸린다.
    assert "Reuters item without Korean analysis" not in alert_headlines
    assert {alert.id for alert in context.routine_alerts} == {
        alert.id for alert in response.alerts
    }
    assert after_recommendations == before_recommendations
    assert after_orders == before_orders


def test_daily_routine_migration_is_additive_and_stacked_on_current_head() -> None:
    root = Path(__file__).resolve().parents[4]
    migration = root / "alembic/versions/20260829_kasset_daily_routines.py"
    text = migration.read_text(encoding="utf-8")
    upgrade = text.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]

    assert 'down_revision = "20260829_market_news_keyset"' in text
    assert "kasset_ai_daily_routine_settings" in upgrade
    assert "owner_user_id" in upgrade
    assert "routine_date" in upgrade
    assert "INSERT INTO" not in upgrade.upper()
