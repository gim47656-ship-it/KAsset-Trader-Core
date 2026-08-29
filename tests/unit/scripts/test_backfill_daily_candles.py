import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.daily_candles.repository import MarketKey
from app.services.daily_candles.sync_service import SyncTarget


def test_cli_argument_parser_accepts_required_args():
    from scripts.backfill_daily_candles import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(
        ["--market", "us", "--symbols", "AAPL,MSFT", "--horizon-bars", "500"]
    )
    assert ns.market == "us"
    assert ns.symbols == "AAPL,MSFT"
    assert ns.horizon_bars == 500
    assert ns.dry_run is False
    assert ns.all is False
    assert ns.include_benchmark is False
    assert ns.top_market_cap is None


def test_cli_argument_parser_requires_exactly_one_target_mode() -> None:
    from scripts.backfill_daily_candles import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--market", "us"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--market", "us", "--symbols", "AAPL", "--all"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--market", "us", "--all", "--top-market-cap", "100"])


def test_cli_argument_parser_accepts_all_resume_limit_and_benchmark() -> None:
    from scripts.backfill_daily_candles import _build_parser

    ns = _build_parser().parse_args(
        [
            "--market",
            "kr",
            "--all",
            "--resume-after",
            "005930",
            "--limit",
            "500",
            "--include-benchmark",
        ]
    )

    assert ns.all is True
    assert ns.symbols is None
    assert ns.resume_after == "005930"
    assert ns.limit == 500
    assert ns.include_benchmark is True


def test_cli_argument_parser_accepts_top_market_cap() -> None:
    from scripts.backfill_daily_candles import _build_parser

    ns = _build_parser().parse_args(["--market", "us", "--top-market-cap", "100"])

    assert ns.top_market_cap == 100
    assert ns.symbols is None
    assert ns.all is False


def test_cli_dry_run_flag():
    from scripts.backfill_daily_candles import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(["--market", "kr", "--symbols", "005930", "--dry-run"])
    assert ns.dry_run is True


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("KRW-BTC", "upbit_krw"),
        ("USDT-ETH", "upbit_usdt"),
    ],
)
def test_crypto_partition_is_derived_from_symbol(symbol: str, expected: str) -> None:
    from scripts.backfill_daily_candles import _partition_for_symbol

    assert (
        _partition_for_symbol(
            market=MarketKey.CRYPTO,
            symbol=symbol,
            requested_partition=None,
        )
        == expected
    )


def test_crypto_partition_override_must_match_symbol() -> None:
    from scripts.backfill_daily_candles import _partition_for_symbol

    with pytest.raises(ValueError, match="일치해야"):
        _partition_for_symbol(
            market=MarketKey.CRYPTO,
            symbol="USDT-ETH",
            requested_partition="upbit_krw",
        )


@pytest.mark.asyncio
async def test_crypto_backfill_builds_symbol_specific_canonical_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.backfill_daily_candles as cli

    service = SimpleNamespace(
        sync_one=AsyncMock(
            return_value=SimpleNamespace(rows_upserted=1, fallback_used=False)
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(cli, "_build_default_service", AsyncMock(return_value=service))
    args = cli._build_parser().parse_args(
        ["--market", "crypto", "--symbols", "KRW-BTC,USDT-ETH"]
    )

    assert await cli._amain(args) == 0
    assert [call.kwargs["target"] for call in service.sync_one.await_args_list] == [
        SyncTarget(
            market=MarketKey.CRYPTO,
            symbol="KRW-BTC",
            partition="upbit_krw",
        ),
        SyncTarget(
            market=MarketKey.CRYPTO,
            symbol="USDT-ETH",
            partition="upbit_usdt",
        ),
    ]
    service.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbols",
    [
        "KRW-BTC,USDT-ETH",
        "KRW-BTC,KRW-BTC/USD",
    ],
)
async def test_crypto_backfill_preflights_all_targets_before_service_or_sync(
    monkeypatch: pytest.MonkeyPatch,
    symbols: str,
) -> None:
    import scripts.backfill_daily_candles as cli

    service_factory = AsyncMock()
    monkeypatch.setattr(cli, "_build_default_service", service_factory)
    args = cli._build_parser().parse_args(
        [
            "--market",
            "crypto",
            "--symbols",
            symbols,
            "--partition",
            "upbit_krw",
        ]
    )

    with pytest.raises(ValueError):
        await cli._amain(args)

    service_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_mode_continues_failures_rolls_back_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import scripts.backfill_daily_candles as cli

    targets = [
        SyncTarget(market=MarketKey.US, symbol="AAPL", partition="NASD"),
        SyncTarget(market=MarketKey.US, symbol="IBM", partition="NYSE"),
        SyncTarget(market=MarketKey.US, symbol="MSFT", partition="NASD"),
    ]
    success = SimpleNamespace(
        rows_upserted=1,
        fallback_used=False,
        skipped_reason=None,
    )
    fallback_success = SimpleNamespace(
        rows_upserted=1,
        fallback_used=True,
        skipped_reason=None,
    )
    service = SimpleNamespace(
        resolve_backfill_targets=AsyncMock(return_value=targets),
        sync_one=AsyncMock(
            side_effect=[fallback_success, RuntimeError("provider down"), success]
        ),
        sync_benchmark=AsyncMock(),
        rollback=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(cli, "_build_default_service", AsyncMock(return_value=service))
    args = cli._build_parser().parse_args(["--market", "us", "--all"])

    with caplog.at_level(logging.INFO):
        exit_code = await cli._amain(args)

    assert exit_code == 1
    assert service.sync_one.await_count == 3
    service.rollback.assert_awaited_once()
    assert "targets_total=3 succeeded=2 failed=1 fallback=1" in caplog.text
    assert "failed_symbols=['IBM']" in caplog.text
    service.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_mode_remains_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.backfill_daily_candles as cli

    service = SimpleNamespace(
        sync_one=AsyncMock(side_effect=RuntimeError("first failed")),
        sync_benchmark=AsyncMock(),
        rollback=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(cli, "_build_default_service", AsyncMock(return_value=service))
    args = cli._build_parser().parse_args(
        ["--market", "us", "--symbols", "AAPL,MSFT", "--partition", "NASDAQ"]
    )

    with pytest.raises(RuntimeError, match="first failed"):
        await cli._amain(args)

    assert service.sync_one.await_count == 1
    assert service.sync_one.await_args.kwargs["target"] == SyncTarget(
        market=MarketKey.US,
        symbol="AAPL",
        partition="NASD",
    )
    service.rollback.assert_awaited_once()
    service.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_all_dry_run_only_resolves_database_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.backfill_daily_candles as cli

    targets = [SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX")]
    service = SimpleNamespace(
        resolve_backfill_targets=AsyncMock(return_value=targets),
        sync_one=AsyncMock(),
        sync_benchmark=AsyncMock(),
        rollback=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(cli, "_build_default_service", AsyncMock(return_value=service))
    args = cli._build_parser().parse_args(
        ["--market", "kr", "--all", "--dry-run", "--include-benchmark"]
    )

    assert await cli._amain(args) == 0

    service.resolve_backfill_targets.assert_awaited_once_with(
        market="kr",
        resume_after=None,
        limit=None,
    )
    service.sync_one.assert_not_awaited()
    service.sync_benchmark.assert_not_awaited()
    service.rollback.assert_not_awaited()
    service.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_top_market_cap_uses_distinct_deterministic_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.backfill_daily_candles as cli

    targets = [
        SyncTarget(market=MarketKey.US, symbol="MSFT", partition="NASD"),
        SyncTarget(market=MarketKey.US, symbol="IBM", partition="NYSE"),
    ]
    success = SimpleNamespace(
        rows_upserted=1,
        fallback_used=False,
        skipped_reason=None,
    )
    service = SimpleNamespace(
        resolve_backfill_targets=AsyncMock(),
        resolve_top_market_cap_targets=AsyncMock(return_value=targets),
        sync_one=AsyncMock(side_effect=[RuntimeError("provider down"), success]),
        sync_benchmark=AsyncMock(),
        rollback=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(cli, "_build_default_service", AsyncMock(return_value=service))
    args = cli._build_parser().parse_args(["--market", "us", "--top-market-cap", "2"])

    assert await cli._amain(args) == 1

    service.resolve_top_market_cap_targets.assert_awaited_once_with(
        market="us",
        count=2,
    )
    service.resolve_backfill_targets.assert_not_awaited()
    assert [
        call.kwargs["target"] for call in service.sync_one.await_args_list
    ] == targets
    service.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_include_benchmark_is_an_explicit_target_outside_all_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.backfill_daily_candles as cli

    candidate = SyncTarget(
        market=MarketKey.KR,
        symbol="005930",
        partition="KRX",
    )
    success = SimpleNamespace(
        rows_upserted=1,
        fallback_used=False,
        skipped_reason=None,
    )
    service = SimpleNamespace(
        resolve_backfill_targets=AsyncMock(return_value=[candidate]),
        sync_one=AsyncMock(return_value=success),
        sync_benchmark=AsyncMock(return_value=success),
        rollback=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(cli, "_build_default_service", AsyncMock(return_value=service))
    args = cli._build_parser().parse_args(
        [
            "--market",
            "kr",
            "--all",
            "--limit",
            "1",
            "--include-benchmark",
        ]
    )

    assert await cli._amain(args) == 0

    service.sync_one.assert_awaited_once_with(
        target=candidate,
        horizon_bars=400,
    )
    service.sync_benchmark.assert_awaited_once_with(
        market=MarketKey.KR,
        horizon_bars=400,
    )
