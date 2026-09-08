"""KR 종목 lifecycle·기업행위 증거 행의 구성과 영속화.

과거에 KIS OpenAPI로 수집하던 경로(클라이언트 프로토콜과 sync 드라이버)는
공급자 제거와 함께 삭제됐다. 남은 것은 이미 저장된 증거·커버리지 행과 동일한
규칙으로 행을 만들고 멱등 upsert 하는 순수/영속화 계층이며, 새 브로커 수집은
일어나지 않는다. 이 서비스는 delisted 종목을 열거하지 않고 ``kr_symbol_universe``에
명시된 심볼만 다룬다.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_lifecycle_actions import (
    KAssetCorporateActionFetchCoverage,
    KRCorporateActionEvidence,
    KRStockLifecycleObservation,
)
from app.models.kr_symbol_universe import KRSymbolUniverse

# 삭제된 ``app.services.brokers.kis.corporate_actions``의 endpoint/TR 식별자를
# 값 그대로 옮겨왔다. 이 값들은 이미 저장된 증거·커버리지 행의
# ``provider_endpoint``/``provider_tr_id`` 컬럼에 그대로 들어 있으므로 과거 행을
# 해석하고 재계산하려면 동일한 문자열이 유지돼야 한다. 새 수집 경로는 없다.
SEARCH_STOCK_INFO_ENDPOINT = "/uapi/domestic-stock/v1/quotations/search-stock-info"
SEARCH_STOCK_INFO_TR = "CTPF1002R"
REV_SPLIT_ENDPOINT = "/uapi/domestic-stock/v1/ksdinfo/rev-split"
REV_SPLIT_TR = "HHKDB669105C0"
PAIDIN_CAPIN_ENDPOINT = "/uapi/domestic-stock/v1/ksdinfo/paidin-capin"
PAIDIN_CAPIN_TR = "HHKDB669100C0"
BONUS_ISSUE_ENDPOINT = "/uapi/domestic-stock/v1/ksdinfo/bonus-issue"
BONUS_ISSUE_TR = "HHKDB669101C0"
DIVIDEND_ENDPOINT = "/uapi/domestic-stock/v1/ksdinfo/dividend"
DIVIDEND_TR = "HHKDB669102C0"

_SOURCE = "kis_openapi"
_PROVIDER = "KIS"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{6}$")


class KRLifecycleActionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MonthlyWindow:
    from_date: date
    to_date: date


@dataclass(frozen=True)
class _ActionSpec:
    evidence_kind: str
    endpoint: str
    tr_id: str
    list_date_keys: tuple[str, ...]
    payment_date_keys: tuple[str, ...] = ()
    action_type_keys: tuple[str, ...] = ("action_type", "event_type", "ca_type")


_ACTION_SPECS = (
    _ActionSpec(
        evidence_kind="face_value_change",
        endpoint=REV_SPLIT_ENDPOINT,
        tr_id=REV_SPLIT_TR,
        list_date_keys=("list_dt",),
    ),
    _ActionSpec(
        evidence_kind="paid_in_capital_increase",
        endpoint=PAIDIN_CAPIN_ENDPOINT,
        tr_id=PAIDIN_CAPIN_TR,
        list_date_keys=("list_date",),
    ),
    _ActionSpec(
        evidence_kind="bonus_issue",
        endpoint=BONUS_ISSUE_ENDPOINT,
        tr_id=BONUS_ISSUE_TR,
        list_date_keys=("list_date",),
        payment_date_keys=("odd_pay_dt",),
    ),
    _ActionSpec(
        evidence_kind="dividend",
        endpoint=DIVIDEND_ENDPOINT,
        tr_id=DIVIDEND_TR,
        list_date_keys=(),
        payment_date_keys=("divi_pay_dt", "stk_div_pay_dt", "odd_pay_dt"),
        action_type_keys=("action_type", "event_type", "ca_type", "divi_kind"),
    ),
)


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
