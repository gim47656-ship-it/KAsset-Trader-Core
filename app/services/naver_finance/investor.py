"""Naver Finance investor trends, investment opinions, and KR snapshot."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.core.number_utils import parse_korean_number as _parse_korean_number
from app.services.analyst_normalizer import (
    build_consensus,
    normalize_rating_label,
    rating_to_bucket,
)
from app.services.naver_finance.detail_cache_port import DetailCachePort
from app.services.naver_finance.news import _parse_news_soup
from app.services.naver_finance.parser import (
    DEFAULT_HEADERS,
    NAVER_FINANCE_BASE,
    NAVER_FINANCE_ITEM,
    _extract_current_price_from_main_soup,
    _fetch_html,
    _fetch_html_with_client,
    _parse_naver_date,
)
from app.services.naver_finance.valuation import _parse_valuation_from_soups


def _parse_report_detail_soup(soup: BeautifulSoup) -> dict[str, Any] | None:
    info_div = soup.select_one("div.view_info_1")
    if not info_div:
        # ROB-814: the parse anchor itself is missing — a page-shape anomaly
        # (anti-bot interstitial, deleted-post notice, Naver selector rot),
        # NOT a report with a legitimately-absent target. Return None so the
        # assembly treats it like a fetch failure: shown as no-detail but
        # NEVER written to the insert-once ROB-811 cache, which would freeze
        # the anomaly permanently (no update path) even after a parser fix.
        return None

    result: dict[str, Any] = {
        "target_price": None,
        "rating": None,
    }

    target_elem = info_div.select_one("em.money strong")
    if target_elem:
        result["target_price"] = _parse_korean_number(target_elem.get_text(strip=True))

    rating_elem = info_div.select_one("em.coment")
    if rating_elem:
        result["rating"] = rating_elem.get_text(strip=True)

    return result


def _collect_opinion_report_infos(
    company_list_soup: BeautifulSoup,
    limit: int,
) -> list[dict[str, Any]]:
    table = company_list_soup.select_one("table.type_1")
    if not table:
        return []

    report_infos: list[dict[str, Any]] = []
    seen_nids: set[str] = set()
    rows = table.select("tbody tr, tr")
    for row in rows:
        cells = row.select("td")
        if len(cells) < 5:
            continue

        try:
            title_elem = cells[1].select_one("a")
            if not title_elem:
                continue

            href = title_elem.get("href") or ""
            href_str = href if isinstance(href, str) else ""
            nid_match = re.search(r"nid=(\d+)", href_str)
            if not nid_match:
                continue

            nid = nid_match.group(1)
            if nid in seen_nids:
                continue
            seen_nids.add(nid)

            report_infos.append(
                {
                    "nid": nid,
                    "stock_name": cells[0].get_text(strip=True),
                    "title": title_elem.get_text(strip=True),
                    "firm": cells[2].get_text(strip=True),
                    "date": _parse_naver_date(cells[4].get_text(strip=True)),
                    "url": (
                        href_str
                        if href_str.startswith("http")
                        else NAVER_FINANCE_BASE + "/research/" + href_str
                    ),
                }
            )
            if len(report_infos) >= limit:
                break
        except (IndexError, ValueError):
            continue

    return report_infos


async def _build_investment_opinions_from_company_list_soup(
    code: str,
    company_list_soup: BeautifulSoup,
    limit: int,
    *,
    current_price: int | None,
    detail_fetcher: Callable[[str], Awaitable[dict[str, Any] | None]],
    window_months: int = 12,
    detail_cache: DetailCachePort | None = None,
) -> dict[str, Any]:
    opinions: dict[str, Any] = {
        "symbol": code,
        "count": 0,
        "opinions": [],
        "consensus": None,
    }
    report_infos = _collect_opinion_report_infos(company_list_soup, limit)
    if report_infos:
        nids = [info["nid"] for info in report_infos]
        cached: dict[str, Any] = {}
        if detail_cache is not None:
            cached = await detail_cache.get_many(nids)

        miss_indexes = [i for i, nid in enumerate(nids) if nid not in cached]
        miss_results = await asyncio.gather(
            *(detail_fetcher(nids[i]) for i in miss_indexes),
            return_exceptions=True,
        )

        details: list[Any] = [cached.get(nid) for nid in nids]
        to_write: dict[str, Any] = {}
        for i, result in zip(miss_indexes, miss_results, strict=True):
            details[i] = result
            if isinstance(result, dict):
                to_write[nids[i]] = result

        if detail_cache is not None and to_write:
            await detail_cache.put_many(to_write)

        for info, detail in zip(report_infos, details, strict=True):
            raw_rating = None
            if isinstance(detail, dict):
                raw_rating = detail.get("rating")

            rating_label = normalize_rating_label(raw_rating)
            opinions["opinions"].append(
                {
                    "stock_name": info["stock_name"],
                    "title": info["title"],
                    "firm": info["firm"],
                    "date": info["date"],
                    "url": info["url"],
                    "target_price": detail.get("target_price")
                    if isinstance(detail, dict)
                    else None,
                    "rating": rating_label,
                    "rating_bucket": rating_to_bucket(rating_label),
                }
            )

    opinions["count"] = len(opinions["opinions"])
    opinions["consensus"] = build_consensus(
        opinions["opinions"], current_price, window_months=window_months
    )
    return opinions


def _parse_holding_rate(text: str | None) -> float | None:
    """Foreign holding RATE as a percent in [0, 100] (e.g. '47.73%' → 47.73).

    ROB-448: ``parse_korean_number`` divides by 100 on a trailing '%' (→0.4773), which
    would mis-scale a holding rate. Strip the '%' and parse the bare number instead.
    """
    if not text:
        return None
    cleaned = text.replace("%", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


#: 데스크톱 ``frgn.naver``는 Npay 증권 SPA로 바뀌어 수급 표가 사라졌다
#: (2026-09-25 관측). 같은 데이터를 모바일 증권 JSON에서 받는다.
NAVER_MOBILE_TREND_URL = "https://m.stock.naver.com/api/stock/{code}/trend"
#: 모바일 API가 한 번에 돌려주는 최대 행 수. 더 과거는 ``bizdate``로 넘긴다.
_TREND_PAGE_SIZE = 60
#: 네이버 등락 코드. 4=하한, 5=하락은 전일비가 음수다.
_FALLING_CODES = frozenset({"4", "5"})


async def _fetch_trend_page(
    code: str, *, page_size: int, bizdate: str | None
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"pageSize": page_size}
    if bizdate is not None:
        params["bizdate"] = bizdate
    url = NAVER_MOBILE_TREND_URL.format(code=code)
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=10) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _parse_trend_row(row: dict[str, Any]) -> dict[str, Any] | None:
    bizdate = str(row.get("bizdate") or "")
    if len(bizdate) != 8 or not bizdate.isdigit():
        return None
    close = _parse_korean_number(row.get("closePrice"))
    change = _parse_korean_number(row.get("compareToPreviousClosePrice"))
    direction = row.get("compareToPreviousPrice")
    if (
        change is not None
        and change > 0
        and isinstance(direction, dict)
        and str(direction.get("code")) in _FALLING_CODES
    ):
        change = -change
    base = close - change if close is not None and change is not None else None
    return {
        "date": f"{bizdate[:4]}-{bizdate[4:6]}-{bizdate[6:]}",
        "close": close,
        "change": change,
        # 기존 계약과 같이 비율(0.015 = 1.5%)로 돌려준다.
        "change_pct": change / base if base else None,
        "volume": _parse_korean_number(row.get("accumulatedTradingVolume")),
        "institutional_net": _parse_korean_number(row.get("organPureBuyQuant")),
        "foreign_net": _parse_korean_number(row.get("foreignerPureBuyQuant")),
        "individual_net": _parse_korean_number(row.get("individualPureBuyQuant")),
        # 모바일 API는 보유주수를 주지 않는다. 보유율은 0..100 퍼센트다.
        "foreign_holding_shares": None,
        "foreign_holding_rate": _parse_holding_rate(row.get("foreignerHoldRatio")),
    }


async def fetch_investor_trends(code: str, days: int = 20) -> dict[str, Any]:
    """Fetch daily foreign/institutional/individual net trades, newest first.

    URL: m.stock.naver.com/api/stock/{code}/trend (``bizdate`` pages backwards)

    Args:
        code: 6-digit Korean stock code
        days: Number of trading days of data to fetch

    Returns:
        Daily investor flow data (foreign, institutional, individual net trades)
    """
    trends: dict[str, Any] = {"symbol": code, "days": days, "data": []}
    bizdate: str | None = None
    while len(trends["data"]) < days:
        rows = await _fetch_trend_page(
            code,
            page_size=min(_TREND_PAGE_SIZE, days - len(trends["data"])),
            bizdate=bizdate,
        )
        next_bizdate = bizdate
        for row in rows:
            parsed = _parse_trend_row(row)
            if parsed is None:
                continue
            trends["data"].append(parsed)
            next_bizdate = str(row["bizdate"])
            if len(trends["data"]) >= days:
                break
        # 빈 페이지이거나 커서가 줄지 않으면 더 과거가 없다.
        if not rows or next_bizdate == bizdate:
            break
        bizdate = next_bizdate
    return trends


async def _fetch_report_detail(nid: str) -> dict[str, Any] | None:
    try:
        url = f"{NAVER_FINANCE_BASE}/research/company_read.naver"
        soup = await _fetch_html(url, params={"nid": nid})
        return _parse_report_detail_soup(soup)
    except Exception:
        return None


async def _fetch_report_detail_with_client(
    client: httpx.AsyncClient, nid: str
) -> dict[str, Any] | None:
    try:
        url = f"{NAVER_FINANCE_BASE}/research/company_read.naver"
        soup = await _fetch_html_with_client(client, url, params={"nid": nid})
        return _parse_report_detail_soup(soup)
    except Exception:
        return None


async def _fetch_current_price(code: str) -> int | None:
    """Fetch current stock price from Naver Finance main page.

    Args:
        code: 6-digit Korean stock code

    Returns:
        Current price as integer, or None if not found
    """
    try:
        url = f"{NAVER_FINANCE_ITEM}/main.naver"
        soup = await _fetch_html(url, params={"code": code})
        return _extract_current_price_from_main_soup(soup)
    except Exception:
        return None


async def fetch_investment_opinions(
    code: str,
    limit: int = 10,
    *,
    window_months: int = 12,
    detail_cache: DetailCachePort | None = None,
) -> dict[str, Any]:
    """Fetch securities firm investment opinions and target prices.

    URL: finance.naver.com/research/company_list.naver
    Individual reports: finance.naver.com/research/company_read.naver?nid={nid}

    Args:
        code: 6-digit Korean stock code
        limit: Maximum number of opinions to return
        window_months: ROB-486 컨센서스 recency 윈도우(개월). 윈도우 밖 date 의
            행은 집계 제외(rows_excluded_stale). 한 건이라도 윈도우 밖이면
            목표가/upside 집계는 null 이다(ROB-1300 — 잔여 행으로 조용히
            갱신하지 않음). undated 행은 fail-open 으로 유지(rows_undated
            카운트, ROB-488)되며, opinions 리스트 자체는 윈도우 밖 행도 포함한다.

    Returns:
        Investment opinions with normalized ratings and consensus statistics:
        - symbol: Stock code
        - count: Number of opinions
        - opinions: List of individual opinions with normalized ratings
        - consensus: Windowed aggregated statistics (buy/hold/sell counts,
          target prices, upside_pct + rows_total/rows_used/rows_excluded_stale/
          rows_undated/newest_opinion_date/window_months)
    """
    url = f"{NAVER_FINANCE_BASE}/research/company_list.naver"
    company_list_soup = await _fetch_html(
        url, params={"searchType": "itemCode", "itemCode": code}
    )
    current_price = await _fetch_current_price(code)
    return await _build_investment_opinions_from_company_list_soup(
        code,
        company_list_soup,
        limit,
        current_price=current_price,
        detail_fetcher=_fetch_report_detail,
        window_months=window_months,
        detail_cache=detail_cache,
    )


async def _fetch_kr_snapshot(
    code: str,
    *,
    news_limit: int = 5,
    opinion_limit: int = 10,
    detail_cache: DetailCachePort | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        main_url = f"{NAVER_FINANCE_ITEM}/main.naver"
        sise_url = f"{NAVER_FINANCE_ITEM}/sise.naver"
        news_url = f"{NAVER_FINANCE_ITEM}/news_news.naver"
        company_list_url = f"{NAVER_FINANCE_BASE}/research/company_list.naver"
        page_results = await asyncio.gather(
            _fetch_html_with_client(client, main_url, params={"code": code}),
            _fetch_html_with_client(client, sise_url, params={"code": code}),
            _fetch_html_with_client(
                client,
                news_url,
                params={"code": code, "page": "", "clusterId": ""},
            ),
            _fetch_html_with_client(
                client,
                company_list_url,
                params={"searchType": "itemCode", "itemCode": code},
            ),
            return_exceptions=True,
        )
        main_soup = (
            page_results[0] if isinstance(page_results[0], BeautifulSoup) else None
        )
        sise_soup = (
            page_results[1] if isinstance(page_results[1], BeautifulSoup) else None
        )
        news_soup = (
            page_results[2] if isinstance(page_results[2], BeautifulSoup) else None
        )
        company_list_soup = (
            page_results[3] if isinstance(page_results[3], BeautifulSoup) else None
        )

        snapshot: dict[str, Any] = {
            "valuation": None,
            "news": None,
            "opinions": None,
        }

        if main_soup is not None and sise_soup is not None:
            snapshot["valuation"] = _parse_valuation_from_soups(
                code, main_soup, sise_soup
            )

        if news_soup is not None:
            snapshot["news"] = _parse_news_soup(news_soup, news_limit)

        if company_list_soup is not None:
            current_price = (
                _extract_current_price_from_main_soup(main_soup)
                if main_soup is not None
                else None
            )
            snapshot[
                "opinions"
            ] = await _build_investment_opinions_from_company_list_soup(
                code,
                company_list_soup,
                opinion_limit,
                current_price=current_price,
                detail_fetcher=lambda nid: _fetch_report_detail_with_client(
                    client, nid
                ),
                detail_cache=detail_cache,
            )

        return snapshot
