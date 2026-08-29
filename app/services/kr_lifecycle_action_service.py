"""Known-symbol KIS lifecycle and corporate-action evidence collection.

The service never enumerates delisted instruments. It operates only on symbols
explicitly present in ``kr_symbol_universe`` or on the current active/common
subset selected from that table.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from calendar import monthrange
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_lifecycle_actions import (
    KAssetCorporateActionFetchCoverage,
    KRCorporateActionEvidence,
    KRStockLifecycleObservation,
)
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.brokers.kis.corporate_actions import (
    BONUS_ISSUE_ENDPOINT,
    BONUS_ISSUE_TR,
    DIVIDEND_ENDPOINT,
    DIVIDEND_TR,
    PAIDIN_CAPIN_ENDPOINT,
    PAIDIN_CAPIN_TR,
    REV_SPLIT_ENDPOINT,
    REV_SPLIT_TR,
    SEARCH_STOCK_INFO_ENDPOINT,
    SEARCH_STOCK_INFO_TR,
)

_SOURCE = "kis_openapi"
_PROVIDER = "KIS"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{6}$")


class KRLifecycleActionError(RuntimeError):
    pass


class KRLifecycleActionClient(Protocol):
    async def search_stock_info(
        self, pdno: str, *, prdt_type_cd: str = "300"
    ) -> list[dict[str, Any]]: ...

    async def ksdinfo_rev_split(
        self,
        sht_cd: str,
        from_date: date | str,
        to_date: date | str,
        *,
        market_gb: str = "0",
    ) -> list[dict[str, Any]]: ...

    async def ksdinfo_paidin_capin(
        self,
        sht_cd: str,
        from_date: date | str,
        to_date: date | str,
        *,
        gb1: str = "2",
    ) -> list[dict[str, Any]]: ...

    async def ksdinfo_bonus_issue(
        self, sht_cd: str, from_date: date | str, to_date: date | str
    ) -> list[dict[str, Any]]: ...

    async def ksdinfo_dividend(
        self,
        sht_cd: str,
        from_date: date | str,
        to_date: date | str,
        *,
        gb1: str = "0",
        high_gb: str = "",
    ) -> list[dict[str, Any]]: ...


SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class MonthlyWindow:
    from_date: date
    to_date: date


@dataclass(frozen=True)
class _ActionSpec:
    method_name: str
    evidence_kind: str
    endpoint: str
    tr_id: str
    list_date_keys: tuple[str, ...]
    payment_date_keys: tuple[str, ...] = ()
    action_type_keys: tuple[str, ...] = ("action_type", "event_type", "ca_type")


_ACTION_SPECS = (
    _ActionSpec(
        method_name="ksdinfo_rev_split",
        evidence_kind="face_value_change",
        endpoint=REV_SPLIT_ENDPOINT,
        tr_id=REV_SPLIT_TR,
        list_date_keys=("list_dt",),
    ),
    _ActionSpec(
        method_name="ksdinfo_paidin_capin",
        evidence_kind="paid_in_capital_increase",
        endpoint=PAIDIN_CAPIN_ENDPOINT,
        tr_id=PAIDIN_CAPIN_TR,
        list_date_keys=("list_date",),
    ),
    _ActionSpec(
        method_name="ksdinfo_bonus_issue",
        evidence_kind="bonus_issue",
        endpoint=BONUS_ISSUE_ENDPOINT,
        tr_id=BONUS_ISSUE_TR,
        list_date_keys=("list_date",),
        payment_date_keys=("odd_pay_dt",),
    ),
    _ActionSpec(
        method_name="ksdinfo_dividend",
        evidence_kind="dividend",
        endpoint=DIVIDEND_ENDPOINT,
        tr_id=DIVIDEND_TR,
        list_date_keys=(),
        payment_date_keys=("divi_pay_dt", "stk_div_pay_dt", "odd_pay_dt"),
        action_type_keys=("action_type", "event_type", "ca_type", "divi_kind"),
    ),
)


@dataclass
class KRLifecycleActionSyncReport:
    mode: str
    fetch_run_id: str
    symbols: list[str]
    date_from: str
    date_to: str
    symbols_succeeded: int = 0
    symbols_failed: int = 0
    lifecycle_requests: int = 0
    windows_attempted: int = 0
    windows_succeeded: int = 0
    windows_failed: int = 0
    lifecycle_rows: int = 0
    corporate_action_rows: int = 0
    rows_prepared: int = 0
    rows_inserted: int = 0
    duplicate_rows: int = 0
    coverage_rows_prepared: int = 0
    coverage_rows_inserted: int = 0
    coverage_duplicate_rows: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    historical_delisted_enumeration_available: bool = False
    historical_delisted_enumeration_note: str = (
        "KIS point lookups for known symbols do not enumerate all historical "
        "delisted symbols."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_kr_symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError("KR symbol must be exactly 6 alphanumeric characters")
    return symbol


def monthly_windows(from_date: date, to_date: date) -> list[MonthlyWindow]:
    if from_date > to_date:
        raise ValueError("from_date must be on or before to_date")
    windows: list[MonthlyWindow] = []
    cursor = from_date
    while cursor <= to_date:
        month_end = date(
            cursor.year,
            cursor.month,
            monthrange(cursor.year, cursor.month)[1],
        )
        window_end = min(month_end, to_date)
        windows.append(MonthlyWindow(from_date=cursor, to_date=window_end))
        cursor = window_end + timedelta(days=1)
    return windows


async def select_kr_symbols(
    db: AsyncSession,
    *,
    explicit_symbols: Sequence[str] = (),
    limit: int | None = None,
    resume_after: str | None = None,
) -> list[str]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    resume = normalize_kr_symbol(resume_after) if resume_after else None

    if explicit_symbols:
        requested = sorted({normalize_kr_symbol(symbol) for symbol in explicit_symbols})
        result = await db.execute(
            select(KRSymbolUniverse.symbol).where(
                KRSymbolUniverse.symbol.in_(requested)
            )
        )
        known = set(result.scalars().all())
        missing = sorted(set(requested) - known)
        if missing:
            raise ValueError(
                "Explicit KR symbols are not present in kr_symbol_universe: "
                + ", ".join(missing)
            )
        selected = [symbol for symbol in requested if resume is None or symbol > resume]
        return selected[:limit] if limit is not None else selected

    statement = (
        select(KRSymbolUniverse.symbol)
        .where(
            KRSymbolUniverse.is_active.is_(True),
            KRSymbolUniverse.is_common_share.is_(True),
            KRSymbolUniverse.delist_date.is_(None),
        )
        .order_by(KRSymbolUniverse.symbol)
    )
    if resume is not None:
        statement = statement.where(KRSymbolUniverse.symbol > resume)
    if limit is not None:
        statement = statement.limit(limit)
    result = await db.execute(statement)
    return list(result.scalars().all())


def _canonical_hash(payload: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise KRLifecycleActionError(
            "Provider row is not representable as canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_key(*, symbol: str, endpoint: str, tr_id: str, raw_hash: str) -> str:
    material = "\x1f".join((_PROVIDER, endpoint, tr_id, symbol, raw_hash))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _optional_text(row: dict[str, Any], keys: Sequence[str], label: str) -> str | None:
    values = [str(row[key]).strip() for key in keys if row.get(key) not in (None, "")]
    unique = list(dict.fromkeys(value for value in values if value))
    if len(unique) > 1:
        raise KRLifecycleActionError(
            f"Provider row has conflicting direct values for {label}: {unique}"
        )
    return unique[0] if unique else None


def _optional_date(row: dict[str, Any], keys: Sequence[str], label: str) -> date | None:
    values = [
        str(row[key]).strip()
        for key in keys
        if row.get(key) not in (None, "")
        and str(row[key]).strip()
        and set(str(row[key]).strip()) != {"0"}
    ]
    unique = list(dict.fromkeys(values))
    if len(unique) > 1:
        raise KRLifecycleActionError(
            f"Provider row has conflicting direct values for {label}: {unique}"
        )
    if not unique:
        return None
    raw = unique[0]
    for format_string in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, format_string).date()
        except ValueError:
            continue
    raise KRLifecycleActionError(f"Provider supplied invalid {label}: {raw!r}")


def _validate_row_symbol(row: dict[str, Any], symbol: str, keys: Sequence[str]) -> None:
    direct = _optional_text(row, keys, "symbol")
    if direct is not None and normalize_kr_symbol(direct) != symbol:
        raise KRLifecycleActionError(
            f"Provider row symbol {direct!r} does not match requested symbol {symbol}"
        )


def build_lifecycle_evidence(
    *,
    symbol: str,
    row: dict[str, Any],
    observed_at: datetime,
    fetch_run_id: uuid.UUID,
) -> dict[str, Any]:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    normalized_symbol = normalize_kr_symbol(symbol)
    _validate_row_symbol(row, normalized_symbol, ("pdno", "sht_cd"))
    list_date = _optional_date(
        row,
        (
            "list_date",
            "lstg_dt",
            "scts_mket_lstg_dt",
            "kosdaq_mket_lstg_dt",
            "frbd_mket_lstg_dt",
        ),
        "list_date",
    )
    delist_date = _optional_date(
        row,
        (
            "delist_date",
            "lstg_abol_dt",
            "scts_mket_lstg_abol_dt",
            "kosdaq_mket_lstg_abol_dt",
            "frbd_mket_lstg_abol_dt",
        ),
        "delist_date",
    )
    if list_date is not None and delist_date is not None and list_date > delist_date:
        raise KRLifecycleActionError("Provider lifecycle dates are reversed")
    raw_fields = dict(row)
    raw_hash = _canonical_hash(raw_fields)
    return {
        "symbol": normalized_symbol,
        "source": _SOURCE,
        "provider": _PROVIDER,
        "provider_endpoint": SEARCH_STOCK_INFO_ENDPOINT,
        "provider_tr_id": SEARCH_STOCK_INFO_TR,
        "pdno": _optional_text(row, ("pdno",), "pdno"),
        "std_pdno": _optional_text(row, ("std_pdno",), "std_pdno"),
        "isin": _optional_text(row, ("isin", "isin_cd"), "isin"),
        "listing_status": _optional_text(
            row,
            ("listing_status", "lstg_status", "lstg_stts", "lstg_stts_cd"),
            "listing_status",
        ),
        "list_date": list_date,
        "delist_date": delist_date,
        "observed_at": observed_at,
        "fetch_run_id": fetch_run_id,
        "raw_provider_fields": raw_fields,
        "canonical_raw_hash": raw_hash,
        "idempotency_key": _idempotency_key(
            symbol=normalized_symbol,
            endpoint=SEARCH_STOCK_INFO_ENDPOINT,
            tr_id=SEARCH_STOCK_INFO_TR,
            raw_hash=raw_hash,
        ),
    }


def build_action_evidence(
    *,
    symbol: str,
    row: dict[str, Any],
    spec: _ActionSpec,
    window: MonthlyWindow,
    observed_at: datetime,
    fetch_run_id: uuid.UUID,
) -> dict[str, Any]:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if window.from_date > window.to_date:
        raise ValueError("corporate-action window dates are reversed")
    normalized_symbol = normalize_kr_symbol(symbol)
    _validate_row_symbol(row, normalized_symbol, ("sht_cd", "pdno"))
    raw_fields = dict(row)
    raw_hash = _canonical_hash(raw_fields)
    return {
        "symbol": normalized_symbol,
        "source": _SOURCE,
        "provider": _PROVIDER,
        "provider_endpoint": spec.endpoint,
        "provider_tr_id": spec.tr_id,
        "evidence_kind": spec.evidence_kind,
        "provider_action_type": _optional_text(
            row, spec.action_type_keys, "provider_action_type"
        ),
        "std_pdno": _optional_text(row, ("std_pdno",), "std_pdno"),
        "isin": _optional_text(row, ("isin", "isin_cd"), "isin"),
        "requested_from_date": window.from_date,
        "requested_to_date": window.to_date,
        "effective_date": _optional_date(
            row, ("effective_date", "eff_dt"), "effective_date"
        ),
        "record_date": _optional_date(row, ("record_date",), "record_date"),
        "list_date": _optional_date(row, spec.list_date_keys, "list_date"),
        "payment_date": _first_supplied_date(
            row, spec.payment_date_keys, "payment_date"
        ),
        "observed_at": observed_at,
        "fetch_run_id": fetch_run_id,
        "raw_provider_fields": raw_fields,
        "canonical_raw_hash": raw_hash,
        "idempotency_key": _idempotency_key(
            symbol=normalized_symbol,
            endpoint=spec.endpoint,
            tr_id=spec.tr_id,
            raw_hash=raw_hash,
        ),
    }


def _first_supplied_date(
    row: dict[str, Any], keys: Sequence[str], label: str
) -> date | None:
    for key in keys:
        if row.get(key) in (None, ""):
            continue
        parsed = _optional_date(row, (key,), label)
        if parsed is not None:
            return parsed
    return None


def _merged_lifecycle_metadata(
    evidence_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for field_name in (
        "std_pdno",
        "isin",
        "listing_status",
        "list_date",
        "delist_date",
    ):
        values = {
            row[field_name] for row in evidence_rows if row.get(field_name) is not None
        }
        if len(values) > 1:
            detail = sorted(map(str, values))
            raise KRLifecycleActionError(
                f"Lifecycle pages conflict on direct field {field_name}: {detail}"
            )
        if values:
            merged[field_name] = values.pop()
    return merged


def build_fetch_coverage(
    *,
    symbol: str,
    spec: _ActionSpec,
    window: MonthlyWindow,
    fetch_run_id: uuid.UUID,
    status: str,
    row_count: int,
    page_count: int,
    last_cursor: str | None,
    completed_at: datetime,
    error: Exception | None = None,
) -> dict[str, Any]:
    if status not in {"success", "failed"}:
        raise ValueError("coverage status must be success or failed")
    if status == "success" and error is not None:
        raise ValueError("success coverage cannot contain an error")
    if status == "failed" and error is None:
        raise ValueError("failed coverage requires an error")
    if row_count < 0:
        raise ValueError("coverage row_count must be >= 0")
    if page_count < 0:
        raise ValueError("coverage page_count must be >= 0")
    if window.from_date > window.to_date:
        raise ValueError("coverage window dates are reversed")
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise ValueError("coverage completed_at must be timezone-aware")
    normalized_symbol = normalize_kr_symbol(symbol)
    identity = "\x1f".join(
        (
            str(fetch_run_id),
            normalized_symbol,
            spec.endpoint,
            spec.tr_id,
            window.from_date.isoformat(),
            window.to_date.isoformat(),
        )
    )
    return {
        "symbol": normalized_symbol,
        "source": _SOURCE,
        "provider": _PROVIDER,
        "provider_endpoint": spec.endpoint,
        "provider_tr_id": spec.tr_id,
        "action_kind": spec.evidence_kind,
        "requested_from_date": window.from_date,
        "requested_to_date": window.to_date,
        "completed_at": completed_at,
        "row_count": row_count,
        "status": status,
        "fetch_run_id": fetch_run_id,
        "error_class": type(error).__name__[:128] if error is not None else None,
        "error_message": str(error).strip()[:500] if error is not None else None,
        "last_cursor": str(last_cursor)[:256] if last_cursor else None,
        "page_count": page_count,
        "idempotency_key": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    }


async def upsert_lifecycle_evidence(
    db: AsyncSession, rows: Sequence[dict[str, Any]]
) -> int:
    if not rows:
        return 0
    statement = (
        pg_insert(KRStockLifecycleObservation)
        .values(list(rows))
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(KRStockLifecycleObservation.id)
    )
    result = await db.execute(statement)
    return len(result.scalars().all())


async def upsert_action_evidence(
    db: AsyncSession, rows: Sequence[dict[str, Any]]
) -> int:
    if not rows:
        return 0
    statement = (
        pg_insert(KRCorporateActionEvidence)
        .values(list(rows))
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(KRCorporateActionEvidence.id)
    )
    result = await db.execute(statement)
    return len(result.scalars().all())


async def upsert_fetch_coverage(
    db: AsyncSession, rows: Sequence[dict[str, Any]]
) -> int:
    if not rows:
        return 0
    statement = (
        pg_insert(KAssetCorporateActionFetchCoverage)
        .values(list(rows))
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(KAssetCorporateActionFetchCoverage.id)
    )
    result = await db.execute(statement)
    return len(result.scalars().all())


async def has_complete_corporate_action_coverage(
    db: AsyncSession,
    *,
    symbol: str,
    from_date: date,
    to_date: date,
) -> bool:
    normalized_symbol = normalize_kr_symbol(symbol)
    windows = monthly_windows(from_date, to_date)
    required = {
        (
            spec.endpoint,
            spec.tr_id,
            spec.evidence_kind,
            window.from_date,
            window.to_date,
        )
        for spec in _ACTION_SPECS
        for window in windows
    }
    result = await db.execute(
        select(
            KAssetCorporateActionFetchCoverage.provider_endpoint,
            KAssetCorporateActionFetchCoverage.provider_tr_id,
            KAssetCorporateActionFetchCoverage.action_kind,
            KAssetCorporateActionFetchCoverage.requested_from_date,
            KAssetCorporateActionFetchCoverage.requested_to_date,
        ).where(
            KAssetCorporateActionFetchCoverage.symbol == normalized_symbol,
            KAssetCorporateActionFetchCoverage.source == _SOURCE,
            KAssetCorporateActionFetchCoverage.provider == _PROVIDER,
            KAssetCorporateActionFetchCoverage.status == "success",
        )
    )
    observed = {tuple(row) for row in result.all()}
    return required <= observed


async def _persist_lifecycle(
    session_factory: SessionFactory,
    *,
    symbol: str,
    evidence_rows: Sequence[dict[str, Any]],
) -> int:
    metadata = _merged_lifecycle_metadata(evidence_rows)
    if not evidence_rows and not metadata:
        return 0
    async with session_factory() as db:
        async with db.begin():
            universe = (
                await db.execute(
                    select(KRSymbolUniverse)
                    .where(KRSymbolUniverse.symbol == symbol)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if universe is None:
                raise KRLifecycleActionError(
                    f"Known symbol disappeared from kr_symbol_universe: {symbol}"
                )
            inserted = await upsert_lifecycle_evidence(db, evidence_rows)
            for field_name, value in metadata.items():
                setattr(universe, field_name, value)
            return inserted


async def _persist_action_window(
    session_factory: SessionFactory,
    event_rows: Sequence[dict[str, Any]],
    coverage_rows: Sequence[dict[str, Any]],
) -> tuple[int, int]:
    if not event_rows and not coverage_rows:
        return 0, 0
    async with session_factory() as db:
        async with db.begin():
            event_count = await upsert_action_evidence(db, event_rows)
            coverage_count = await upsert_fetch_coverage(db, coverage_rows)
            return event_count, coverage_count


def _failure(
    *,
    symbol: str,
    stage: str,
    exc: Exception,
    window: MonthlyWindow | None = None,
    endpoint: str | None = None,
    tr_id: str | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "stage": stage,
        "from_date": window.from_date.isoformat() if window else None,
        "to_date": window.to_date.isoformat() if window else None,
        "provider_endpoint": endpoint,
        "provider_tr_id": tr_id,
        "error_type": type(exc).__name__,
        "message": str(exc)[:500],
    }


async def run_kr_lifecycle_action_sync(
    *,
    client: KRLifecycleActionClient,
    session_factory: SessionFactory,
    symbols: Sequence[str],
    from_date: date,
    to_date: date,
    commit: bool = False,
    observed_at: datetime | None = None,
    fetch_run_id: uuid.UUID | None = None,
) -> KRLifecycleActionSyncReport:
    normalized_symbols = [normalize_kr_symbol(symbol) for symbol in symbols]
    if len(set(normalized_symbols)) != len(normalized_symbols):
        raise ValueError("symbols must not contain duplicates")
    windows = monthly_windows(from_date, to_date)
    observation_time = observed_at or datetime.now(UTC)
    if observation_time.tzinfo is None or observation_time.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    run_id = fetch_run_id or uuid.uuid4()
    report = KRLifecycleActionSyncReport(
        mode="commit" if commit else "dry-run",
        fetch_run_id=str(run_id),
        symbols=list(normalized_symbols),
        date_from=from_date.isoformat(),
        date_to=to_date.isoformat(),
    )
    failed_symbols: set[str] = set()

    for symbol in normalized_symbols:
        report.lifecycle_requests += 1
        try:
            raw_lifecycle_rows = await client.search_stock_info(symbol)
            lifecycle_rows = [
                build_lifecycle_evidence(
                    symbol=symbol,
                    row=row,
                    observed_at=observation_time,
                    fetch_run_id=run_id,
                )
                for row in raw_lifecycle_rows
            ]
            report.lifecycle_rows += len(lifecycle_rows)
            report.rows_prepared += len(lifecycle_rows)
            if commit:
                inserted = await _persist_lifecycle(
                    session_factory,
                    symbol=symbol,
                    evidence_rows=lifecycle_rows,
                )
                report.rows_inserted += inserted
                report.duplicate_rows += len(lifecycle_rows) - inserted
        except Exception as exc:  # noqa: BLE001 - isolate one symbol and report it
            failed_symbols.add(symbol)
            report.failures.append(_failure(symbol=symbol, stage="lifecycle", exc=exc))

        for window in windows:
            report.windows_attempted += 1
            prepared_window: list[dict[str, Any]] = []
            coverage_window: list[dict[str, Any]] = []
            window_failure: tuple[_ActionSpec, Exception] | None = None
            for spec in _ACTION_SPECS:
                raw_action_rows: list[dict[str, Any]] | None = None
                try:
                    method = getattr(client, spec.method_name)
                    raw_action_rows = await method(
                        symbol,
                        window.from_date,
                        window.to_date,
                    )
                    report.corporate_action_rows += len(raw_action_rows)
                    spec_evidence_rows = [
                        build_action_evidence(
                            symbol=symbol,
                            row=row,
                            spec=spec,
                            window=window,
                            observed_at=observation_time,
                            fetch_run_id=run_id,
                        )
                        for row in raw_action_rows
                    ]
                    prepared_window.extend(spec_evidence_rows)
                    coverage_window.append(
                        build_fetch_coverage(
                            symbol=symbol,
                            spec=spec,
                            window=window,
                            fetch_run_id=run_id,
                            status="success",
                            row_count=len(raw_action_rows),
                            page_count=int(getattr(raw_action_rows, "page_count", 1)),
                            last_cursor=getattr(raw_action_rows, "last_cursor", None),
                            completed_at=datetime.now(UTC),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - isolate symbol/window
                    failed_symbols.add(symbol)
                    window_failure = (spec, exc)
                    coverage_window.append(
                        build_fetch_coverage(
                            symbol=symbol,
                            spec=spec,
                            window=window,
                            fetch_run_id=run_id,
                            status="failed",
                            row_count=(
                                len(raw_action_rows)
                                if raw_action_rows is not None
                                else 0
                            ),
                            page_count=int(
                                getattr(
                                    exc,
                                    "page_count",
                                    getattr(raw_action_rows, "page_count", 0),
                                )
                            ),
                            last_cursor=getattr(
                                exc,
                                "last_cursor",
                                getattr(raw_action_rows, "last_cursor", None),
                            ),
                            completed_at=datetime.now(UTC),
                            error=exc,
                        )
                    )
                    report.failures.append(
                        _failure(
                            symbol=symbol,
                            stage="corporate_action_window",
                            exc=exc,
                            window=window,
                            endpoint=spec.endpoint,
                            tr_id=spec.tr_id,
                        )
                    )
                    break

            report.rows_prepared += len(prepared_window)
            report.coverage_rows_prepared += len(coverage_window)
            if commit:
                try:
                    inserted, coverage_inserted = await _persist_action_window(
                        session_factory,
                        prepared_window,
                        coverage_window,
                    )
                    report.rows_inserted += inserted
                    report.duplicate_rows += len(prepared_window) - inserted
                    report.coverage_rows_inserted += coverage_inserted
                    report.coverage_duplicate_rows += (
                        len(coverage_window) - coverage_inserted
                    )
                except Exception as exc:  # noqa: BLE001 - isolate symbol/window
                    failed_symbols.add(symbol)
                    if window_failure is None:
                        window_failure = (_ACTION_SPECS[0], exc)
                    report.failures.append(
                        _failure(
                            symbol=symbol,
                            stage="corporate_action_persist",
                            exc=exc,
                            window=window,
                        )
                    )

            if window_failure is not None:
                report.windows_failed += 1
            else:
                report.windows_succeeded += 1

    report.symbols_failed = len(failed_symbols)
    report.symbols_succeeded = len(normalized_symbols) - report.symbols_failed
    return report
