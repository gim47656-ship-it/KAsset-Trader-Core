from __future__ import annotations

from types import SimpleNamespace

from scripts.sync_us_symbol_master import build_rows


def universe(
    symbol: str, security_type: str | None, name_kr: str = "", name_en: str = ""
):
    return SimpleNamespace(
        symbol=symbol, security_type=security_type, name_kr=name_kr, name_en=name_en
    )


def test_build_rows_maps_types_and_skips_existing_unknown_and_nameless() -> None:
    rows = build_rows(
        [
            universe("SKHY", "DEPOSITARY_RECEIPT", "SK하이닉스(ADR)", "SK HYNIX ADS"),
            universe("NEWCO", "STOCK", "", "New Co"),
            universe("QQQ", "ETF", "인베스코 QQQ"),
            universe("AAPL", "STOCK", "애플"),
            universe("ETNX", "ETN", "어떤 ETN"),
            universe("BLANK", None, "이름만"),
            universe("NONAME", "STOCK"),
        ],
        existing={"AAPL"},
    )
    assert [(r["symbol"], r["name"], r["security_type"]) for r in rows] == [
        ("SKHY", "SK하이닉스(ADR)", "DEPOSITARY_RECEIPT"),
        ("NEWCO", "New Co", "COMMON_STOCK"),
        ("QQQ", "인베스코 QQQ", "ETF"),
    ]
