"""SEC/DART 공시 원문을 제한적으로 가져와 텍스트로 정리한다."""

from __future__ import annotations

import html
import io
import re
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Comment, XMLParsedAsHTMLWarning

from app.core.config import settings
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
DART_DOCUMENT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DART_DOCUMENT_MAX_MEMBERS = 64
DART_DOCUMENT_MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
DART_DOCUMENT_MAX_MEMBER_BYTES = 8 * 1024 * 1024

_SEC_HOSTS = frozenset({"sec.gov", "www.sec.gov"})
_DART_HOSTS = frozenset({"dart.fss.or.kr"})
_DART_LANDING_PATH = "/dsaf001/main.do"
_DART_VIEWER_PATH = "/report/viewer.do"
_DART_USER_AGENT = "KAsset-Trader-Core disclosure-summary/1.0"
_OPENDART_DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
_DART_DOCUMENT_EXTENSIONS = frozenset(
    {".htm", ".html", ".txt", ".xhtml", ".xml"}
)
_OPENDART_ERROR_MESSAGES = {
    "010": "unregistered API key",
    "011": "unavailable API key",
    "012": "API access is not allowed from this address",
    "013": "no document is available",
    "014": "document file is unavailable",
    "020": "request limit exceeded",
    "100": "invalid request parameters",
    "800": "service maintenance",
    "900": "undefined service error",
}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DART_VIEWER_URL_RE = re.compile(
    r"(?P<url>(?:https://dart\.fss\.or\.kr)?/report/viewer\.do\?[^\"'<>\\\s]+)",
    re.IGNORECASE,
)
_SEC_EXHIBIT_LABEL_RE = re.compile(
    r"(?:\bex(?:hibit)?\s*99[.\-]?[12]\b|\b99[.\-][12]\b|press\s+release|cfo\s+commentary)",
    re.IGNORECASE,
)
_CORRECTION_TEXT_MARKERS = (
    "정정 전",
    "정정전",
    "정정 후",
    "정정후",
    "정정사유",
    "정정 사유",
    "정정사항",
    "정정 사항",
)
_MATERIAL_TEXT_MARKERS = _CORRECTION_TEXT_MARKERS + (
    "계약금액",
    "계약기간",
    "매출액",
    "영업이익",
    "당기순이익",
    "발행금액",
    "발행가액",
    "증자",
    "전환가액",
    "합병비율",
    "분할비율",
    "변경 전",
    "변경전",
    "변경 후",
    "변경후",
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


def _sec_material_exhibit_url(document: _FetchedHtml) -> str | None:
    """8-K의 같은 filing 디렉터리에 있는 99.1/99.2 자료 중 99.1을 우선한다."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(document.body, "lxml")
    visible_text = " ".join(soup.stripped_strings)
    if re.search(r"\bFORM\s*8-K\b", visible_text, re.IGNORECASE) is None:
        return None

    parent = urlsplit(document.url)
    parent_directory = parent.path.rsplit("/", 1)[0]
    candidates: list[tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        label = " ".join(anchor.stripped_strings)
        searchable = f"{label} {href}"
        if _SEC_EXHIBIT_LABEL_RE.search(searchable) is None:
            continue
        candidate = html.unescape(urljoin(document.url, href.strip()))
        try:
            if _provider_for_url(candidate) != "sec":
                continue
        except DisclosureContentError:
            continue
        parsed = urlsplit(candidate)
        if parsed.path.rsplit("/", 1)[0] != parent_directory:
            continue
        if parsed.path == parent.path or not parsed.path.lower().endswith(
            (".htm", ".html")
        ):
            continue
        normalized = "".join(searchable.lower().split())
        priority = 0 if ("99.1" in normalized or "pressrelease" in normalized) else 1
        candidates.append((priority, candidate))
    return min(candidates, default=(2, ""))[1] or None


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


def _dart_receipt_number(landing: _FetchedHtml) -> str:
    values = parse_qs(
        urlsplit(landing.url).query,
        keep_blank_values=True,
    ).get("rcpNo", ())
    if len(values) != 1 or re.fullmatch(r"[0-9]+", values[0]) is None:
        raise DisclosureContentError(
            "DART landing URL must contain one numeric rcpNo"
        )
    return values[0]


async def _read_bounded_document_response(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = -1
        if declared_length > DART_DOCUMENT_MAX_RESPONSE_BYTES:
            raise DisclosureContentError(
                "OpenDART document response exceeds the size limit"
            )

    buffer = io.BytesIO()
    consumed = 0
    async for chunk in response.aiter_bytes():
        consumed += len(chunk)
        if consumed > DART_DOCUMENT_MAX_RESPONSE_BYTES:
            raise DisclosureContentError(
                "OpenDART document response exceeds the size limit"
            )
        buffer.write(chunk)
    if consumed == 0:
        raise DisclosureContentError("OpenDART document response body is empty")
    return buffer.getvalue()


def _opendart_api_error(payload: bytes) -> DisclosureContentError | None:
    if payload.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return None
    text = payload[:16_384].decode("utf-8", errors="replace")
    status_match = re.search(
        r"<status>\s*([0-9]{3})\s*</status>",
        text,
        re.IGNORECASE,
    )
    message_match = re.search(r"<message(?:\s[^>]*)?>", text, re.IGNORECASE)
    if status_match is None and message_match is None:
        return None
    status = status_match.group(1) if status_match is not None else "unknown"
    reason = _OPENDART_ERROR_MESSAGES.get(status, "service rejected the request")
    return DisclosureContentError(
        f"OpenDART document API error: {reason} (status {status})"
    )


async def _fetch_opendart_document(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    rcept_no: str,
) -> bytes:
    request = client.build_request(
        "GET",
        _OPENDART_DOCUMENT_URL,
        params={"crtfc_key": api_key, "rcept_no": rcept_no},
        headers={
            "Accept": "application/zip,application/xml;q=0.9",
            "User-Agent": _DART_USER_AGENT,
        },
    )
    try:
        response = await client.send(
            request,
            stream=True,
            follow_redirects=False,
        )
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        raise DisclosureContentError(
            f"OpenDART document fetch failed: {type(exc).__name__}"
        ) from None

    try:
        if response.status_code in _REDIRECT_STATUSES:
            raise DisclosureContentError(
                "OpenDART document API redirects are not allowed"
            )
        if not response.is_success:
            raise DisclosureContentError(
                f"OpenDART document fetch returned HTTP {response.status_code}"
            )
        payload = await _read_bounded_document_response(response)
    finally:
        await response.aclose()

    api_error = _opendart_api_error(payload)
    if api_error is not None:
        raise api_error
    return payload


def _safe_document_member(info: zipfile.ZipInfo) -> bool:
    normalized_name = info.filename.replace("\\", "/")
    path = PurePosixPath(normalized_name)
    if (
        not normalized_name
        or "\x00" in normalized_name
        or normalized_name.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized_name) is not None
        or ".." in path.parts
    ):
        raise DisclosureContentError(
            "OpenDART document ZIP contains an unsafe member path"
        )
    if info.flag_bits & 0x1:
        raise DisclosureContentError(
            "OpenDART document ZIP contains an encrypted member"
        )
    return not info.is_dir() and path.suffix.lower() in _DART_DOCUMENT_EXTENSIONS


def _read_document_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> bytes:
    if info.file_size > DART_DOCUMENT_MAX_MEMBER_BYTES:
        raise DisclosureContentError(
            "OpenDART document ZIP member exceeds the size limit"
        )
    chunks: list[bytes] = []
    consumed = 0
    try:
        with archive.open(info, "r") as member:
            while True:
                chunk = member.read(
                    min(
                        64 * 1024,
                        DART_DOCUMENT_MAX_MEMBER_BYTES + 1 - consumed,
                    )
                )
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > DART_DOCUMENT_MAX_MEMBER_BYTES:
                    raise DisclosureContentError(
                        "OpenDART document ZIP member exceeds the size limit"
                    )
                chunks.append(chunk)
    except DisclosureContentError:
        raise
    except (EOFError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise DisclosureContentError("OpenDART document ZIP is invalid") from exc
    return b"".join(chunks)


def _decode_document_member(payload: bytes) -> str:
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig", errors="replace")
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", errors="replace")

    declaration = re.search(
        br"(?:encoding|charset)\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
        payload[:1024],
        re.IGNORECASE,
    )
    aliases = {
        "utf-8": "utf-8",
        "utf8": "utf-8",
        "euc-kr": "euc-kr",
        "euckr": "euc-kr",
        "cp949": "cp949",
        "ks_c_5601-1987": "cp949",
    }
    encodings: list[str] = []
    if declaration is not None:
        declared = declaration.group(1).decode("ascii").lower()
        encoding = aliases.get(declared)
        if encoding is not None:
            encodings.append(encoding)
    encodings.extend(
        encoding
        for encoding in ("utf-8", "cp949")
        if encoding not in encodings
    )
    for encoding in encodings:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _zip_member_count(payload: bytes) -> int:
    signature = b"PK\x05\x06"
    search_start = max(0, len(payload) - (65_535 + 22))
    search_end = len(payload)
    while search_end > search_start:
        offset = payload.rfind(signature, search_start, search_end)
        if offset < 0:
            break
        if offset + 22 <= len(payload):
            comment_length = int.from_bytes(
                payload[offset + 20 : offset + 22],
                "little",
            )
            if offset + 22 + comment_length == len(payload):
                disk_number = int.from_bytes(
                    payload[offset + 4 : offset + 6],
                    "little",
                )
                central_directory_disk = int.from_bytes(
                    payload[offset + 6 : offset + 8],
                    "little",
                )
                entries_on_disk = int.from_bytes(
                    payload[offset + 8 : offset + 10],
                    "little",
                )
                total_entries = int.from_bytes(
                    payload[offset + 10 : offset + 12],
                    "little",
                )
                central_directory_size = int.from_bytes(
                    payload[offset + 12 : offset + 16],
                    "little",
                )
                central_directory_offset = int.from_bytes(
                    payload[offset + 16 : offset + 20],
                    "little",
                )
                if total_entries > DART_DOCUMENT_MAX_MEMBERS:
                    raise DisclosureContentError(
                        "OpenDART document ZIP has too many members"
                    )
                if (
                    disk_number != 0
                    or central_directory_disk != 0
                    or entries_on_disk != total_entries
                    or central_directory_offset + central_directory_size != offset
                ):
                    raise DisclosureContentError(
                        "OpenDART document ZIP is invalid"
                    )

                cursor = central_directory_offset
                parsed_entries = 0
                while cursor < offset:
                    if (
                        cursor + 46 > offset
                        or payload[cursor : cursor + 4] != b"PK\x01\x02"
                    ):
                        raise DisclosureContentError(
                            "OpenDART document ZIP is invalid"
                        )
                    variable_length = sum(
                        int.from_bytes(
                            payload[cursor + start : cursor + start + 2],
                            "little",
                        )
                        for start in (28, 30, 32)
                    )
                    cursor += 46 + variable_length
                    parsed_entries += 1
                    if parsed_entries > DART_DOCUMENT_MAX_MEMBERS:
                        raise DisclosureContentError(
                            "OpenDART document ZIP has too many members"
                        )
                if cursor != offset or parsed_entries != total_entries:
                    raise DisclosureContentError(
                        "OpenDART document ZIP is invalid"
                    )
                return total_entries
        search_end = offset
    raise DisclosureContentError("OpenDART document ZIP is invalid")


def _disclosure_text_score(text: str) -> int:
    correction_hits = sum(
        text.count(marker) for marker in _CORRECTION_TEXT_MARKERS
    )
    material_hits = sum(text.count(marker) for marker in _MATERIAL_TEXT_MARKERS)
    numeric_rows = sum(
        1
        for line in text.splitlines()
        if "|" in line and re.search(r"\d", line) is not None
    )
    return correction_hits * 100 + material_hits * 10 + numeric_rows


def _extract_opendart_document_text(payload: bytes, *, max_chars: int) -> str:
    if not payload.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        raise DisclosureContentError(
            "OpenDART document response is not a ZIP archive"
        )
    declared_members = _zip_member_count(payload)
    if declared_members > DART_DOCUMENT_MAX_MEMBERS:
        raise DisclosureContentError(
            "OpenDART document ZIP has too many members"
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        infos = archive.infolist()
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise DisclosureContentError("OpenDART document ZIP is invalid") from exc

    with archive:
        if (
            len(infos) != declared_members
            or len(infos) > DART_DOCUMENT_MAX_MEMBERS
        ):
            raise DisclosureContentError(
                "OpenDART document ZIP has too many members"
            )
        total_uncompressed = sum(info.file_size for info in infos)
        if total_uncompressed > DART_DOCUMENT_MAX_UNCOMPRESSED_BYTES:
            raise DisclosureContentError(
                "OpenDART document ZIP exceeds the uncompressed size limit"
            )

        documents: list[tuple[int, int, str, str]] = []
        for info in infos:
            if not _safe_document_member(info):
                continue
            member_payload = _read_document_member(archive, info)
            try:
                member_text = extract_disclosure_text(
                    _decode_document_member(member_payload),
                    max_chars=max_chars,
                    prioritize_material=True,
                )
            except DisclosureContentError:
                continue
            documents.append(
                (
                    _disclosure_text_score(member_text),
                    info.file_size,
                    info.filename,
                    member_text,
                )
            )

    documents.sort(key=lambda item: (-item[0], -item[1], item[2]))
    parts: list[str] = []
    consumed_chars = 0
    seen: set[str] = set()
    for _score, _size, _name, member_text in documents:
        if member_text in seen:
            continue
        seen.add(member_text)
        separator_chars = 2 if parts else 0
        remaining = max_chars - consumed_chars - separator_chars
        if remaining < MIN_TEXT_CHARS:
            break
        selected_text = member_text[:remaining].rstrip()
        if len(selected_text) < MIN_TEXT_CHARS:
            continue
        parts.append(selected_text)
        consumed_chars += separator_chars + len(selected_text)

    text = "\n\n".join(parts)
    if len(text) < MIN_TEXT_CHARS:
        raise DisclosureContentError(
            "OpenDART document ZIP has no usable disclosure text"
        )
    return text[:max_chars].rstrip()


def _replace_tables_with_rows(soup: BeautifulSoup) -> None:
    for table in soup.find_all("table"):
        row_lines: list[str] = []
        for row in table.find_all("tr"):
            cells = [
                " ".join(cell.get_text(" ", strip=True).split())
                for cell in row.find_all(["th", "td"], recursive=False)
            ]
            cells = [cell for cell in cells if cell]
            if cells:
                row_lines.append(" | ".join(cells))
        if not row_lines:
            continue
        replacement = soup.new_tag("div")
        for row_line in row_lines:
            line = soup.new_tag("p")
            line.string = row_line
            replacement.append(line)
        table.replace_with(replacement)


def _prioritized_disclosure_lines(lines: list[str]) -> list[str]:
    unique_lines: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line or line in seen:
            continue
        seen.add(line)
        unique_lines.append(line)

    correction_hits: list[int] = []
    material_hits: list[int] = []
    context: set[int] = set()
    for index, line in enumerate(unique_lines):
        if any(marker in line for marker in _CORRECTION_TEXT_MARKERS):
            correction_hits.append(index)
            context.update(
                range(max(0, index - 1), min(len(unique_lines), index + 2))
            )
        if any(marker in line for marker in _MATERIAL_TEXT_MARKERS):
            material_hits.append(index)
            context.update(
                range(max(0, index - 1), min(len(unique_lines), index + 2))
            )
    direct_hits = set(correction_hits) | set(material_hits)
    priority = [
        *dict.fromkeys(correction_hits),
        *(
            index
            for index in dict.fromkeys(material_hits)
            if index not in set(correction_hits)
        ),
        *(index for index in sorted(context) if index not in direct_hits),
    ]
    priority_set = set(priority)
    return [
        *(unique_lines[index] for index in priority),
        *(
            line
            for index, line in enumerate(unique_lines)
            if index not in priority_set
        ),
    ]


def extract_disclosure_text(
    html_body: str,
    *,
    max_chars: int = MAX_TEXT_CHARS,
    prioritize_material: bool = False,
) -> str:
    """숨은 노이즈를 제거하고 표 행을 보존한 제한 길이 공시 본문을 반환한다."""
    if max_chars < MIN_TEXT_CHARS:
        raise ValueError(f"max_chars must be at least {MIN_TEXT_CHARS}")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html_body, "lxml")
    for tag_name in (
        "ix:header",
        "ix:hidden",
        "ix:references",
        "ix:resources",
        "xbrli:context",
        "xbrli:unit",
    ):
        for element in soup.find_all(tag_name):
            element.decompose()
    for element in soup(["script", "style", "noscript", "template", "svg"]):
        element.decompose()
    for element in soup.find_all(attrs={"hidden": True}):
        if element.parent is not None:
            element.decompose()
    for element in soup.find_all(style=True):
        style = "".join(str(element.get("style", "")).lower().split())
        if element.parent is not None and (
            "display:none" in style or "visibility:hidden" in style
        ):
            element.decompose()
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    _replace_tables_with_rows(soup)
    lines = [" ".join(value.split()) for value in soup.stripped_strings]
    if prioritize_material:
        lines = _prioritized_disclosure_lines(lines)
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
        exhibit: _FetchedHtml | None = None
        if provider == "dart" and urlsplit(document.url).path == _DART_LANDING_PATH:
            try:
                viewer_url = _dart_viewer_url(document)
            except DisclosureContentError:
                viewer_url = None

            rcept_no = _dart_receipt_number(document)
            api_key = (settings.opendart_api_key or "").strip()
            document_error: DisclosureContentError | None = None
            if api_key:
                try:
                    payload = await _fetch_opendart_document(
                        self._client,
                        api_key=api_key,
                        rcept_no=rcept_no,
                    )
                    return _extract_opendart_document_text(
                        payload,
                        max_chars=self._max_text_chars,
                    )
                except DisclosureContentError as exc:
                    document_error = exc

            if viewer_url is None:
                if document_error is not None:
                    raise document_error
                raise DisclosureContentError(
                    "OPENDART_API_KEY is required for DART document fallback"
                )
            document = await _fetch_html(
                self._client,
                viewer_url,
                sec_user_agent=self._sec_user_agent,
                sec_rate_limiter=self._sec_rate_limiter,
                referer=document.url,
            )
        elif provider == "sec":
            exhibit_url = _sec_material_exhibit_url(document)
            if exhibit_url is not None:
                try:
                    exhibit = await _fetch_html(
                        self._client,
                        exhibit_url,
                        sec_user_agent=self._sec_user_agent,
                        sec_rate_limiter=self._sec_rate_limiter,
                        referer=document.url,
                    )
                except DisclosureContentError:
                    exhibit = None

        primary_text = extract_disclosure_text(
            document.body,
            max_chars=self._max_text_chars,
            prioritize_material=provider == "dart",
        )
        remaining = self._max_text_chars - len(primary_text) - 2
        if exhibit is None or remaining < MIN_TEXT_CHARS:
            return primary_text
        try:
            exhibit_text = extract_disclosure_text(exhibit.body, max_chars=remaining)
        except DisclosureContentError:
            return primary_text
        return f"{primary_text}\n\n{exhibit_text}"


__all__ = [
    "DisclosureContentError",
    "DisclosureTextFetcher",
    "MAX_TEXT_CHARS",
    "extract_disclosure_text",
]
