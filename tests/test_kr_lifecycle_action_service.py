from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services import kr_lifecycle_action_service as service

_OBSERVED_AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_RUN_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


class _CompleteClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def search_stock_info(
        self, pdno: str, *, prdt_type_cd: str = "300"
    ) -> list[dict[str, Any]]:
        self.calls.append(("lifecycle", pdno))
        return [
            {
                "pdno": pdno,
                "std_pdno": f"KR{pdno}0000",
                "scts_mket_lstg_dt": "20200102",
                "lstg_abol_dt": "",
            }
        ]

    async def ksdinfo_rev_split(
        self,
        sht_cd: str,
        from_date: date,
        to_date: date,
        *,
        market_gb: str = "0",
    ) -> list[dict[str, Any]]:
        self.calls.append(("rev-split", sht_cd))
        return []

    async def ksdinfo_paidin_capin(
        self,
        sht_cd: str,
        from_date: date,
        to_date: date,
        *,
        gb1: str = "2",
    ) -> list[dict[str, Any]]:
        self.calls.append(("paidin-capin", sht_cd))
        return []

    async def ksdinfo_bonus_issue(
        self, sht_cd: str, from_date: date, to_date: date
    ) -> list[dict[str, Any]]:
        self.calls.append(("bonus-issue", sht_cd))
        return []

    async def ksdinfo_dividend(
        self,
        sht_cd: str,
        from_date: date,
        to_date: date,
        *,
        gb1: str = "0",
        high_gb: str = "",
    ) -> list[dict[str, Any]]:
        self.calls.append(("dividend", sht_cd))
        return []


class _FirstSymbolWindowFailureClient(_CompleteClient):
    async def ksdinfo_rev_split(
        self,
        sht_cd: str,
        from_date: date,
        to_date: date,
        *,
        market_gb: str = "0",
    ) -> list[dict[str, Any]]:
        self.calls.append(("rev-split", sht_cd))
        if sht_cd == "000001":
            raise RuntimeError("provider unavailable")
        return []


def _forbidden_session_factory() -> Any:
    pytest.fail("dry-run must not open a write session")


def test_monthly_windows_cover_range_without_overlap() -> None:
    assert service.monthly_windows(date(2026, 1, 30), date(2026, 3, 2)) == [
        service.MonthlyWindow(date(2026, 1, 30), date(2026, 1, 31)),
        service.MonthlyWindow(date(2026, 2, 1), date(2026, 2, 28)),
        service.MonthlyWindow(date(2026, 3, 1), date(2026, 3, 2)),
    ]


def test_lifecycle_preserves_std_pdno_without_guessing_isin_or_status() -> None:
    provider_row = {
        "pdno": "005930",
        "std_pdno": "KR7005930003",
        "scts_mket_lstg_dt": "19750611",
        "lstg_abol_dt": "",
        "tr_stop_yn": "N",
    }

    evidence = service.build_lifecycle_evidence(
        symbol="005930",
        row=provider_row,
        observed_at=_OBSERVED_AT,
        fetch_run_id=_RUN_ID,
    )

    assert evidence["raw_provider_fields"] == provider_row
    assert evidence["std_pdno"] == "KR7005930003"
    assert evidence["isin"] is None
    assert evidence["listing_status"] is None
    assert evidence["list_date"] == date(1975, 6, 11)
    assert evidence["delist_date"] is None


def test_exact_provider_fields_are_preserved_and_dates_are_only_direct() -> None:
    provider_row = {
        "sht_cd": "005930",
        "record_date": "20260102",
        "inter_bf_face_amt": "5000",
        "inter_af_face_amt": "1000",
        "td_stop_dt": "20260105",
        "list_dt": "20260112",
        "opaque": {"provider": "value"},
    }

    evidence = service.build_action_evidence(
        symbol="005930",
        row=provider_row,
        spec=service._ACTION_SPECS[0],
        window=service.MonthlyWindow(date(2026, 1, 1), date(2026, 1, 31)),
        observed_at=_OBSERVED_AT,
        fetch_run_id=_RUN_ID,
    )

    assert evidence["raw_provider_fields"] == provider_row
    assert evidence["record_date"] == date(2026, 1, 2)
    assert evidence["list_date"] == date(2026, 1, 12)
    assert evidence["effective_date"] is None
    assert evidence["observed_at"] == _OBSERVED_AT
    assert evidence["provider_endpoint"].endswith("/rev-split")
    assert evidence["provider_tr_id"] == "HHKDB669105C0"


def test_face_value_direction_does_not_infer_split_or_consolidation_type() -> None:
    evidence = service.build_action_evidence(
        symbol="005930",
        row={
            "sht_cd": "005930",
            "record_date": "20260102",
            "inter_bf_face_amt": "5000",
            "inter_af_face_amt": "1000",
        },
        spec=service._ACTION_SPECS[0],
        window=service.MonthlyWindow(date(2026, 1, 1), date(2026, 1, 31)),
        observed_at=_OBSERVED_AT,
        fetch_run_id=_RUN_ID,
    )

    assert evidence["evidence_kind"] == "face_value_change"
    assert evidence["provider_action_type"] is None


def test_duplicate_event_is_idempotent_and_corrected_event_gets_new_key() -> None:
    original = {
        "sht_cd": "005930",
        "record_date": "20260102",
        "fix_rate": "0.10",
    }
    corrected = {**original, "fix_rate": "0.20"}
    kwargs = {
        "symbol": "005930",
        "spec": service._ACTION_SPECS[1],
        "window": service.MonthlyWindow(date(2026, 1, 1), date(2026, 1, 31)),
        "observed_at": _OBSERVED_AT,
        "fetch_run_id": _RUN_ID,
    }

    first = service.build_action_evidence(row=original, **kwargs)
    duplicate = service.build_action_evidence(
        row=dict(reversed(list(original.items()))),
        **{**kwargs, "observed_at": datetime(2026, 8, 31, tzinfo=UTC)},
    )
    correction = service.build_action_evidence(row=corrected, **kwargs)

    assert first["canonical_raw_hash"] == duplicate["canonical_raw_hash"]
    assert first["idempotency_key"] == duplicate["idempotency_key"]
    assert correction["canonical_raw_hash"] != first["canonical_raw_hash"]
    assert correction["idempotency_key"] != first["idempotency_key"]


def test_zero_event_success_and_failure_are_distinct_coverage_evidence() -> None:
    window = service.MonthlyWindow(date(2026, 1, 1), date(2026, 1, 31))
    success = service.build_fetch_coverage(
        symbol="005930",
        spec=service._ACTION_SPECS[0],
        window=window,
        fetch_run_id=_RUN_ID,
        status="success",
        row_count=0,
        page_count=1,
        last_cursor=None,
        completed_at=_OBSERVED_AT,
    )
    failure = service.build_fetch_coverage(
        symbol="005930",
        spec=service._ACTION_SPECS[0],
        window=window,
        fetch_run_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        status="failed",
        row_count=0,
        page_count=0,
        last_cursor=None,
        completed_at=_OBSERVED_AT,
        error=RuntimeError("provider failed"),
    )

    assert success["status"] == "success"
    assert success["row_count"] == 0
    assert success["error_class"] is None
    assert failure["status"] == "failed"
    assert failure["error_class"] == "RuntimeError"
    assert failure["idempotency_key"] != success["idempotency_key"]


class _CoverageResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return self._rows


class _CoverageDB:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    async def execute(self, statement: object) -> _CoverageResult:
        return _CoverageResult(self.rows)


@pytest.mark.asyncio
async def test_clear_requires_success_coverage_for_every_endpoint_and_month() -> None:
    windows = service.monthly_windows(date(2026, 1, 31), date(2026, 2, 1))
    complete_rows = [
        (
            spec.endpoint,
            spec.tr_id,
            spec.evidence_kind,
            window.from_date,
            window.to_date,
        )
        for spec in service._ACTION_SPECS
        for window in windows
    ]

    assert await service.has_complete_corporate_action_coverage(
        _CoverageDB(complete_rows),  # type: ignore[arg-type]
        symbol="005930",
        from_date=date(2026, 1, 31),
        to_date=date(2026, 2, 1),
    )
    assert not await service.has_complete_corporate_action_coverage(
        _CoverageDB(complete_rows[:-1]),  # type: ignore[arg-type]
        symbol="005930",
        from_date=date(2026, 1, 31),
        to_date=date(2026, 2, 1),
    )


@pytest.mark.asyncio
async def test_dry_run_reads_network_but_opens_no_write_session() -> None:
    client = _CompleteClient()

    report = await service.run_kr_lifecycle_action_sync(
        client=client,
        session_factory=_forbidden_session_factory,
        symbols=["005930"],
        from_date=date(2026, 1, 31),
        to_date=date(2026, 2, 1),
        commit=False,
        observed_at=_OBSERVED_AT,
        fetch_run_id=_RUN_ID,
    )

    assert report.mode == "dry-run"
    assert report.lifecycle_requests == 1
    assert report.windows_attempted == 2
    assert report.windows_succeeded == 2
    assert report.rows_prepared == 1
    assert report.coverage_rows_prepared == 8
    assert report.coverage_rows_inserted == 0
    assert report.rows_inserted == 0
    assert len(client.calls) == 1 + (2 * 4)
    assert report.historical_delisted_enumeration_available is False


@pytest.mark.asyncio
async def test_symbol_window_failure_is_isolated_and_structured() -> None:
    client = _FirstSymbolWindowFailureClient()

    report = await service.run_kr_lifecycle_action_sync(
        client=client,
        session_factory=_forbidden_session_factory,
        symbols=["000001", "000002"],
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        commit=False,
        observed_at=_OBSERVED_AT,
        fetch_run_id=_RUN_ID,
    )

    assert report.symbols_failed == 1
    assert report.symbols_succeeded == 1
    assert report.windows_failed == 1
    assert report.windows_succeeded == 1
    assert report.failures == [
        {
            "symbol": "000001",
            "stage": "corporate_action_window",
            "from_date": "2026-01-01",
            "to_date": "2026-01-31",
            "provider_endpoint": "/uapi/domestic-stock/v1/ksdinfo/rev-split",
            "provider_tr_id": "HHKDB669105C0",
            "error_type": "RuntimeError",
            "message": "provider unavailable",
        }
    ]
    assert ("dividend", "000002") in client.calls


@pytest.mark.asyncio
async def test_commit_persists_failed_window_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def persist_lifecycle(*_args: Any, **_kwargs: Any) -> int:
        return 1

    async def persist_window(
        _session_factory: object,
        event_rows: list[dict[str, Any]],
        coverage_rows: list[dict[str, Any]],
    ) -> tuple[int, int]:
        captured.extend(coverage_rows)
        return len(event_rows), len(coverage_rows)

    monkeypatch.setattr(service, "_persist_lifecycle", persist_lifecycle)
    monkeypatch.setattr(service, "_persist_action_window", persist_window)
    report = await service.run_kr_lifecycle_action_sync(
        client=_FirstSymbolWindowFailureClient(),
        session_factory=_forbidden_session_factory,
        symbols=["000001"],
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        commit=True,
        observed_at=_OBSERVED_AT,
        fetch_run_id=_RUN_ID,
    )

    assert report.windows_failed == 1
    assert report.coverage_rows_inserted == 1
    assert captured[0]["status"] == "failed"
    assert captured[0]["error_class"] == "RuntimeError"


@pytest.mark.asyncio
async def test_empty_lifecycle_response_is_not_converted_to_delisting() -> None:
    client = _CompleteClient()

    async def empty_stock_info(
        pdno: str, *, prdt_type_cd: str = "300"
    ) -> list[dict[str, Any]]:
        return []

    client.search_stock_info = empty_stock_info  # type: ignore[method-assign]
    report = await service.run_kr_lifecycle_action_sync(
        client=client,
        session_factory=_forbidden_session_factory,
        symbols=["005930"],
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        commit=False,
        observed_at=_OBSERVED_AT,
        fetch_run_id=_RUN_ID,
    )

    assert report.lifecycle_rows == 0
    assert not report.failures


@pytest.mark.integration
@pytest.mark.asyncio
async def test_action_upsert_is_idempotent_and_retains_correction(
    db_session: AsyncSession,
) -> None:
    if await db_session.get(KRSymbolUniverse, "005930") is None:
        db_session.add(
            KRSymbolUniverse(
                symbol="005930",
                name="삼성전자",
                exchange="KOSPI",
                nxt_eligible=False,
                is_active=True,
            )
        )
        await db_session.flush()
    nonce = str(uuid.uuid4())
    kwargs = {
        "symbol": "005930",
        "spec": service._ACTION_SPECS[1],
        "window": service.MonthlyWindow(date(2026, 1, 1), date(2026, 1, 31)),
        "observed_at": _OBSERVED_AT,
        "fetch_run_id": _RUN_ID,
    }
    original = service.build_action_evidence(
        row={
            "sht_cd": "005930",
            "record_date": "20260102",
            "fix_rate": "0.10",
            "test_nonce": nonce,
        },
        **kwargs,
    )
    correction = service.build_action_evidence(
        row={
            **original["raw_provider_fields"],
            "fix_rate": "0.20",
        },
        **kwargs,
    )

    assert await service.upsert_action_evidence(db_session, [original]) == 1
    assert await service.upsert_action_evidence(db_session, [original]) == 0
    assert await service.upsert_action_evidence(db_session, [correction]) == 1
