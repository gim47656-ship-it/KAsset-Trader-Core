from __future__ import annotations

from types import SimpleNamespace

from scripts.sync_symbol_master import build_kr_rows, build_us_rows


def us(symbol, security_type, name_kr="", name_en=""):
    return SimpleNamespace(
        symbol=symbol, security_type=security_type, name_kr=name_kr, name_en=name_en
    )


def kr(symbol, security_type, is_common_share, name="종목"):
    return SimpleNamespace(
        symbol=symbol,
        security_type=security_type,
        is_common_share=is_common_share,
        name=name,
    )


def test_us_rows_map_types_and_skip_existing_unknown_and_nameless() -> None:
    rows = build_us_rows(
        [
            us("SKHY", "DEPOSITARY_RECEIPT", "SK하이닉스(ADR)", "SK HYNIX ADS"),
            us("NEWCO", "STOCK", "", "New Co"),
            us("QQQ", "ETF", "인베스코 QQQ"),
            us("AAPL", "STOCK", "애플"),
            us("ETNX", "ETN", "어떤 ETN"),
            us("BLANK", None, "이름만"),
            us("NONAME", "STOCK"),
        ],
        existing={"AAPL"},
    )
    assert [(r["symbol"], r["name"], r["security_type"]) for r in rows] == [
        ("SKHY", "SK하이닉스(ADR)", "DEPOSITARY_RECEIPT"),
        ("NEWCO", "New Co", "COMMON_STOCK"),
        ("QQQ", "인베스코 QQQ", "ETF"),
    ]


def test_kr_rows_keep_common_stock_and_etf_but_not_preferred() -> None:
    rows = build_kr_rows(
        [
            kr("111110", "STOCK", True, "신규상장"),
            kr("000105", "STOCK", False, "유한양행우"),
            kr("222220", "ETF", True, "KODEX 신규"),
            kr("333330", "REIT", True),
            kr("005930", "STOCK", True, "삼성전자"),
        ],
        existing={"005930"},
    )
    assert [(r["market"], r["symbol"], r["security_type"]) for r in rows] == [
        ("KRX", "111110", "COMMON_STOCK"),
        ("KRX", "222220", "ETF"),
    ]
