"""공시 provider 의 `news_articles.feed_source` 단일 출처.

뉴스와 공시는 `news_articles` 한 테이블에 섞여 저장되고 `feed_source` 로만 구분된다.
모바일 와이어는 `kind = news | disclosure` 두 값뿐이므로 "어떤 provider 가 공시인가"를
읽기 API 와 수집기가 같은 목록으로 판단해야 한다. 그 목록이 여러 곳에 하드코딩되면
시장을 추가할 때마다 한 곳이 빠져 새 공시가 뉴스로 표시된다.
"""

from __future__ import annotations

DART_FEED_SOURCE = "dart"
"""한국 전자공시(OpenDART)."""

SEC_FEED_SOURCE = "sec"
"""미국 공시(SEC EDGAR)."""

DISCLOSURE_FEED_SOURCES: tuple[str, ...] = (DART_FEED_SOURCE, SEC_FEED_SOURCE)
"""공시로 취급하는 `feed_source` 전체.

여기에 없는 값(뉴스 provider, `NULL`)은 모두 `kind="news"` 다. SQL `IN` 절에 그대로
넘기므로 순서를 고정한 tuple 로 둔다.
"""
