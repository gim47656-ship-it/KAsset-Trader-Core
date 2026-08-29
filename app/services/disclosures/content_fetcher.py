"""SEC/DART 공시 원문을 제한적으로 가져와 텍스트로 정리한다."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Comment

from app.services.disclosures.sec_edgar import (
    SecEdgarError,
    SecRateLimiter,
    get_shared_sec_rate_limiter,
    resolve_sec_user_agent,
)

MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 16_000
MIN_TEXT_CHARS = 40

_SEC_HOSTS = frozenset({"sec.gov", "www.sec.gov"})
_DART_HOSTS = frozenset({"dart.fss.or.kr"})
_DART_LANDING_PATH = "/dsaf001/main.do"
_DART_VIEWER_PATH = "/report/viewer.do"
_DART_USER_AGENT = "KAsset-Trader-Core disclosure-summary/1.0"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DART_VIEWER_URL_RE = re.compile(
    r"(?P<url>(?:https://dart\.fss\.or\.kr)?/report/viewer\.do\?[^\"'<>\\\s]+)",
    re.IGNORECASE,
)


class DisclosureContentError(RuntimeError):
    """공시 원문을 안전하게 읽거나 본문으로 정리할 수 없을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class _FetchedHtml:
    url: str
    body: str


def _provider_for_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DisclosureContentError("invalid disclosure URL") from exc

    if parsed.scheme.lower() != "https":
        raise DisclosureContentError("disclosure URL scheme must be https")
    if parsed.username is not None or parsed.password is not None:
        raise DisclosureContentError("disclosure URL credentials are not allowed")
    if port not in (None, 443):
        raise DisclosureContentError("disclosure URL port is not allowed")
    if parsed.fragment:
        raise DisclosureContentError("disclosure URL fragment is not allowed")

    host = (parsed.hostname or "").lower().rstrip(".")
    if host in _SEC_HOSTS:
        if not parsed.path.startswith("/Archives/edgar/data/"):
            raise DisclosureContentError("SEC disclosure path is not allowed")
        return "sec"
    if host in _DART_HOSTS:
        if parsed.path not in {_DART_LANDING_PATH, _DART_VIEWER_PATH}:
            raise DisclosureContentError("DART disclosure path is not allowed")
        return "dart"
    raise DisclosureContentError("disclosure URL host is not allowed")


def _decode_body(response: httpx.Response, payload: bytes) -> str:
    encoding = response.encoding or "utf-8"
    try:
        return payload.decode(encoding, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


async def _read_limited_body(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    consumed = 0
    async for chunk in response.aiter_bytes():
        remaining = MAX_RESPONSE_BYTES - consumed
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        consumed += min(len(chunk), remaining)
        if consumed >= MAX_RESPONSE_BYTES:
            break
    return b"".join(chunks)


def _headers_for(
    provider: str,
    *,
    sec_user_agent: str | None,
    referer: str | None,
) -> dict[str, str]:
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8"
        ),
    }
    if provider == "sec":
        try:
            headers["User-Agent"] = resolve_sec_user_agent(sec_user_agent)
        except SecEdgarError as exc:
            raise DisclosureContentError(str(exc)) from exc
    else:
        headers["User-Agent"] = _DART_USER_AGENT
        if referer is not None:
            headers["Referer"] = referer
    return headers


async def _fetch_html(
    client: httpx.AsyncClient,
    url: str,
    *,
    sec_user_agent: str | None,
    sec_rate_limiter: SecRateLimiter | None,
    referer: str | None = None,
) -> _FetchedHtml:
    current_url = url
    current_referer = referer
    for redirect_count in range(MAX_REDIRECTS + 1):
        provider = _provider_for_url(current_url)
        request = client.build_request(
            "GET",
            current_url,
            headers=_headers_for(
                provider,
                sec_user_agent=sec_user_agent,
                referer=current_referer,
            ),
        )
        if provider == "sec":
            limiter = sec_rate_limiter or await get_shared_sec_rate_limiter()
            await limiter.acquire()
        try:
            response = await client.send(
                request,
                stream=True,
                follow_redirects=False,
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise DisclosureContentError(
                f"disclosure fetch failed: {type(exc).__name__}"
            ) from exc

        try:
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise DisclosureContentError(
                        "disclosure redirect is missing Location"
                    )
                if redirect_count >= MAX_REDIRECTS:
                    raise DisclosureContentError("too many disclosure redirects")
                next_url = urljoin(str(response.url), location)
                if _provider_for_url(next_url) != provider:
                    raise DisclosureContentError(
                        "cross-provider disclosure redirect is not allowed"
                    )
                current_referer = str(response.url)
                current_url = next_url
                continue
            if not response.is_success:
                raise DisclosureContentError(
                    f"disclosure fetch returned HTTP {response.status_code}"
                )
            payload = await _read_limited_body(response)
            if not payload:
                raise DisclosureContentError("disclosure response body is empty")
            return _FetchedHtml(
                url=str(response.url),
                body=_decode_body(response, payload),
            )
        finally:
            await response.aclose()

    raise DisclosureContentError("too many disclosure redirects")


def _javascript_value(source: str, name: str) -> str | None:
    pattern = re.compile(
        rf"(?:[\"']?{re.escape(name)}[\"']?)\s*[:=]\s*[\"']([^\"']+)[\"']",
        re.IGNORECASE,
    )
    match = pattern.search(source)
    return html.unescape(match.group(1)).strip() if match else None


def _dart_viewer_url(landing: _FetchedHtml) -> str:
    soup = BeautifulSoup(landing.body, "lxml")
    for element in soup.select("iframe[src], frame[src], a[href]"):
        value = element.get("src") or element.get("href")
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = html.unescape(urljoin(landing.url, value.strip()))
        if urlsplit(candidate).path != _DART_VIEWER_PATH:
            continue
        _provider_for_url(candidate)
        return candidate

    direct_match = _DART_VIEWER_URL_RE.search(landing.body)
    if direct_match is not None:
        candidate = html.unescape(urljoin(landing.url, direct_match.group("url")))
        _provider_for_url(candidate)
        return candidate

    query = parse_qs(urlsplit(landing.url).query)
    rcp_no = _javascript_value(landing.body, "rcpNo") or next(
        iter(query.get("rcpNo", ())),
        None,
    )
    dcm_no = _javascript_value(landing.body, "dcmNo")
    if rcp_no is None or dcm_no is None or not rcp_no.isdigit() or not dcm_no.isdigit():
        raise DisclosureContentError("DART landing page has no safe viewer URL")

    viewer_query: dict[str, str] = {"rcpNo": rcp_no, "dcmNo": dcm_no}
    for name in ("eleId", "offset", "length"):
        value = _javascript_value(landing.body, name)
        if value is not None:
            if not value.isdigit():
                raise DisclosureContentError("DART viewer numeric parameter is invalid")
            viewer_query[name] = value
    dtd = _javascript_value(landing.body, "dtd")
    if dtd is not None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+", dtd) is None:
            raise DisclosureContentError("DART viewer dtd parameter is invalid")
        viewer_query["dtd"] = dtd

    candidate = urljoin(
        landing.url,
        f"{_DART_VIEWER_PATH}?{urlencode(viewer_query)}",
    )
    _provider_for_url(candidate)
    return candidate


def extract_disclosure_text(
    html_body: str,
    *,
    max_chars: int = MAX_TEXT_CHARS,
) -> str:
    """스크립트/스타일을 제거하고 공백을 정규화한 제한 길이 본문을 반환한다."""
    if max_chars < MIN_TEXT_CHARS:
        raise ValueError(f"max_chars must be at least {MIN_TEXT_CHARS}")

    soup = BeautifulSoup(html_body, "lxml")
    for element in soup(["script", "style", "noscript", "template", "svg"]):
        element.decompose()
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    lines = [" ".join(value.split()) for value in soup.stripped_strings]
    text = "\n".join(value for value in lines if value).strip()
    if len(text) < MIN_TEXT_CHARS:
        raise DisclosureContentError("disclosure body text is missing or too short")
    return text[:max_chars].rstrip()


class DisclosureTextFetcher:
    """주입된 HTTP 클라이언트로 SEC/DART 본문만 읽는다."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        sec_user_agent: str | None = None,
        sec_rate_limiter: SecRateLimiter | None = None,
        max_text_chars: int = MAX_TEXT_CHARS,
    ) -> None:
        self._client = client
        self._sec_user_agent = sec_user_agent
        self._sec_rate_limiter = sec_rate_limiter
        self._max_text_chars = max_text_chars

    async def fetch(self, url: str) -> str:
        provider = _provider_for_url(url)
        document = await _fetch_html(
            self._client,
            url,
            sec_user_agent=self._sec_user_agent,
            sec_rate_limiter=self._sec_rate_limiter,
        )
        if provider == "dart" and urlsplit(document.url).path == _DART_LANDING_PATH:
            viewer_url = _dart_viewer_url(document)
            document = await _fetch_html(
                self._client,
                viewer_url,
                sec_user_agent=self._sec_user_agent,
                sec_rate_limiter=self._sec_rate_limiter,
                referer=document.url,
            )
        return extract_disclosure_text(
            document.body,
            max_chars=self._max_text_chars,
        )


__all__ = [
    "DisclosureContentError",
    "DisclosureTextFetcher",
    "MAX_TEXT_CHARS",
    "extract_disclosure_text",
]
