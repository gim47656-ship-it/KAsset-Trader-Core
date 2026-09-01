from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import WatchError

from app.analysis.models import PriceAnalysis
from app.core.config import settings

if TYPE_CHECKING:
    from app.extensions.kasset.ai.runtime_config import AiRuntimeSnapshot

logger = logging.getLogger(__name__)

SCREENER_LIVE_ORDER_UNAVAILABLE = (
    "live order submission is not available on this endpoint"
)

ScreenMarket = Literal["kr", "us", "crypto"]
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()
#: 종료 시 취소한 보고서 task를 회수하는 유한 대기. 취소를 계속 삼키는 코루틴은
#: Python이 강제 종료할 수 없으므로 빈 registry를 약속하지 않는다.
_REPORT_SHUTDOWN_REAP_TIMEOUT_SECONDS = 5.0


def _spawn_background(
    coro: Coroutine[Any, Any, Any], *, name: str
) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def shutdown_screener_report_tasks() -> None:
    """앱 종료 전에 추적 중인 보고서 생성 task를 취소하고 유한하게 회수한다."""

    tasks = tuple(_BACKGROUND_TASKS)
    if not tasks:
        return

    for task in tasks:
        task.cancel()

    _, pending = await asyncio.wait(
        tasks, timeout=_REPORT_SHUTDOWN_REAP_TIMEOUT_SECONDS
    )
    if pending:
        logger.warning(
            "screener.report_shutdown_pending",
            extra={"pending_report_tasks": len(pending)},
        )


class _GeneratedScreenerReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["buy", "hold", "sell"]
    confidence: int = Field(ge=0, le=100)
    reasons: list[str] = Field(min_length=1, max_length=8)
    price_analysis: PriceAnalysis
    detailed_text: str = Field(min_length=1)


async def _report_route_snapshot() -> AiRuntimeSnapshot:
    """보고서 route 정책 snapshot을 한 번 읽는다.

    배경 task에는 요청 scope session이 없으므로 다른 job/flow와 같이 자체
    session을 연다. 조회가 실패하면 ``get_ai_runtime_snapshot``이 fail-closed
    snapshot을 돌려주고, 그 결과 route가 하나도 조립되지 않는다.
    """

    from app.core.db import AsyncSessionLocal
    from app.services.ai_runtime_config import get_ai_runtime_snapshot

    async with AsyncSessionLocal() as db:
        return await get_ai_runtime_snapshot(db)


async def generate_screener_report(
    *, market: str, symbol: str, name: str
) -> dict[str, Any]:
    """``review_terra`` 정책 순서로 근거 기반 보고서를 만든다.

    운영자가 저장한 lane 정책이 route 순서를 정하고, MCP sidecar가 없거나
    정책에서 빠지면 direct-api·openrouter fallback이 그대로 이어받는다.
    """
    from app.extensions.kasset.ai.factory import build_review_json_client
    from app.extensions.kasset.ai.runtime_config import AiLane
    from app.mcp_server.tooling.analysis_tool_handlers import analyze_stock_impl

    client = build_review_json_client(
        name="screener-report",
        lane=AiLane.REVIEW_TERRA,
        direct_model=settings.KASSET_AI_MODEL_TERRA,
        fallback_model=settings.KASSET_AI_OPENROUTER_MODEL_PRO,
        snapshot=await _report_route_snapshot(),
    )
    if client is None:
        raise RuntimeError("review_terra lane has no usable AI route")
    evidence = await analyze_stock_impl(symbol=symbol, market=market)
    raw = await client.request_json(
        model=settings.KASSET_AI_MODEL_TERRA,
        input_payload={
            "market": market,
            "symbol": symbol,
            "name": name,
            "analysis": evidence,
        },
        reasoning_effort="high",
        schema_name="screener_trading_report",
        schema=_GeneratedScreenerReport.model_json_schema(),
        additional_instructions=(
            "분석 증거에 없는 수치나 사실을 만들지 마세요. 모든 설명은 한국어로 작성하고 "
            "현재가와 기술적 근거에 맞춰 네 가격 범위를 제시하세요. 각 범위는 min이 max보다 "
            "작거나 같아야 합니다. 투자 수익을 보장하거나 매매를 강요하지 마세요."
        ),
    )
    report = _GeneratedScreenerReport.model_validate(raw)
    price_analysis = report.price_analysis
    for price_range in (
        price_analysis.appropriate_buy_range,
        price_analysis.appropriate_sell_range,
        price_analysis.buy_hope_range,
        price_analysis.sell_target_range,
    ):
        if price_range.min > price_range.max:
            raise ValueError("screener report price range min exceeds max")
    return report.model_dump(mode="json")


async def screen_stocks_impl(**kwargs: Any) -> dict[str, Any]:
    """Lazy wrapper for the MCP screening implementation.

    Tests monkeypatch this module-level name, while read-only `/invest` paths
    can import ScreenerService without loading the full MCP registry/order stack.
    """
    from app.mcp_server.tooling.analysis_tool_handlers import screen_stocks_impl as impl

    return await impl(**kwargs)


@dataclass(slots=True)
class _ReportKeys:
    result_key: str
    inflight_key: str
    status_key: str
    job_key: str


class ScreenerService:
    SCREENING_CACHE_TTL_SECONDS = 300
    REPORT_CACHE_TTL_SECONDS = 3600
    REPORT_INFLIGHT_TTL_SECONDS = 120
    # 생성 상한은 inflight TTL보다 반드시 짧아야 한다. TTL이 먼저 만료되면 다음
    # ``request_report``가 새 ``SET NX``를 잡아 같은 종목에 두 번째 생성을 띄우고
    # evidence 수집과 모델 호출 비용이 이중으로 든다.
    REPORT_GENERATION_TIMEOUT_SECONDS = 100
    REPORT_STATUSES = frozenset({"queued", "running", "completed", "failed"})
    TERMINAL_REPORT_STATUSES = frozenset({"completed", "failed"})
    REPORT_STATUS_ORDER = {
        "queued": 0,
        "running": 1,
        "completed": 2,
        "failed": 2,
    }

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        report_generator: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._redis = redis_client
        self._report_generator = report_generator or generate_screener_report

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(
                settings.get_redis_url(),
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                decode_responses=True,
            )
        return self._redis

    @staticmethod
    def _normalize_market(market: str) -> ScreenMarket:
        normalized = (market or "").strip().lower()
        if normalized in {"kr", "kospi", "kosdaq"}:
            return "kr"
        if normalized in {"us", "nasdaq", "nyse"}:
            return "us"
        if normalized == "crypto":
            return "crypto"
        raise ValueError("market must be one of: kr, us, crypto")

    @staticmethod
    def _normalize_symbol(market: ScreenMarket, symbol: str) -> str:
        raw = (symbol or "").strip()
        if not raw:
            raise ValueError("symbol is required")
        if market == "kr":
            return raw.upper()
        if market == "us":
            return raw.upper()
        return raw.upper()

    @staticmethod
    def _instrument_type(market: ScreenMarket) -> str:
        mapping = {
            "kr": "equity_kr",
            "us": "equity_us",
            "crypto": "crypto",
        }
        return mapping[market]

    @staticmethod
    def _market_from_instrument_type(instrument_type: str | None) -> ScreenMarket:
        normalized = (instrument_type or "").strip().lower()
        mapping: dict[str, ScreenMarket] = {
            "equity_kr": "kr",
            "equity_us": "us",
            "crypto": "crypto",
        }
        if normalized in mapping:
            return mapping[normalized]
        raise ValueError("instrument_type must be one of: equity_kr, equity_us, crypto")

    @staticmethod
    def _compact_json(data: dict[str, Any]) -> str:
        return json.dumps(
            data, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

    @staticmethod
    def _normalize_sort_by(market: ScreenMarket, sort_by: str | None) -> str | None:
        normalized = (sort_by or "").strip().lower() or None
        if market == "crypto" and normalized == "volume":
            return "trade_amount"
        return normalized

    @staticmethod
    def _normalize_min_volume(min_volume: float | None) -> float | None:
        if min_volume is None:
            return None
        if min_volume < 0:
            raise ValueError("min_volume must be >= 0")
        return min_volume

    @staticmethod
    def _calculate_overfetch_limit(request_limit: int) -> int:
        return min(100, max(request_limit * 3, request_limit))

    @staticmethod
    def _volume_metric_for_row(market: ScreenMarket, row: dict[str, Any]) -> float:
        raw_value = (
            row.get("trade_amount_24h") if market == "crypto" else row.get("volume")
        )
        try:
            return float(raw_value or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _apply_min_volume_filter(
        cls,
        result: dict[str, Any],
        *,
        market: ScreenMarket,
        min_volume: float | None,
        request_limit: int,
    ) -> dict[str, Any]:
        if min_volume is None:
            return result

        raw_results = result.get("results")
        if not isinstance(raw_results, list):
            raw_results = []

        filtered_results = [
            row
            for row in raw_results
            if isinstance(row, dict)
            and cls._volume_metric_for_row(market, row) >= min_volume
        ]
        sliced_results = filtered_results[:request_limit]

        filters_applied = result.get("filters_applied")
        normalized_filters_applied = (
            dict(filters_applied) if isinstance(filters_applied, dict) else {}
        )
        normalized_filters_applied["min_volume"] = min_volume

        return {
            **result,
            "results": sliced_results,
            "total_count": len(filtered_results),
            "returned_count": len(sliced_results),
            "filters_applied": normalized_filters_applied,
        }

    def _screening_cache_key(self, filters: dict[str, Any]) -> str:
        serialized = self._compact_json(filters)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        return f"screener:list:{digest}"

    def _report_keys(
        self, market: ScreenMarket, symbol: str, job_id: str
    ) -> _ReportKeys:
        return _ReportKeys(
            result_key=f"screener:report:result:{market}:{symbol}",
            inflight_key=f"screener:report:inflight:{market}:{symbol}",
            status_key=f"screener:report:status:{job_id}",
            job_key=f"screener:report:job:{job_id}",
        )

    @classmethod
    def _normalize_report_status(cls, status: str | None) -> str | None:
        normalized = (status or "").strip().lower()
        if normalized in cls.REPORT_STATUSES:
            return normalized
        return None

    @classmethod
    def _can_transition_report_status(
        cls, current: str | None, next_status: str
    ) -> bool:
        normalized_next = cls._normalize_report_status(next_status)
        if normalized_next is None:
            raise ValueError(f"invalid report status: {next_status}")

        normalized_current = cls._normalize_report_status(current)
        if normalized_current is None:
            return True
        if normalized_current in cls.TERMINAL_REPORT_STATUSES:
            return False
        return (
            cls.REPORT_STATUS_ORDER[normalized_next]
            >= cls.REPORT_STATUS_ORDER[normalized_current]
        )

    async def _transition_report_status(
        self,
        status_key: str,
        next_status: str,
        *,
        redis_client: redis.Redis | None = None,
    ) -> str:
        normalized_next = self._normalize_report_status(next_status)
        if normalized_next is None:
            raise ValueError(f"invalid report status: {next_status}")

        if redis_client is None:
            redis_client = await self._get_redis()

        pipeline_factory = getattr(redis_client, "pipeline", None)
        if not callable(pipeline_factory):
            return await self._transition_report_status_non_atomic(
                redis_client,
                status_key,
                normalized_next,
            )

        for _ in range(3):
            pipeline = cast(Any, pipeline_factory(transaction=True))
            try:
                await pipeline.watch(status_key)
                current_status = self._normalize_report_status(
                    await pipeline.get(status_key)
                )
                if not self._can_transition_report_status(
                    current_status, normalized_next
                ):
                    return current_status or normalized_next

                pipeline.multi()
                pipeline.setex(
                    status_key,
                    self.REPORT_CACHE_TTL_SECONDS,
                    normalized_next,
                )
                await pipeline.execute()
                return normalized_next
            except WatchError:
                continue
            finally:
                await pipeline.reset()

        return await self._transition_report_status_non_atomic(
            redis_client,
            status_key,
            normalized_next,
        )

    async def _transition_report_status_non_atomic(
        self,
        redis_client: redis.Redis,
        status_key: str,
        normalized_next: str,
    ) -> str:
        current_status = self._normalize_report_status(
            await redis_client.get(status_key)
        )
        if not self._can_transition_report_status(current_status, normalized_next):
            return current_status or normalized_next

        await redis_client.setex(
            status_key,
            self.REPORT_CACHE_TTL_SECONDS,
            normalized_next,
        )
        return normalized_next

    async def _release_inflight_claim(
        self,
        inflight_key: str,
        job_id: str,
        *,
        redis_client: redis.Redis | None = None,
    ) -> bool:
        """이 job이 잡은 claim일 때만 지운다.

        inflight key는 market+symbol 스코프라 개별 job보다 오래 산다. 소유권을
        확인하지 않고 지우면 늦게 끝난 job이 그 사이에 시작한 다음 job의 claim을
        날려 같은 종목에 세 번째 생성을 열어 준다.
        """

        if not inflight_key or not job_id:
            return False
        if redis_client is None:
            redis_client = await self._get_redis()

        pipeline_factory = getattr(redis_client, "pipeline", None)
        if not callable(pipeline_factory):
            if await redis_client.get(inflight_key) != job_id:
                return False
            await redis_client.delete(inflight_key)
            return True

        for _ in range(3):
            pipeline = cast(Any, pipeline_factory(transaction=True))
            try:
                await pipeline.watch(inflight_key)
                if await pipeline.get(inflight_key) != job_id:
                    return False
                pipeline.multi()
                pipeline.delete(inflight_key)
                await pipeline.execute()
                return True
            except WatchError:
                continue
            finally:
                await pipeline.reset()
        return False

    async def _load_cached_json(self, key: str) -> dict[str, Any] | None:
        redis_client = await self._get_redis()
        raw = await redis_client.get(key)
        if not raw:
            return None
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return None

    async def _store_json(self, key: str, ttl: int, data: dict[str, Any]) -> None:
        redis_client = await self._get_redis()
        await redis_client.setex(key, ttl, self._compact_json(data))

    async def list_screening(
        self,
        market: str = "kr",
        asset_type: str | None = None,
        category: str | None = None,
        sector: str | None = None,
        strategy: str | None = None,
        sort_by: str | None = None,
        sort_order: str | None = "desc",
        min_market_cap: float | None = None,
        max_per: float | None = None,
        max_pbr: float | None = None,
        min_dividend_yield: float | None = None,
        min_dividend: float | None = None,
        min_analyst_buy: float | None = None,
        max_rsi: float | None = None,
        min_volume: float | None = None,
        min_consecutive_up_days: int | None = None,
        min_week_change_rate: float | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        from app.mcp_server.tooling.analysis_screen_core import normalize_screen_request

        normalized_request = normalize_screen_request(
            market=market,
            asset_type=asset_type,
            category=category,
            sector=sector,
            strategy=strategy,
            sort_by=sort_by,
            sort_order=sort_order,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            min_dividend_yield=min_dividend_yield,
            min_dividend=min_dividend,
            min_analyst_buy=min_analyst_buy,
            max_rsi=max_rsi,
            min_consecutive_up_days=min_consecutive_up_days,
            min_week_change_rate=min_week_change_rate,
            limit=limit,
        )
        normalized_market = self._normalize_market(normalized_request["market"])
        normalized_sort_by = self._normalize_sort_by(normalized_market, sort_by)
        normalized_min_volume = self._normalize_min_volume(min_volume)
        request_limit = limit
        filters = {
            "market": normalized_market,
            "asset_type": normalized_request["asset_type"],
            "category": normalized_request["category_for_filters"],
            "sector": normalized_request["sector"],
            "strategy": normalized_request["strategy"],
            "sort_by": normalized_sort_by,
            "sort_order": normalized_request["sort_order"],
            "min_market_cap": normalized_request["min_market_cap"],
            "max_per": normalized_request["max_per"],
            "max_pbr": normalized_request["max_pbr"],
            "min_dividend_yield": normalized_request["min_dividend_yield"],
            "min_analyst_buy": normalized_request["min_analyst_buy"],
            "max_rsi": normalized_request["max_rsi"],
            "min_volume": normalized_min_volume,
            "min_consecutive_up_days": normalized_request["min_consecutive_up_days"],
            "min_week_change_rate": normalized_request["min_week_change_rate"],
            "limit": request_limit,
        }
        cache_key = self._screening_cache_key(filters)
        cached = await self._load_cached_json(cache_key)
        if cached:
            return {**cached, "cache_hit": True}

        call_kwargs = {
            key: value
            for key, value in filters.items()
            if value is not None and key != "min_volume"
        }
        if (
            normalized_min_volume is not None
            or normalized_request["min_analyst_buy"] is not None
        ):
            call_kwargs["limit"] = self._calculate_overfetch_limit(request_limit)
        if normalized_request["min_dividend_input"] is not None:
            call_kwargs["min_dividend"] = normalized_request["min_dividend_input"]

        result = await screen_stocks_impl(**cast(Any, call_kwargs))
        filtered_result = self._apply_min_volume_filter(
            result,
            market=normalized_market,
            min_volume=normalized_min_volume,
            request_limit=request_limit,
        )
        filters_applied = filtered_result.get("filters_applied")
        normalized_filters_applied = (
            dict(filters_applied) if isinstance(filters_applied, dict) else {}
        )
        normalized_filters_applied.setdefault("market", normalized_market)
        normalized_filters_applied.setdefault(
            "asset_type", normalized_request["asset_type"]
        )
        normalized_filters_applied.setdefault(
            "category", normalized_request["category_for_filters"]
        )
        normalized_filters_applied.setdefault("sector", normalized_request["sector"])
        normalized_filters_applied.setdefault(
            "strategy", normalized_request["strategy"]
        )
        normalized_filters_applied.setdefault("sort_by", normalized_sort_by)
        normalized_filters_applied.setdefault(
            "sort_order", normalized_request["sort_order"]
        )
        normalized_filters_applied.setdefault(
            "min_market_cap", normalized_request["min_market_cap"]
        )
        normalized_filters_applied.setdefault("max_per", normalized_request["max_per"])
        normalized_filters_applied.setdefault("max_pbr", normalized_request["max_pbr"])
        normalized_filters_applied.setdefault(
            "min_dividend_yield", normalized_request["min_dividend_yield"]
        )
        normalized_filters_applied.setdefault(
            "min_analyst_buy", normalized_request["min_analyst_buy"]
        )
        normalized_filters_applied.setdefault("max_rsi", normalized_request["max_rsi"])
        normalized_filters_applied.setdefault(
            "min_consecutive_up_days", normalized_request["min_consecutive_up_days"]
        )
        normalized_filters_applied.setdefault(
            "min_week_change_rate", normalized_request["min_week_change_rate"]
        )
        normalized_filters_applied["min_volume"] = normalized_min_volume
        if normalized_request["min_dividend_input"] is not None:
            normalized_filters_applied["min_dividend_input"] = normalized_request[
                "min_dividend_input"
            ]
            normalized_filters_applied["min_dividend_normalized"] = normalized_request[
                "min_dividend_yield"
            ]
            normalized_filters_applied["min_dividend_yield_input"] = normalized_request[
                "min_dividend_input"
            ]
            normalized_filters_applied["min_dividend_yield_normalized"] = (
                normalized_request["min_dividend_yield"]
            )
        filtered_result = {
            **filtered_result,
            "filters_applied": normalized_filters_applied,
        }
        await self._store_json(
            cache_key,
            self.SCREENING_CACHE_TTL_SECONDS,
            filtered_result,
        )
        return {**filtered_result, "cache_hit": False}

    async def refresh_screening(
        self,
        market: str = "kr",
        asset_type: str | None = None,
        category: str | None = None,
        sector: str | None = None,
        strategy: str | None = None,
        sort_by: str | None = None,
        sort_order: str | None = "desc",
        min_market_cap: float | None = None,
        max_per: float | None = None,
        max_pbr: float | None = None,
        min_dividend_yield: float | None = None,
        min_dividend: float | None = None,
        min_analyst_buy: float | None = None,
        max_rsi: float | None = None,
        min_volume: float | None = None,
        min_consecutive_up_days: int | None = None,
        min_week_change_rate: float | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        from app.mcp_server.tooling.analysis_screen_core import normalize_screen_request

        normalized_request = normalize_screen_request(
            market=market,
            asset_type=asset_type,
            category=category,
            sector=sector,
            strategy=strategy,
            sort_by=sort_by,
            sort_order=sort_order,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            min_dividend_yield=min_dividend_yield,
            min_dividend=min_dividend,
            min_analyst_buy=min_analyst_buy,
            max_rsi=max_rsi,
            min_consecutive_up_days=min_consecutive_up_days,
            min_week_change_rate=min_week_change_rate,
            limit=limit,
        )
        normalized_market = self._normalize_market(normalized_request["market"])
        normalized_sort_by = self._normalize_sort_by(normalized_market, sort_by)
        normalized_min_volume = self._normalize_min_volume(min_volume)
        filters = {
            "market": normalized_market,
            "asset_type": normalized_request["asset_type"],
            "category": normalized_request["category_for_filters"],
            "sector": normalized_request["sector"],
            "strategy": normalized_request["strategy"],
            "sort_by": normalized_sort_by,
            "sort_order": normalized_request["sort_order"],
            "min_market_cap": normalized_request["min_market_cap"],
            "max_per": normalized_request["max_per"],
            "max_pbr": normalized_request["max_pbr"],
            "min_dividend_yield": normalized_request["min_dividend_yield"],
            "min_analyst_buy": normalized_request["min_analyst_buy"],
            "max_rsi": normalized_request["max_rsi"],
            "min_volume": normalized_min_volume,
            "min_consecutive_up_days": normalized_request["min_consecutive_up_days"],
            "min_week_change_rate": normalized_request["min_week_change_rate"],
            "limit": limit,
        }
        cache_key = self._screening_cache_key(filters)
        redis_client = await self._get_redis()
        await redis_client.delete(cache_key)
        return await self.list_screening(
            market=market,
            asset_type=asset_type,
            category=category,
            sector=sector,
            strategy=strategy,
            sort_by=sort_by,
            sort_order=sort_order,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            min_dividend_yield=min_dividend_yield,
            min_dividend=min_dividend,
            min_analyst_buy=min_analyst_buy,
            max_rsi=max_rsi,
            min_volume=min_volume,
            min_consecutive_up_days=min_consecutive_up_days,
            min_week_change_rate=min_week_change_rate,
            limit=limit,
        )

    async def request_report(
        self, market: str, symbol: str, name: str | None = None
    ) -> dict[str, Any]:
        normalized_market = self._normalize_market(market)
        normalized_symbol = self._normalize_symbol(normalized_market, symbol)
        report_key = f"screener:report:result:{normalized_market}:{normalized_symbol}"
        inflight_key = (
            f"screener:report:inflight:{normalized_market}:{normalized_symbol}"
        )
        redis_client = await self._get_redis()

        existing_report = await self._load_cached_json(report_key)
        if existing_report is not None:
            return {
                "job_id": existing_report.get("request_id"),
                "status": "completed",
                "is_reused": True,
                "report": existing_report,
            }

        inflight_job_id = await redis_client.get(inflight_key)
        if inflight_job_id:
            status_key = f"screener:report:status:{inflight_job_id}"
            status = self._normalize_report_status(await redis_client.get(status_key))
            return {
                "job_id": inflight_job_id,
                "status": status or "queued",
                "is_reused": True,
            }

        provisional_job_id = ""
        inflight_claimed = False
        for _ in range(3):
            provisional_job_id = str(uuid4())
            inflight_claimed = await redis_client.set(
                inflight_key,
                provisional_job_id,
                ex=self.REPORT_INFLIGHT_TTL_SECONDS,
                nx=True,
            )
            if inflight_claimed:
                break

            reused_job_id = await redis_client.get(inflight_key)
            if reused_job_id:
                status_key = f"screener:report:status:{reused_job_id}"
                status = self._normalize_report_status(
                    await redis_client.get(status_key)
                )
                return {
                    "job_id": reused_job_id,
                    "status": status or "queued",
                    "is_reused": True,
                }

        if not inflight_claimed:
            failed_job_id = provisional_job_id or str(uuid4())
            error_message = "inflight_job_unavailable"
            keys = self._report_keys(
                normalized_market, normalized_symbol, failed_job_id
            )
            await self._transition_report_status(
                keys.status_key,
                "failed",
                redis_client=redis_client,
            )
            await self._store_json(
                keys.job_key,
                self.REPORT_CACHE_TTL_SECONDS,
                {
                    "job_id": failed_job_id,
                    "market": normalized_market,
                    "symbol": normalized_symbol,
                    "result_key": keys.result_key,
                    "status_key": keys.status_key,
                    "inflight_key": keys.inflight_key,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "error": error_message,
                },
            )
            return {
                "job_id": failed_job_id,
                "status": "failed",
                "error": error_message,
                "is_reused": False,
            }

        display_name = (name or normalized_symbol).strip() or normalized_symbol
        instrument_type = self._instrument_type(normalized_market)
        keys = self._report_keys(
            normalized_market, normalized_symbol, provisional_job_id
        )
        metadata = {
            "job_id": provisional_job_id,
            "market": normalized_market,
            "symbol": normalized_symbol,
            "result_key": keys.result_key,
            "status_key": keys.status_key,
            "inflight_key": keys.inflight_key,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        await self._store_json(keys.job_key, self.REPORT_CACHE_TTL_SECONDS, metadata)
        persisted_status = await self._transition_report_status(
            keys.status_key,
            "queued",
            redis_client=redis_client,
        )
        await redis_client.set(
            keys.inflight_key,
            provisional_job_id,
            ex=self.REPORT_INFLIGHT_TTL_SECONDS,
        )
        _spawn_background(
            self._generate_and_store_report(
                job_id=provisional_job_id,
                market=normalized_market,
                symbol=normalized_symbol,
                name=display_name,
                instrument_type=instrument_type,
                keys=keys,
                metadata=metadata,
            ),
            name=f"screener-report:{provisional_job_id}",
        )
        return {
            "job_id": provisional_job_id,
            "status": persisted_status,
            "is_reused": False,
        }

    async def _generate_and_store_report(
        self,
        *,
        job_id: str,
        market: ScreenMarket,
        symbol: str,
        name: str,
        instrument_type: str,
        keys: _ReportKeys,
        metadata: dict[str, Any],
    ) -> None:
        redis_client = await self._get_redis()
        await self._transition_report_status(
            keys.status_key,
            "running",
            redis_client=redis_client,
        )
        generated: dict[str, Any] | None = None
        error_message = "report_generation_failed"
        try:
            # 무제한 생성은 inflight TTL을 넘겨 같은 종목에 두 번째 생성을
            # 띄운다. 상한은 evidence 수집과 모델 호출 전체를 덮는다.
            async with asyncio.timeout(self.REPORT_GENERATION_TIMEOUT_SECONDS):
                generated = await self._report_generator(
                    market=market,
                    symbol=symbol,
                    name=name,
                )
        except TimeoutError:
            error_message = "report_generation_timeout"
        except Exception as exc:
            error_message = str(exc).strip() or exc.__class__.__name__

        if generated is not None:
            try:
                callback_result = await self.process_callback(
                    {
                        "request_id": job_id,
                        "symbol": symbol,
                        "name": name,
                        "instrument_type": instrument_type,
                        **generated,
                    }
                )
                if callback_result.get("status") != "ok":
                    raise RuntimeError(
                        str(callback_result.get("error") or "report_callback_failed")
                    )
                return
            except Exception as exc:
                error_message = str(exc).strip() or exc.__class__.__name__

        await self._release_inflight_claim(
            keys.inflight_key,
            job_id,
            redis_client=redis_client,
        )
        await self._transition_report_status(
            keys.status_key,
            "failed",
            redis_client=redis_client,
        )
        await self._store_json(
            keys.job_key,
            self.REPORT_CACHE_TTL_SECONDS,
            {
                **metadata,
                "updated_at": datetime.now(UTC).isoformat(),
                "error": error_message,
            },
        )

    async def get_report_status(self, job_id: str) -> dict[str, Any]:
        if not job_id:
            raise ValueError("job_id is required")

        redis_client = await self._get_redis()
        status_key = f"screener:report:status:{job_id}"
        status = self._normalize_report_status(await redis_client.get(status_key))
        metadata = await self._load_cached_json(f"screener:report:job:{job_id}")

        if status is None and metadata is None:
            return {
                "job_id": job_id,
                "status": "failed",
                "error": "job_not_found",
                "not_found": True,
            }

        inflight_key_value = metadata.get("inflight_key") if metadata else None
        if isinstance(inflight_key_value, str) and inflight_key_value:
            inflight_job_id = await redis_client.get(inflight_key_value)
            if inflight_job_id == job_id and status in {None, "queued"}:
                status = await self._transition_report_status(
                    status_key,
                    "running",
                    redis_client=redis_client,
                )

        if status is None:
            if metadata and isinstance(metadata.get("error"), str):
                status = "failed"
            else:
                status = "queued"

        response: dict[str, Any] = {"job_id": job_id, "status": status}
        if status == "completed":
            if metadata and isinstance(metadata.get("result_key"), str):
                report = await self._load_cached_json(metadata["result_key"])
                if report is not None:
                    response["report"] = report
        elif status == "failed":
            if (
                metadata
                and isinstance(metadata.get("error"), str)
                and metadata["error"]
            ):
                response["error"] = metadata["error"]
            else:
                response["error"] = "job_failed"
        return response

    async def process_callback(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("request_id") or "").strip()
        if not job_id:
            raise ValueError("request_id is required")

        redis_client = await self._get_redis()
        job_key = f"screener:report:job:{job_id}"
        metadata = await self._load_cached_json(job_key)
        default_status_key = f"screener:report:status:{job_id}"
        status_key = (
            str(metadata.get("status_key"))
            if metadata and isinstance(metadata.get("status_key"), str)
            else default_status_key
        )
        inflight_key = (
            str(metadata.get("inflight_key"))
            if metadata and isinstance(metadata.get("inflight_key"), str)
            else ""
        )
        error_metadata_base = (
            {
                **metadata,
                "job_id": job_id,
                "status_key": status_key,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            if metadata
            else {
                "job_id": job_id,
                "status_key": status_key,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

        try:
            callback_market = self._market_from_instrument_type(
                payload.get("instrument_type")
            )
            callback_symbol = self._normalize_symbol(
                callback_market, str(payload.get("symbol") or "")
            )
        except ValueError as exc:
            error_message = str(exc)
            persisted_status = await self._transition_report_status(
                status_key,
                "failed",
                redis_client=redis_client,
            )
            if persisted_status == "failed":
                await self._store_json(
                    job_key,
                    self.REPORT_CACHE_TTL_SECONDS,
                    {
                        **error_metadata_base,
                        "error": error_message,
                    },
                )
                await self._release_inflight_claim(
                    inflight_key,
                    job_id,
                    redis_client=redis_client,
                )
            return {
                "status": "failed",
                "request_id": job_id,
                "job_id": job_id,
                "error": error_message,
            }

        if metadata is None:
            keys = self._report_keys(callback_market, callback_symbol, job_id)
            metadata = {
                "job_id": job_id,
                "market": callback_market,
                "symbol": callback_symbol,
                "result_key": keys.result_key,
                "status_key": keys.status_key,
                "inflight_key": keys.inflight_key,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        else:
            expected_market = None
            expected_symbol = None
            if isinstance(metadata.get("market"), str):
                try:
                    expected_market = self._normalize_market(metadata["market"])
                except ValueError:
                    expected_market = None
            if expected_market and isinstance(metadata.get("symbol"), str):
                try:
                    expected_symbol = self._normalize_symbol(
                        expected_market, metadata["symbol"]
                    )
                except ValueError:
                    expected_symbol = None

            if expected_market and expected_symbol:
                if (
                    callback_market != expected_market
                    or callback_symbol != expected_symbol
                ):
                    error_message = (
                        "callback_payload_mismatch:"
                        f" expected={expected_market}:{expected_symbol}"
                        f" actual={callback_market}:{callback_symbol}"
                    )
                    persisted_status = await self._transition_report_status(
                        status_key,
                        "failed",
                        redis_client=redis_client,
                    )
                    if persisted_status == "failed":
                        await self._store_json(
                            job_key,
                            self.REPORT_CACHE_TTL_SECONDS,
                            {
                                **error_metadata_base,
                                "market": expected_market,
                                "symbol": expected_symbol,
                                "error": error_message,
                            },
                        )
                        await self._release_inflight_claim(
                            inflight_key,
                            job_id,
                            redis_client=redis_client,
                        )
                    return {
                        "status": "failed",
                        "request_id": job_id,
                        "job_id": job_id,
                        "error": error_message,
                    }
            else:
                expected_market = callback_market
                expected_symbol = callback_symbol

            keys = self._report_keys(expected_market, expected_symbol, job_id)
            metadata = {
                **metadata,
                "job_id": job_id,
                "market": expected_market,
                "symbol": expected_symbol,
                "result_key": (
                    metadata["result_key"]
                    if isinstance(metadata.get("result_key"), str)
                    else keys.result_key
                ),
                "status_key": (
                    metadata["status_key"]
                    if isinstance(metadata.get("status_key"), str)
                    else keys.status_key
                ),
                "inflight_key": (
                    metadata["inflight_key"]
                    if isinstance(metadata.get("inflight_key"), str)
                    else keys.inflight_key
                ),
                "updated_at": datetime.now(UTC).isoformat(),
            }

        result_key = str(metadata["result_key"])
        status_key = str(metadata["status_key"])
        inflight_key = str(metadata["inflight_key"])
        payload_with_timestamp = {
            **payload,
            "received_at": datetime.now(UTC).isoformat(),
        }

        await self._store_json(
            result_key, self.REPORT_CACHE_TTL_SECONDS, payload_with_timestamp
        )
        await self._transition_report_status(
            status_key,
            "completed",
            redis_client=redis_client,
        )
        await self._store_json(
            f"screener:report:job:{job_id}",
            self.REPORT_CACHE_TTL_SECONDS,
            {
                **metadata,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        await self._release_inflight_claim(
            inflight_key,
            job_id,
            redis_client=redis_client,
        )

        return {
            "status": "ok",
            "request_id": job_id,
            "job_id": job_id,
            "is_reused": False,
        }

    async def place_order(
        self,
        market: str,
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: Literal["limit", "market"] = "limit",
        quantity: float | None = None,
        price: float | None = None,
        amount: float | None = None,
        confirm: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        """Return a request preview and physically reject live submission."""

        normalized_market = self._normalize_market(market)
        normalized_symbol = self._normalize_symbol(normalized_market, symbol)
        preview = {
            "dry_run": True,
            "preview_only": True,
            "market": normalized_market,
            "symbol": normalized_symbol,
            "side": side,
            "order_type": order_type,
            "quantity": quantity,
            "price": price,
            "amount": amount,
            "reason": reason,
        }
        if confirm:
            return {
                "success": False,
                **preview,
                "error": SCREENER_LIVE_ORDER_UNAVAILABLE,
                "error_code": "live_order_submission_unavailable",
                "message": SCREENER_LIVE_ORDER_UNAVAILABLE,
            }
        return {
            "success": True,
            **preview,
            "message": "Order request preview only",
        }
