from __future__ import annotations

import argparse
import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import sync_kr_lifecycle_actions as cli


def test_parse_args_is_dry_run_first_and_bounded() -> None:
    args = cli.parse_args(
        [
            "--symbol",
            "005930",
            "--from-date",
            "2026-01-01",
            "--to-date",
            "2026-02-28",
        ]
    )

    assert args.commit is False
    assert args.limit == 20
    assert args.symbol == ["005930"]
    assert args.from_date == date(2026, 1, 1)
    assert args.to_date == date(2026, 2, 28)


class _SelectionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


class _Client:
    closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_run_prints_structured_scope_rows_failures_and_history_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import app.core.db as db_module
    import app.services.brokers.kis.client as client_module
    import app.services.kr_lifecycle_action_service as service_module

    captured: dict[str, Any] = {}

    def session_factory() -> _SelectionContext:
        return _SelectionContext()

    async def select_symbols(
        db: object,
        *,
        explicit_symbols: list[str],
        limit: int,
        resume_after: str | None,
    ) -> list[str]:
        captured["selection"] = {
            "explicit_symbols": explicit_symbols,
            "limit": limit,
            "resume_after": resume_after,
        }
        return ["005930"]

    async def run_sync(**kwargs: Any) -> SimpleNamespace:
        captured["sync"] = kwargs
        payload = {
            "mode": "dry-run",
            "symbols": ["005930"],
            "windows_attempted": 2,
            "rows_prepared": 3,
            "failures": [],
            "historical_delisted_enumeration_available": False,
        }
        return SimpleNamespace(failures=[], to_dict=lambda: payload)

    monkeypatch.setattr(db_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(client_module, "KISClient", _Client)
    monkeypatch.setattr(service_module, "select_kr_symbols", select_symbols)
    monkeypatch.setattr(service_module, "run_kr_lifecycle_action_sync", run_sync)
    args = argparse.Namespace(
        symbol=["005930"],
        from_date=date(2026, 1, 1),
        to_date=date(2026, 2, 28),
        limit=10,
        resume_after="000001",
        commit=False,
    )

    exit_code = await cli.run(args)

    assert exit_code == 0
    assert captured["selection"] == {
        "explicit_symbols": ["005930"],
        "limit": 10,
        "resume_after": "000001",
    }
    assert captured["sync"]["commit"] is False
    report = json.loads(capsys.readouterr().out)
    assert report["symbols"] == ["005930"]
    assert report["windows_attempted"] == 2
    assert report["rows_prepared"] == 3
    assert report["failures"] == []
    assert report["historical_delisted_enumeration_available"] is False
