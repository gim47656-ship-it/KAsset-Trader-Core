from __future__ import annotations

import json

import pytest

import scripts.quote_parity_shadow_probe as probe
from scripts.quote_parity_shadow_probe import (
    exit_code_for,
    load_symbols_file,
    main,
    parse_args,
)

pytestmark = pytest.mark.unit


def test_live_provider_flags_are_physically_removed() -> None:
    args = parse_args(["--user-id", "1"])

    assert args.limit == 200
    assert not hasattr(args, "confirm_live")
    assert not hasattr(args, "us_kis_live_last")
    assert not hasattr(probe, "_build_live_clients")


def test_exit_code_map_keeps_disabled_rule_blocked() -> None:
    assert exit_code_for("go") == 0
    assert exit_code_for("no_go") == 2
    assert exit_code_for("blocked") == 2
    assert exit_code_for("unknown") == 1


def test_symbols_file_splits_markets_and_rejects_secrets(tmp_path) -> None:
    path = tmp_path / "symbols.json"
    path.write_text(
        json.dumps(
            [
                {"market": "US", "symbol": "BRK-B"},
                {"market": "KR", "symbol": "005930"},
            ]
        ),
        encoding="utf-8",
    )
    assert load_symbols_file(path) == (["005930"], ["BRK.B"])

    bad = tmp_path / "bad.csv"
    bad.write_text("symbol,authorization\nAAPL,Bearer abc123\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_symbols_file(bad)


@pytest.mark.asyncio
async def test_probe_returns_disabled_without_provider_construction(
    tmp_path, capsys
) -> None:
    path = tmp_path / "symbols.json"
    path.write_text(
        json.dumps([{"market": "US", "symbol": "AAPL"}]),
        encoding="utf-8",
    )

    result = await main(["--symbols-file", str(path)])
    payload = json.loads(capsys.readouterr().out)

    assert result == 2
    assert payload["mode"] == "disabled"
    assert payload["status"] == "disabled"
    assert payload["go_no_go"]["decision"] == "blocked"
    assert "weakened to Toss-only" in payload["reason"]
