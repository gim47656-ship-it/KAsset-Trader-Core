from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services import kr_lifecycle_action_service as service

_OBSERVED_AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_RUN_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


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


def test_direct_provider_delist_date_is_promoted_with_64_char_status() -> None:
    status = "D" * 64
    evidence = service.build_lifecycle_evidence(
        symbol="005930",
        row={
            "pdno": "005930",
            "listing_status": status,
            "lstg_abol_dt": "20260829",
        },
        observed_at=_OBSERVED_AT,
        fetch_run_id=_RUN_ID,
    )

    metadata = service._merged_lifecycle_metadata([evidence])

    assert metadata["listing_status"] == status
    assert metadata["delist_date"] == date(2026, 8, 29)


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
