from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services.daily_candles.repository import MarketKey
from app.services.daily_candles.sync_service import (
    DailyCandleSyncService,
    SyncTarget,
)


class TestSyncOneSymbol:
    @pytest.mark.asyncio
    async def test_kis_kr_path_upserts_with_source_kis(self):
        repo = MagicMock()
        repo.latest_time_utc = AsyncMock(return_value=None)
        repo.upsert_rows = AsyncMock(return_value=10)
        repo.session = MagicMock()
        repo.session.commit = AsyncMock()
        repo.session.rollback = AsyncMock()

        frame = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=10, freq="B"),
                "open": [100.0] * 10,
                "high": [101.0] * 10,
                "low": [99.0] * 10,
                "close": [100.5] * 10,
                "volume": [1000] * 10,
                "value": [100500] * 10,
            }
        )
        kis_fetcher = AsyncMock(return_value=frame)
        yahoo_fetcher = AsyncMock(return_value=[])

        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=kis_fetcher,
            kis_us_fetcher=AsyncMock(),
            yahoo_us_fetcher=yahoo_fetcher,
            upbit_crypto_fetcher=AsyncMock(),
        )

        result = await svc.sync_one(
            target=SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
            horizon_bars=400,
        )

        kis_fetcher.assert_awaited_once()
        yahoo_fetcher.assert_not_awaited()
        repo.upsert_rows.assert_awaited_once()
        upserted_rows = repo.upsert_rows.await_args.kwargs["rows"]
        assert all(r.source == "kis" for r in upserted_rows)
        assert result.rows_upserted == 10
        repo.session.commit.assert_awaited_once()
        repo.session.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_us_kis_empty_falls_back_to_yahoo(self):
        repo = MagicMock()
        repo.latest_time_utc = AsyncMock(return_value=None)
        repo.upsert_rows = AsyncMock(return_value=5)
        repo.session = MagicMock()
        repo.session.commit = AsyncMock()
        repo.session.rollback = AsyncMock()

        from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow

        yahoo_rows = [
            YahooFallbackRow(
                time_utc=datetime(2024, 5, day, tzinfo=UTC),
                symbol="ILLIQUID",
                open=10.0,
                high=11.0,
                low=9.0,
                close=10.5,
                adj_close=10.4,
                volume=100.0,
                value=1050.0,
            )
            for day in range(1, 6)
        ]

        kis_us_fetcher = AsyncMock(return_value=pd.DataFrame())
        yahoo_fetcher = AsyncMock(return_value=yahoo_rows)

        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=AsyncMock(),
            kis_us_fetcher=kis_us_fetcher,
            yahoo_us_fetcher=yahoo_fetcher,
            upbit_crypto_fetcher=AsyncMock(),
        )

        result = await svc.sync_one(
            target=SyncTarget(market=MarketKey.US, symbol="ILLIQUID", partition="NASD"),
            horizon_bars=400,
        )

        kis_us_fetcher.assert_awaited_once()
        yahoo_fetcher.assert_awaited_once()
        assert repo.upsert_rows.await_count == 1  # only the yahoo path actually upserts
        upserted_rows = repo.upsert_rows.await_args.kwargs["rows"]
        assert all(r.source == "yahoo_fallback" for r in upserted_rows)
        assert result.rows_upserted == 5
        assert result.fallback_used is True
        repo.session.commit.assert_awaited_once()
        repo.session.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_us_kis_exception_falls_back_to_yahoo(self):
        from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow

        repo = MagicMock()
        repo.upsert_rows = AsyncMock(return_value=1)
        repo.session = MagicMock()
        repo.session.commit = AsyncMock()
        repo.session.rollback = AsyncMock()
        yahoo_fetcher = AsyncMock(
            return_value=[
                YahooFallbackRow(
                    time_utc=datetime(2024, 5, 1, tzinfo=UTC),
                    symbol="AAPL",
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    adj_close=98.25,
                    volume=1000.0,
                    value=100500.0,
                )
            ]
        )
        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=AsyncMock(),
            kis_us_fetcher=AsyncMock(side_effect=TimeoutError("kis down")),
            yahoo_us_fetcher=yahoo_fetcher,
            upbit_crypto_fetcher=AsyncMock(),
        )

        result = await svc.sync_one(
            target=SyncTarget(
                market=MarketKey.US,
                symbol="AAPL",
                partition="NASD",
            ),
            horizon_bars=400,
        )

        yahoo_fetcher.assert_awaited_once_with(symbol="AAPL", n=400)
        written = repo.upsert_rows.await_args.kwargs["rows"]
        assert written[0].source == "yahoo_fallback"
        assert written[0].adj_close == pytest.approx(98.25)
        assert result.fallback_used is True
        repo.session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_us_malformed_success_does_not_fall_back(self):
        repo = MagicMock()
        repo.upsert_rows = AsyncMock()
        repo.session = MagicMock()
        yahoo_fetcher = AsyncMock()
        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=AsyncMock(),
            kis_us_fetcher=AsyncMock(
                return_value=pd.DataFrame(
                    {
                        "date": ["2024-05-01"],
                        "open": [100.0],
                        "high": [101.0],
                        "low": [99.0],
                        "volume": [1000],
                    }
                )
            ),
            yahoo_us_fetcher=yahoo_fetcher,
            upbit_crypto_fetcher=AsyncMock(),
        )

        with pytest.raises(ValueError, match="close가 없습니다"):
            await svc.sync_one(
                target=SyncTarget(
                    market=MarketKey.US,
                    symbol="AAPL",
                    partition="NASD",
                ),
                horizon_bars=400,
            )

        yahoo_fetcher.assert_not_awaited()
        repo.upsert_rows.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("defect", ["missing_adj_close", "duplicate_date"])
    async def test_us_adjusted_close_backfill_rejects_required_slice_defects(
        self, defect: str
    ):
        from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow

        repo = MagicMock()
        repo.upsert_us_adjusted_close = AsyncMock()
        repo.session = MagicMock()
        repo.session.commit = AsyncMock()
        start = datetime(2024, 1, 1, tzinfo=UTC)
        yahoo_rows = [
            YahooFallbackRow(
                time_utc=start
                + timedelta(
                    days=298 if defect == "duplicate_date" and index == 299 else index
                ),
                symbol="AAPL",
                open=100.0 + index,
                high=101.0 + index,
                low=99.0 + index,
                close=100.5 + index,
                adj_close=(
                    None
                    if defect == "missing_adj_close" and index == 299
                    else 98.25 + index
                ),
                volume=1000.0,
                value=(100.5 + index) * 1000.0,
            )
            for index in range(300)
        ]
        yahoo_fetcher = AsyncMock(return_value=yahoo_rows)
        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=AsyncMock(),
            kis_us_fetcher=AsyncMock(),
            yahoo_us_fetcher=yahoo_fetcher,
            upbit_crypto_fetcher=AsyncMock(),
        )

        with pytest.raises(RuntimeError, match="완전하지 않습니다"):
            await svc.sync_us_adjusted_close(
                target=SyncTarget(
                    market=MarketKey.US,
                    symbol="AAPL",
                    partition="NASD",
                ),
                horizon_bars=400,
            )

        yahoo_fetcher.assert_awaited_once_with(symbol="AAPL", n=400)
        repo.upsert_us_adjusted_close.assert_not_awaited()
        repo.session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_us_adjusted_close_backfill_rejects_fewer_than_readiness_bars(
        self,
    ):
        from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow

        start = datetime(2024, 1, 1, tzinfo=UTC)
        yahoo_rows = [
            YahooFallbackRow(
                time_utc=start + timedelta(days=index),
                symbol="SNDK",
                open=50.0,
                high=51.0,
                low=49.0,
                close=50.5,
                adj_close=50.25,
                volume=1000.0,
                value=50500.0,
            )
            for index in range(251)
        ]
        repo = MagicMock()
        repo.upsert_us_adjusted_close = AsyncMock()
        repo.session = MagicMock()
        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=AsyncMock(),
            kis_us_fetcher=AsyncMock(),
            yahoo_us_fetcher=AsyncMock(return_value=yahoo_rows),
            upbit_crypto_fetcher=AsyncMock(),
        )

        with pytest.raises(RuntimeError, match="required=252 received=251"):
            await svc.sync_us_adjusted_close(
                target=SyncTarget(
                    market=MarketKey.US,
                    symbol="SNDK",
                    partition="NASD",
                ),
                horizon_bars=400,
            )

        repo.upsert_us_adjusted_close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_us_adjusted_close_backfill_accepts_300_of_400_bars(self):
        from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow

        repo = MagicMock()
        repo.upsert_us_adjusted_close = AsyncMock(return_value=299)
        repo.session = MagicMock()
        repo.session.commit = AsyncMock()
        repo.session.rollback = AsyncMock()
        kis_us_fetcher = AsyncMock()
        start = datetime(2024, 1, 1, tzinfo=UTC)
        yahoo_rows = [
            YahooFallbackRow(
                time_utc=start + timedelta(days=index),
                symbol="SNDK",
                open=30.0 + index,
                high=31.0 + index,
                low=29.0 + index,
                close=30.5 + index,
                adj_close=None if index == 0 else 29.5 + index,
                volume=1000.0,
                value=(30.5 + index) * 1000.0,
            )
            for index in range(300)
        ]
        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=AsyncMock(),
            kis_us_fetcher=kis_us_fetcher,
            yahoo_us_fetcher=AsyncMock(return_value=yahoo_rows),
            upbit_crypto_fetcher=AsyncMock(),
        )
        target = SyncTarget(
            market=MarketKey.US,
            symbol="SNDK",
            partition="NASD",
        )

        assert await svc.sync_us_adjusted_close(target=target, horizon_bars=400) == 299

        kis_us_fetcher.assert_not_awaited()
        written = repo.upsert_us_adjusted_close.await_args.kwargs["rows"]
        assert len(written) == 299
        assert written[0].time_utc == start + timedelta(days=1)
        assert written[-1].time_utc == start + timedelta(days=299)
        assert all(row.adj_close is not None for row in written)
        assert all(row.partition == "NASD" for row in written)
        assert all(row.source == "yahoo_fallback" for row in written)
        repo.session.commit.assert_awaited_once()
        repo.session.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kr_commit_failure_triggers_rollback(self):
        repo = MagicMock()
        repo.latest_time_utc = AsyncMock(return_value=None)
        repo.upsert_rows = AsyncMock(return_value=10)
        repo.session = MagicMock()
        repo.session.commit = AsyncMock(side_effect=RuntimeError("db error"))
        repo.session.rollback = AsyncMock()

        frame = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=10, freq="B"),
                "open": [100.0] * 10,
                "high": [101.0] * 10,
                "low": [99.0] * 10,
                "close": [100.5] * 10,
                "volume": [1000] * 10,
                "value": [100500] * 10,
            }
        )
        kis_fetcher = AsyncMock(return_value=frame)

        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=kis_fetcher,
            kis_us_fetcher=AsyncMock(),
            yahoo_us_fetcher=AsyncMock(),
            upbit_crypto_fetcher=AsyncMock(),
        )

        with pytest.raises(RuntimeError, match="db error"):
            await svc.sync_one(
                target=SyncTarget(
                    market=MarketKey.KR, symbol="005930", partition="KRX"
                ),
                horizon_bars=400,
            )

        repo.upsert_rows.assert_awaited_once()  # upsert ran
        repo.session.commit.assert_awaited_once()  # commit attempted
        repo.session.rollback.assert_awaited_once()  # rollback called


@pytest.mark.asyncio
async def test_crypto_universe_uses_canonical_upbit_partition() -> None:
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(return_value=[SimpleNamespace(market="KRW-BTC")])
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc._resolve_universe(market="crypto")

    assert targets == [
        SyncTarget(
            market=MarketKey.CRYPTO,
            symbol="KRW-BTC",
            partition="upbit_krw",
        )
    ]


@pytest.mark.asyncio
async def test_backfill_kr_targets_are_only_eligible_common_shares() -> None:
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        return_value=[
            SimpleNamespace(symbol="005930", exchange="KOSPI"),
            SimpleNamespace(symbol="000660", exchange="KOSPI"),
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc.resolve_backfill_targets(
        market="kr",
        resume_after="000660",
        limit=1,
    )

    assert targets == [
        SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX")
    ]
    sql = str(repo.session.execute.await_args.args[0])
    assert "is_common_share IS TRUE" in sql
    assert "security_type = 'STOCK'" in sql
    assert "COALESCE(krx_trading_suspended, FALSE) = FALSE" in sql
    assert "nxt_trading_suspended" not in sql
    assert "delist_date IS NULL" in sql


@pytest.mark.asyncio
async def test_backfill_us_targets_map_partition_then_resume_and_limit() -> None:
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        return_value=[
            SimpleNamespace(symbol="BBB", exchange="NYSE"),
            SimpleNamespace(symbol="AAA", exchange="NASDAQ"),
            SimpleNamespace(symbol="ZZZ", exchange="AMEX"),
            SimpleNamespace(symbol="BAD", exchange="OTC"),
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc.resolve_backfill_targets(
        market="us",
        resume_after="AAA",
        limit=1,
    )

    assert targets == [SyncTarget(market=MarketKey.US, symbol="BBB", partition="NYSE")]
    sql = str(repo.session.execute.await_args.args[0])
    assert "is_common_stock IS TRUE" in sql
    assert "is_active IS TRUE" in sql


@pytest.mark.asyncio
async def test_cohort_backfill_targets_use_exact_members_and_universe_exchange() -> (
    None
):
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        side_effect=[
            [SimpleNamespace(market="us")],
            [
                SimpleNamespace(
                    symbol="AAPL",
                    rank=1,
                    member_kind="active",
                    exchange="NASDAQ",
                ),
                SimpleNamespace(
                    symbol="SOXL",
                    rank=1,
                    member_kind="forced",
                    exchange="AMEX",
                ),
                SimpleNamespace(
                    symbol="TQQQ",
                    rank=2,
                    member_kind="forced",
                    exchange="NASD",
                ),
            ],
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc.resolve_cohort_backfill_targets(
        market="us",
        cohort_id="us-2026-08-30",
    )

    assert targets == [
        SyncTarget(market=MarketKey.US, symbol="AAPL", partition="NASD"),
        SyncTarget(market=MarketKey.US, symbol="SOXL", partition="AMEX"),
        SyncTarget(market=MarketKey.US, symbol="TQQQ", partition="NASD"),
    ]
    member_query = str(repo.session.execute.await_args_list[1].args[0])
    assert "member.member_kind IN ('active', 'forced')" in member_query
    assert "ORDER BY member.rank, member.symbol" in member_query
    assert "is_common_stock" not in member_query
    assert "is_etf" not in member_query
    assert "is_leveraged" not in member_query


@pytest.mark.asyncio
async def test_cohort_backfill_rejects_market_mismatch_before_members() -> None:
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(return_value=[SimpleNamespace(market="kr")])
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    with pytest.raises(ValueError, match="일치하지 않습니다"):
        await svc.resolve_cohort_backfill_targets(
            market="us",
            cohort_id="kr-2026-08-30",
        )

    repo.session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_top_market_cap_kr_uses_latest_positive_eligible_partition() -> None:
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        return_value=[
            SimpleNamespace(symbol="005930", exchange="KRX"),
            SimpleNamespace(symbol="000660", exchange="KRX"),
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc.resolve_top_market_cap_targets(market="kr", count=2)

    assert targets == [
        SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
        SyncTarget(market=MarketKey.KR, symbol="000660", partition="KRX"),
    ]
    query = str(repo.session.execute.await_args.args[0])
    assert "MAX(snapshot_date)" in query
    assert "valuation.market_cap > 0" in query
    assert "universe.security_type = 'STOCK'" in query
    assert "universe.is_common_share IS TRUE" in query
    assert "ORDER BY market_cap DESC, symbol ASC" in query
    assert repo.session.execute.await_args.args[1] == {
        "market": "kr",
        "count": 2,
    }


@pytest.mark.asyncio
async def test_top_market_cap_us_maps_only_supported_exchanges() -> None:
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        return_value=[
            SimpleNamespace(symbol="MSFT", exchange="NASDAQ"),
            SimpleNamespace(symbol="IBM", exchange="NYQ"),
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc.resolve_top_market_cap_targets(market="us", count=2)

    assert targets == [
        SyncTarget(market=MarketKey.US, symbol="MSFT", partition="NASD"),
        SyncTarget(market=MarketKey.US, symbol="IBM", partition="NYSE"),
    ]
    query = str(repo.session.execute.await_args.args[0])
    assert "universe.is_common_stock IS TRUE" in query
    assert "universe.exchange" in query
    assert "ORDER BY market_cap DESC, symbol ASC" in query


@pytest.mark.asyncio
async def test_top_market_cap_fails_closed_when_latest_partition_is_short() -> None:
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        return_value=[SimpleNamespace(symbol="005930", exchange="KRX")]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="requested=2 received=1"):
        await svc.resolve_top_market_cap_targets(market="kr", count=2)


@pytest.mark.asyncio
async def test_kr_universe_unions_active_watchlist_symbols() -> None:
    """관심종목이 유니버스에 없으면 그 종목 일봉이 영구히 비어 있다.

    실측 결함(2026-08-28): `kr_symbol_universe`가 0행이라 이 job이 매일 대상
    0건으로 끝났고, 사용자가 추가한 종목은 전일종가·차트가 계속 비었다.
    """
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        side_effect=[
            [SimpleNamespace(symbol="005930")],
            [SimpleNamespace(symbol="005380"), SimpleNamespace(symbol="005930")],
            [],
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc._resolve_universe(market="kr")

    # 중복 없이 합쳐지고 정렬된다.
    assert targets == [
        SyncTarget(market=MarketKey.KR, symbol="005380", partition="KRX"),
        SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
    ]


@pytest.mark.asyncio
async def test_us_watchlist_without_exchange_defaults_to_nasd_partition() -> None:
    """거래소 행이 없는 관심종목도 읽기 경로가 조회하는 파티션으로 들어간다."""

    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        side_effect=[
            [SimpleNamespace(symbol="AAPL", exchange="NASD")],
            [
                SimpleNamespace(symbol="TQQQ", exchange=None),
                SimpleNamespace(symbol="SOXL", exchange="NASDAQ"),
            ],
            [],
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc._resolve_universe(market="us")

    assert targets == [
        SyncTarget(market=MarketKey.US, symbol="AAPL", partition="NASD"),
        SyncTarget(market=MarketKey.US, symbol="SOXL", partition="NASD"),
        SyncTarget(market=MarketKey.US, symbol="TQQQ", partition="NASD"),
    ]


@pytest.mark.asyncio
async def test_universe_table_partition_wins_over_watchlist() -> None:
    """같은 종목이 양쪽에 있으면 유니버스 테이블의 파티션을 유지한다."""

    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        side_effect=[
            [SimpleNamespace(symbol="TQQQ", exchange="NYSE")],
            [SimpleNamespace(symbol="TQQQ", exchange=None)],
            [],
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc._resolve_universe(market="us")

    assert targets == [SyncTarget(market=MarketKey.US, symbol="TQQQ", partition="NYSE")]


@pytest.mark.asyncio
async def test_kr_universe_unions_research_cohort_members() -> None:
    """코호트 멤버가 활성 유니버스에서 빠져도 일봉 수집은 계속돼야 한다.

    readiness와 PAPER 승격은 불변 코호트를 기준으로 측정되므로, 멤버가 비활성/
    상장폐지로 빠지는 순간 수집이 끊기면 그 코호트 행은 영구히 stale이 되고
    승격 게이트를 다시 닫을 방법이 없다.
    """

    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        side_effect=[
            [SimpleNamespace(symbol="005930")],
            [],
            [
                SimpleNamespace(symbol="000660", exchange="KRX"),
                SimpleNamespace(symbol="005930", exchange="KRX"),
            ],
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc._resolve_universe(market="kr")

    assert targets == [
        SyncTarget(market=MarketKey.KR, symbol="000660", partition="KRX"),
        SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
    ]
    cohort_sql = str(repo.session.execute.await_args_list[2].args[0])
    assert "kasset_research_cohort_members" in cohort_sql
    assert "public.kr_symbol_universe" in cohort_sql
    assert repo.session.execute.await_args_list[2].args[1] == {"market": "kr"}


@pytest.mark.asyncio
async def test_us_cohort_member_without_a_valid_exchange_is_never_guessed() -> None:
    """파티션을 못 정하는 코호트 멤버는 임의 파티션으로 쓰지 않고 제외한다."""

    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        side_effect=[
            [],
            [],
            [
                SimpleNamespace(symbol="OLDCO", exchange=None),
                SimpleNamespace(symbol="BADX", exchange="TSE"),
                SimpleNamespace(symbol="NVDA", exchange="NASDAQ"),
            ],
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc._resolve_universe(market="us")

    assert targets == [SyncTarget(market=MarketKey.US, symbol="NVDA", partition="NASD")]


@pytest.mark.asyncio
async def test_crypto_sync_rejects_partition_that_disagrees_with_symbol() -> None:
    repo = MagicMock()
    repo.session = MagicMock()
    repo.upsert_rows = AsyncMock()
    upbit_fetcher = AsyncMock()
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=upbit_fetcher,
    )

    with pytest.raises(ValueError, match="must match"):
        await svc.sync_one(
            target=SyncTarget(
                market=MarketKey.CRYPTO,
                symbol="USDT-ETH",
                partition="upbit_krw",
            ),
            horizon_bars=30,
        )

    upbit_fetcher.assert_not_awaited()
    repo.upsert_rows.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_close_awaits_callbacks():
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.commit = AsyncMock()
    repo.session.rollback = AsyncMock()
    close_callback = AsyncMock()

    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        close_callbacks=[close_callback],
    )

    await svc.close()

    close_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_kr_benchmark_prefers_kis_index_and_preserves_provenance() -> None:
    repo = MagicMock()
    repo.upsert_rows = AsyncMock(return_value=2)
    repo.session = MagicMock()
    repo.session.commit = AsyncMock()
    repo.session.rollback = AsyncMock()
    kis_index_fetcher = AsyncMock(
        return_value=pd.DataFrame(
            {
                "date": ["2024-05-01", "2024-05-02"],
                "open": [2600.0, 2610.0],
                "high": [2610.0, 2620.0],
                "low": [2590.0, 2600.0],
                "close": [2605.0, 2615.0],
                "volume": [1.0, 2.0],
                "value": [2605.0, 5230.0],
            }
        )
    )
    naver_fetcher = AsyncMock()
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        kis_kr_benchmark_fetcher=kis_index_fetcher,
        naver_kr_benchmark_fetcher=naver_fetcher,
    )

    result = await svc.sync_benchmark(market=MarketKey.KR, horizon_bars=2)

    kis_index_fetcher.assert_awaited_once_with(symbol="KOSPI", n=2)
    naver_fetcher.assert_not_awaited()
    written = repo.upsert_rows.await_args.kwargs["rows"]
    assert [row.source for row in written] == ["kis_index", "kis_index"]
    assert result.fallback_used is False


@pytest.mark.asyncio
async def test_kr_benchmark_can_sync_kosdaq_independently() -> None:
    repo = MagicMock()
    repo.upsert_rows = AsyncMock(return_value=2)
    repo.session = MagicMock()
    repo.session.commit = AsyncMock()
    repo.session.rollback = AsyncMock()
    kis_index_fetcher = AsyncMock(
        return_value=pd.DataFrame(
            {
                "date": ["2024-05-01", "2024-05-02"],
                "open": [850.0, 860.0],
                "high": [860.0, 870.0],
                "low": [840.0, 850.0],
                "close": [855.0, 865.0],
                "volume": [1.0, 2.0],
                "value": [855.0, 1730.0],
            }
        )
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        kis_kr_benchmark_fetcher=kis_index_fetcher,
        naver_kr_benchmark_fetcher=AsyncMock(),
    )

    result = await svc.sync_benchmark(
        market=MarketKey.KR,
        horizon_bars=2,
        symbol="kosdaq",
    )

    kis_index_fetcher.assert_awaited_once_with(symbol="KOSDAQ", n=2)
    assert [row.symbol for row in repo.upsert_rows.await_args.kwargs["rows"]] == [
        "KOSDAQ",
        "KOSDAQ",
    ]
    assert result.target == SyncTarget(
        market=MarketKey.KR,
        symbol="KOSDAQ",
        partition="KRX",
    )


@pytest.mark.asyncio
async def test_kr_benchmark_persists_naver_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MagicMock()
    repo.upsert_rows = AsyncMock(return_value=2)
    repo.session = MagicMock()
    repo.session.commit = AsyncMock()
    repo.session.rollback = AsyncMock()
    monkeypatch.setattr(
        "app.services.daily_candles.sync_service.drop_forming_daily_rows",
        lambda frame, market: frame.iloc[:-1],
    )
    kis_index_fetcher = AsyncMock(side_effect=TimeoutError("KIS maintenance"))
    naver_fetcher = AsyncMock(
        return_value=pd.DataFrame(
            {
                "date": ["2024-05-01", "2024-05-02", "2024-05-03"],
                "open": [2600.0, 2610.0, 2620.0],
                "high": [2610.0, 2620.0, 2630.0],
                "low": [2590.0, 2600.0, 2610.0],
                "close": [2605.0, 2615.0, 2625.0],
                "volume": [1.0, 2.0, 3.0],
                "value": [2605.0, 5230.0, 7875.0],
            }
        )
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        kis_kr_benchmark_fetcher=kis_index_fetcher,
        naver_kr_benchmark_fetcher=naver_fetcher,
    )

    result = await svc.sync_benchmark(market=MarketKey.KR, horizon_bars=2)

    kis_index_fetcher.assert_awaited_once_with(symbol="KOSPI", n=2)
    naver_fetcher.assert_awaited_once_with(symbol="KOSPI", n=2)
    written = repo.upsert_rows.await_args.kwargs["rows"]
    assert [row.symbol for row in written] == ["KOSPI", "KOSPI"]
    assert [row.source for row in written] == ["naver", "naver"]
    assert [row.close for row in written] == [2605.0, 2615.0]
    assert result.target == SyncTarget(
        market=MarketKey.KR,
        symbol="KOSPI",
        partition="KRX",
    )
    assert result.fallback_used is True


@pytest.mark.asyncio
async def test_us_benchmark_uses_spy_nasd_kis_partition() -> None:
    repo = MagicMock()
    repo.upsert_rows = AsyncMock(return_value=1)
    repo.session = MagicMock()
    repo.session.commit = AsyncMock()
    repo.session.rollback = AsyncMock()
    kis_us_fetcher = AsyncMock(
        return_value=pd.DataFrame(
            {
                "date": ["2024-05-01"],
                "open": [500.0],
                "high": [501.0],
                "low": [499.0],
                "close": [500.5],
                "volume": [1000],
            }
        )
    )
    yahoo_fetcher = AsyncMock()
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=kis_us_fetcher,
        yahoo_us_fetcher=yahoo_fetcher,
        upbit_crypto_fetcher=AsyncMock(),
    )

    result = await svc.sync_benchmark(market=MarketKey.US, horizon_bars=1)

    kis_us_fetcher.assert_awaited_once_with(
        symbol="SPY",
        exchange_code="NASD",
        n=2,
    )
    yahoo_fetcher.assert_not_awaited()
    assert repo.upsert_rows.await_args.kwargs["update_adj_close"] is False
    assert result.target == SyncTarget(
        market=MarketKey.US,
        symbol="SPY",
        partition="NASD",
    )


@pytest.mark.asyncio
async def test_kr_empty_kis_falls_back_to_toss_daily():
    from app.services.daily_candles.repository import MarketKey
    from app.services.daily_candles.sync_service import (
        DailyCandleSyncService,
        SyncTarget,
    )

    upserted_rows = []

    class Repo:
        session = AsyncMock()

        async def upsert_rows(self, *, market, rows):
            upserted_rows.extend(rows)
            return len(rows)

    Repo.session.commit = AsyncMock()
    svc = DailyCandleSyncService(
        repository=Repo(),
        kis_kr_fetcher=AsyncMock(return_value=pd.DataFrame()),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        toss_kr_fetcher=AsyncMock(
            return_value=pd.DataFrame(
                [
                    {
                        "date": "2026-06-12",
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 2,
                        "volume": 10,
                        "value": 20,
                    }
                ]
            )
        ),
    )

    result = await svc.sync_one(
        target=SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
        horizon_bars=1,
    )

    assert result.fallback_used is True
    assert upserted_rows[0].source == "toss"


@pytest.mark.asyncio
async def test_us_empty_kis_and_yahoo_falls_back_to_toss_daily():
    from app.services.daily_candles.repository import MarketKey
    from app.services.daily_candles.sync_service import (
        DailyCandleSyncService,
        SyncTarget,
    )

    upserted_rows = []

    class Repo:
        session = AsyncMock()

        async def upsert_rows(self, *, market, rows, update_adj_close=True):
            upserted_rows.extend(rows)
            return len(rows)

    Repo.session.commit = AsyncMock()
    svc = DailyCandleSyncService(
        repository=Repo(),
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(return_value=pd.DataFrame()),
        yahoo_us_fetcher=AsyncMock(return_value=[]),
        upbit_crypto_fetcher=AsyncMock(),
        toss_us_fetcher=AsyncMock(
            return_value=pd.DataFrame(
                [
                    {
                        "date": "2026-06-12",
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 2,
                        "volume": 10,
                        "value": 20,
                    }
                ]
            )
        ),
    )

    result = await svc.sync_one(
        target=SyncTarget(market=MarketKey.US, symbol="AAPL", partition="NASD"),
        horizon_bars=1,
    )

    assert result.fallback_used is True
    assert upserted_rows[0].source == "toss_fallback"


@pytest.mark.asyncio
async def test_kr_kis_exception_falls_back_to_toss_daily():
    """ROB-706: a KIS exception (not just empty rows) triggers the KR Toss fallback."""
    upserted_rows = []

    class Repo:
        session = AsyncMock()

        async def upsert_rows(self, *, market, rows):
            upserted_rows.extend(rows)
            return len(rows)

    Repo.session.commit = AsyncMock()
    svc = DailyCandleSyncService(
        repository=Repo(),
        kis_kr_fetcher=AsyncMock(side_effect=TimeoutError("kis maintenance")),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        toss_kr_fetcher=AsyncMock(
            return_value=pd.DataFrame(
                [
                    {
                        "date": "2026-06-12",
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 2,
                        "volume": 10,
                        "value": 20,
                    }
                ]
            )
        ),
    )

    result = await svc.sync_one(
        target=SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
        horizon_bars=1,
    )

    assert result.fallback_used is True
    assert upserted_rows[0].source == "toss"


@pytest.mark.asyncio
async def test_kr_kis_exception_without_toss_fetcher_reraises():
    """No Toss fetcher wired (TOSS_API_ENABLED off) → a KIS exception still
    propagates (today's behavior); no upsert/commit runs."""
    repo = MagicMock()
    repo.upsert_rows = AsyncMock()
    repo.session = MagicMock()
    repo.session.commit = AsyncMock()
    repo.session.rollback = AsyncMock()

    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(side_effect=RuntimeError("kis down")),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        # no toss_kr_fetcher
    )

    with pytest.raises(RuntimeError, match="kis down"):
        await svc.sync_one(
            target=SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
            horizon_bars=1,
        )

    repo.upsert_rows.assert_not_awaited()
    repo.session.commit.assert_not_awaited()
