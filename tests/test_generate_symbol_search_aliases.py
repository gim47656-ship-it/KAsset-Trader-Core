from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from scripts.generate_symbol_search_aliases import (
    generate,
    normalize_response,
    parse_args,
)


def stock(symbol: str, name: str = "삼성전자") -> SimpleNamespace:
    return SimpleNamespace(
        market="KRX",
        symbol=symbol,
        name=name,
        name_en=None,
        security_type="COMMON_STOCK",
    )


def test_response_filters_unknown_duplicate_short_long_and_ambiguous_aliases() -> None:
    rows = [
        stock("005930"),
        stock("000660", "SK하이닉스"),
        stock("111111", "테스트 종목"),
    ]
    result = normalize_response(
        {
            "items": [
                {
                    "symbol": "005930",
                    "aliases": ["삼 전", "삼성전자", "005930", "x", "a" * 31, "공통"],
                },
                {"symbol": "000660", "aliases": ["하 닉", "공통"]},
                {"symbol": "111111", "aliases": ["공통"]},
                {"symbol": "UNKNOWN", "aliases": ["미요청"]},
            ]
        },
        rows,
    )
    assert [(item["symbol"], item["alias"]) for item in result] == [
        ("005930", "삼전"),
        ("000660", "하닉"),
    ]


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"items": "invalid"},
        {"items": [{"symbol": "005930", "aliases": [1]}]},
        {"items": [{"symbol": "005930", "aliases": ["aa"] * 7}]},
    ],
)
def test_malformed_response_rejects_batch(response: object) -> None:
    with pytest.raises(ValueError):
        normalize_response(response, [stock("005930")])


def test_dry_run_skips_failed_batch_without_writing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.generate_symbol_search_aliases as module

    async def targets(db: object, args: object) -> tuple[list[SimpleNamespace], str]:
        return [stock("005930"), stock("000660", "SK하이닉스")], "symbol 순"

    monkeypatch.setattr(module, "select_targets", targets)

    class FakeClient:
        calls = 0

        async def request_json(self, **kwargs: object) -> dict[str, object]:
            self.calls += 1
            assert kwargs["reasoning_effort"] == "low"
            if self.calls == 1:
                raise ValueError("invalid JSON")
            return {"items": [{"symbol": "000660", "aliases": ["하닉"]}]}

    class NoWrites:
        async def commit(self) -> None:
            raise AssertionError("dry-run wrote to DB")

        async def scalars(self, statement: object) -> None:
            raise AssertionError("dry-run wrote to DB")

        async def execute(self, statement: object) -> None:
            raise AssertionError("dry-run wrote to DB")

    summary = asyncio.run(
        generate(NoWrites(), FakeClient(), parse_args(["--batch-size", "1"]))
    )
    assert (summary.targets, summary.calls, summary.failures, summary.saved) == (
        2,
        2,
        1,
        0,
    )
    assert summary.sample == [("KRX", "000660", "하닉")]
    assert "저장 0" in capsys.readouterr().out
