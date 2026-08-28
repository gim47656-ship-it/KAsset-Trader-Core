from __future__ import annotations

import pytest

from app.services.nhplug_symbol_master_service import (
    SymbolMasterRecord,
    parse_domestic_master,
    parse_global_master,
)


def _fixed_record(size: int, fields: list[tuple[int, int, str]]) -> bytes:
    record = bytearray(b" " * size)
    for offset, length, value in fields:
        encoded = value.encode("cp949")
        assert len(encoded) <= length
        record[offset : offset + length] = encoded.ljust(length, b" ")
    record[-1] = 0x0A
    return bytes(record)


def _domestic_record(
    *,
    symbol: str,
    name: str,
    name_en: str,
    market_code: str = "1",
    security_group: str = "0",
    managed: str = "N",
    suspended: str = "N",
    liquidation: str = "N",
) -> bytes:
    return _fixed_record(
        237,
        [
            (0, 6, symbol),
            (6, 1, market_code),
            (7, 41, name),
            (48, 41, name_en),
            (160, 1, managed),
            (161, 1, suspended),
            (165, 1, security_group),
            (189, 1, liquidation),
        ],
    )


def _global_record(
    *,
    symbol: str,
    name: str,
    name_en: str,
    nation: str = "USA",
    issue_type: str = "01",
    tradable: str = "1",
) -> bytes:
    return _fixed_record(
        164,
        [
            (15, 40, name),
            (55, 40, name_en),
            (95, 3, nation),
            (98, 12, symbol),
            (125, 2, issue_type),
            (137, 1, tradable),
        ],
    )


def test_parse_domestic_master_uses_cp949_byte_offsets_and_filters() -> None:
    payload = b"".join(
        [
            _domestic_record(
                symbol="005930",
                name="*삼성전자",
                name_en="Samsung Electronics",
            ),
            _domestic_record(
                symbol="069500",
                name="KODEX 200",
                name_en="KODEX 200 ETF",
                security_group="8",
            ),
            _domestic_record(
                symbol="005935",
                name="삼성전자우",
                name_en="Samsung Electronics Pref",
            ),
            _domestic_record(
                symbol="123450",
                name="거래정지 종목",
                name_en="Suspended",
                suspended="Y",
            ),
        ]
    )

    assert parse_domestic_master(payload) == [
        SymbolMasterRecord(
            market="KRX",
            symbol="005930",
            name="삼성전자",
            name_en="Samsung Electronics",
            security_type="COMMON_STOCK",
        ),
        SymbolMasterRecord(
            market="KRX",
            symbol="069500",
            name="KODEX 200",
            name_en="KODEX 200 ETF",
            security_type="ETF",
        ),
    ]


def test_parse_global_master_keeps_only_tradable_us_stocks_and_etfs() -> None:
    payload = b"".join(
        [
            _global_record(
                symbol="BRK/B",
                name="버크셔 해서웨이 B",
                name_en="Berkshire Hathaway B",
            ),
            _global_record(
                symbol="SPY",
                name="SPDR S&P 500 ETF",
                name_en="SPDR S&P 500 ETF",
                issue_type="12",
            ),
            _global_record(
                symbol="7203",
                name="도요타자동차",
                name_en="Toyota Motor",
                nation="JPN",
            ),
            _global_record(
                symbol="LOCKED",
                name="거래불가",
                name_en="Not Tradable",
                tradable="0",
            ),
        ]
    )

    assert parse_global_master(payload) == [
        SymbolMasterRecord(
            market="US",
            symbol="BRK.B",
            name="버크셔 해서웨이 B",
            name_en="Berkshire Hathaway B",
            security_type="COMMON_STOCK",
        ),
        SymbolMasterRecord(
            market="US",
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            name_en="SPDR S&P 500 ETF",
            security_type="ETF",
        ),
    ]


def test_fixed_width_parser_rejects_misaligned_or_unterminated_payload() -> None:
    with pytest.raises(ValueError, match="record size mismatch"):
        parse_domestic_master(b"short")

    record = bytearray(_global_record(symbol="AAPL", name="애플", name_en="Apple Inc"))
    record[-1] = 0x20
    with pytest.raises(ValueError, match="record terminator mismatch"):
        parse_global_master(bytes(record))
