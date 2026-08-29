"""SEC/DART 공시 원문 fetch와 본문 정리 경계 테스트."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from app.core.config import settings
from app.services.disclosures.content_fetcher import (
    DART_DOCUMENT_MAX_RESPONSE_BYTES,
    DisclosureContentError,
    DisclosureTextFetcher,
    extract_disclosure_text,
)


class FakeRateLimiter:
    def __init__(self) -> None:
        self.acquire_count = 0

    async def acquire(self) -> None:
        self.acquire_count += 1


def _document_zip(
    body: bytes,
    *,
    name: str = "20260828001916.xml",
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, body)
    return buffer.getvalue()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://www.sec.gov/Archives/edgar/data/1/report.htm",
        "https://www.sec.gov.evil.test/Archives/edgar/data/1/report.htm",
        "https://127.0.0.1/Archives/edgar/data/1/report.htm",
        "https://dart.fss.or.kr/private/admin",
    ],
)
async def test_fetch_rejects_non_allowlisted_url_before_http(url: str) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="should not be called", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DisclosureContentError):
            await DisclosureTextFetcher(
                client,
                sec_user_agent="KAsset tests test@example.com",
            ).fetch(url)

    assert calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_revalidates_redirect_host_before_following() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://169.254.169.254/latest/meta-data/"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DisclosureContentError, match="host is not allowed"):
            await DisclosureTextFetcher(
                client,
                sec_user_agent="KAsset tests test@example.com",
            ).fetch("https://www.sec.gov/Archives/edgar/data/1/report.htm")

    assert calls == ["https://www.sec.gov/Archives/edgar/data/1/report.htm"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sec_fetch_sends_contract_user_agent_and_limits_clean_text() -> None:
    seen_user_agent: str | None = None
    limiter = FakeRateLimiter()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_user_agent
        seen_user_agent = request.headers.get("user-agent")
        body = """
        <html><head><style>.secret { display:none }</style></head>
        <body><script>window.secret = 999;</script>
        <h1>Quarterly report</h1>
        <p>Revenue was 123 million dollars.</p>
        <p>The company maintained its existing guidance for the year.</p>
        </body></html>
        """
        return httpx.Response(
            200,
            text=body,
            headers={"Content-Type": "text/html; charset=utf-8"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await DisclosureTextFetcher(
            client,
            sec_user_agent="KAsset tests test@example.com",
            sec_rate_limiter=limiter,
            max_text_chars=80,
        ).fetch("https://www.sec.gov/Archives/edgar/data/1/report.htm")

    assert limiter.acquire_count == 1
    assert seen_user_agent == "KAsset tests test@example.com"
    assert "window.secret" not in text
    assert "display:none" not in text
    assert text.startswith("Quarterly report\nRevenue was 123 million dollars.")
    assert len(text) <= 80


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sec_8k_appends_same_filing_press_release_exhibit() -> None:
    calls: list[tuple[str, str | None]] = []
    limiter = FakeRateLimiter()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("referer")))
        if request.url.path.endswith("/report.htm"):
            return httpx.Response(
                200,
                text=(
                    "<html><body><h1>FORM 8-K</h1>"
                    "<p>Item 2.02 Results of Operations and Financial Condition.</p>"
                    '<a href="report-ex99-1.htm">Exhibit 99.1 Press Release</a>'
                    "</body></html>"
                ),
                request=request,
            )
        return httpx.Response(
            200,
            text=(
                "<html><body><h1>Quarterly results</h1>"
                "<p>Revenue was 30 billion dollars and net income was 12 billion dollars.</p>"
                "</body></html>"
            ),
            request=request,
        )

    report_url = "https://www.sec.gov/Archives/edgar/data/1/filing/report.htm"
    exhibit_url = "https://www.sec.gov/Archives/edgar/data/1/filing/report-ex99-1.htm"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await DisclosureTextFetcher(
            client,
            sec_user_agent="KAsset tests test@example.com",
            sec_rate_limiter=limiter,
            max_text_chars=300,
        ).fetch(report_url)

    assert "Item 2.02 Results of Operations" in text
    assert "Revenue was 30 billion dollars" in text
    assert limiter.acquire_count == 2
    assert calls == [(report_url, None), (exhibit_url, report_url)]


@pytest.mark.unit
def test_extract_disclosure_text_drops_inline_xbrl_hidden_header() -> None:
    text = extract_disclosure_text(
        """
        <?xml version="1.0" encoding="utf-8"?>
        <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
          <body>
            <ix:header style="display:none">
              <ix:resources>
                <xbrli:context>0001045810 2026-07-26 us-gaap:Revenue</xbrli:context>
              </ix:resources>
              <ix:hidden>999999 hidden metadata</ix:hidden>
            </ix:header>
            <div hidden>888888 hidden attribute</div>
            <div style="visibility: hidden">777777 hidden style</div>
            <h1>Quarterly report</h1>
            <p>Revenue increased to 30 billion dollars in the quarter.</p>
          </body>
        </html>
        """
    )

    assert text == (
        "Quarterly report\nRevenue increased to 30 billion dollars in the quarter."
    )
    assert "0001045810" not in text
    assert "999999" not in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dart_landing_resolves_viewer_and_extracts_actual_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(settings, "opendart_api_key", "")

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("referer")))
        if request.url.path == "/dsaf001/main.do":
            return httpx.Response(
                200,
                text=(
                    '<html><body><iframe src="/report/viewer.do?rcpNo=20260829000001'
                    '&amp;dcmNo=12345&amp;eleId=1"></iframe></body></html>'
                ),
                request=request,
            )
        return httpx.Response(
            200,
            text=(
                "<html><body><h1>주요사항보고서</h1>"
                "<p>회사는 신규 공급 계약을 체결했다고 공시했다.</p>"
                "<script>가짜 숫자 777</script>"
                "<p>계약 상대방과 일정은 원문 표에 기재되어 있다.</p></body></html>"
            ),
            request=request,
        )

    landing_url = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260829000001"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await DisclosureTextFetcher(client).fetch(landing_url)

    assert "신규 공급 계약" in text
    assert "가짜 숫자" not in text
    assert calls == [
        (landing_url, None),
        (
            "https://dart.fss.or.kr/report/viewer.do?rcpNo=20260829000001&dcmNo=12345&eleId=1",
            landing_url,
        ),
    ]


@pytest.mark.unit
def test_extract_disclosure_text_rejects_empty_shell() -> None:
    with pytest.raises(DisclosureContentError, match="missing or too short"):
        extract_disclosure_text(
            "<html><style>body{}</style><script>alert(1)</script><body>공시</body></html>"
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dart_landing_without_viewer_uses_opendart_document_zip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "test-opendart-secret"
    monkeypatch.setattr(settings, "opendart_api_key", api_key)
    landing_url = (
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260828001916"
    )
    document = (
        '<?xml version="1.0" encoding="EUC-KR"?>'
        "<DOCUMENT><TITLE>주요사항보고서</TITLE>"
        "<P>회사는 대규모 한국어 공급 계약을 체결했다고 공시했습니다.</P>"
        "<P>계약 금액과 이행 기간은 공시 원문에 기재되어 있습니다.</P></DOCUMENT>"
    ).encode("cp949")
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, request.url.path))
        if request.url.host == "dart.fss.or.kr":
            return httpx.Response(
                200,
                text="<html><body>DART landing without viewer metadata</body></html>",
                request=request,
            )
        assert request.method == "GET"
        assert request.url.host == "opendart.fss.or.kr"
        assert request.url.path == "/api/document.xml"
        assert request.url.params["crtfc_key"] == api_key
        assert request.url.params["rcept_no"] == "20260828001916"
        return httpx.Response(
            200,
            content=_document_zip(document),
            headers={"Content-Type": "application/zip"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await DisclosureTextFetcher(client, max_text_chars=80).fetch(
            landing_url
        )

    assert "대규모 한국어 공급 계약" in text
    assert "계약 금액과 이행 기간" in text
    assert len(text) <= 80
    assert calls == [
        ("dart.fss.or.kr", "/dsaf001/main.do"),
        ("opendart.fss.or.kr", "/api/document.xml"),
    ]

@pytest.mark.unit
@pytest.mark.asyncio
async def test_dart_zip_prioritizes_correction_table_before_long_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "opendart_api_key", "test-opendart-secret")
    landing_url = (
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260829009999"
    )
    noise = "반복 안내 문구와 서식 설명 " * 600
    document = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<DOCUMENT><TITLE>[기재정정] 단일판매ㆍ공급계약체결</TITLE>"
        f"<P>{noise}</P>"
        "<TABLE>"
        "<TR><TH>정정사유</TH><TD>계약금액 변경</TD></TR>"
        "<TR><TH>항목</TH><TH>정정 전</TH><TH>정정 후</TH></TR>"
        "<TR><TD>계약금액</TD><TD>12,000,000,000원</TD>"
        "<TD>18,000,000,000원</TD></TR>"
        "<TR><TD>계약기간</TD><TD>2026-09-01</TD><TD>2029-08-31</TD></TR>"
        "</TABLE></DOCUMENT>"
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "dart.fss.or.kr":
            return httpx.Response(
                200,
                text="<html><body>DART landing without viewer metadata</body></html>",
                request=request,
            )
        return httpx.Response(
            200,
            content=_document_zip(document),
            headers={"Content-Type": "application/zip"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await DisclosureTextFetcher(client, max_text_chars=500).fetch(
            landing_url
        )

    assert text.startswith("정정사유 | 계약금액 변경")
    assert "항목 | 정정 전 | 정정 후" in text
    assert "계약금액 | 12,000,000,000원 | 18,000,000,000원" in text
    assert "계약기간 | 2026-09-01 | 2029-08-31" in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dart_document_fallback_maps_api_error_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "secret-must-not-appear"
    receipt_no = "20260828001916"
    monkeypatch.setattr(settings, "opendart_api_key", api_key)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "dart.fss.or.kr":
            return httpx.Response(200, text="<html>no viewer</html>", request=request)
        return httpx.Response(
            200,
            content=(
                "<result><status>013</status>"
                f"<message>no data {api_key} {request.url}</message></result>"
            ).encode(),
            headers={"Content-Type": "application/xml"},
            request=request,
        )

    landing_url = (
        f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DisclosureContentError, match=r"status 013") as exc_info:
            await DisclosureTextFetcher(client).fetch(landing_url)

    error = str(exc_info.value)
    assert api_key not in error
    assert receipt_no not in error
    assert "crtfc_key" not in error


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_content", "response_headers", "error_match"),
    [
        (
            b"PK\x03\x04not-a-valid-zip",
            {},
            "ZIP is invalid",
        ),
        (
            b"small body",
            {"Content-Length": str(DART_DOCUMENT_MAX_RESPONSE_BYTES + 1)},
            "response exceeds the size limit",
        ),
    ],
)
async def test_dart_document_fallback_rejects_invalid_or_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
    response_content: bytes,
    response_headers: dict[str, str],
    error_match: str,
) -> None:
    monkeypatch.setattr(settings, "opendart_api_key", "test-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "dart.fss.or.kr":
            return httpx.Response(200, text="<html>no viewer</html>", request=request)
        return httpx.Response(
            200,
            content=response_content,
            headers=response_headers,
            request=request,
        )

    landing_url = (
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260828001916"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DisclosureContentError, match=error_match):
            await DisclosureTextFetcher(client).fetch(landing_url)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dart_document_fallback_rejects_unsafe_zip_member_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "opendart_api_key", "test-key")
    unsafe_zip = _document_zip(
        b"<html><body>unsafe archive member must not be read</body></html>",
        name="../escape.xml",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "dart.fss.or.kr":
            return httpx.Response(200, text="<html>no viewer</html>", request=request)
        return httpx.Response(200, content=unsafe_zip, request=request)

    landing_url = (
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260828001916"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DisclosureContentError, match="unsafe member path"):
            await DisclosureTextFetcher(client).fetch(landing_url)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt_query", "api_key", "error_match"),
    [
        ("rcpNo=20260828001916", "", "OPENDART_API_KEY is required"),
        ("rcpNo=not-numeric", "test-key", "one numeric rcpNo"),
        ("other=value", "test-key", "one numeric rcpNo"),
    ],
)
async def test_dart_document_fallback_requires_key_and_numeric_receipt_number(
    monkeypatch: pytest.MonkeyPatch,
    receipt_query: str,
    api_key: str,
    error_match: str,
) -> None:
    monkeypatch.setattr(settings, "opendart_api_key", api_key)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="<html>no viewer</html>", request=request)

    landing_url = f"https://dart.fss.or.kr/dsaf001/main.do?{receipt_query}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DisclosureContentError, match=error_match):
            await DisclosureTextFetcher(client).fetch(landing_url)

    assert calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dart_document_fallback_does_not_follow_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "opendart_api_key", "test-key")
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "dart.fss.or.kr":
            return httpx.Response(200, text="<html>no viewer</html>", request=request)
        return httpx.Response(
            302,
            headers={"Location": "https://evil.test/stolen"},
            request=request,
        )

    landing_url = (
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260828001916"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DisclosureContentError, match="redirects are not allowed"):
            await DisclosureTextFetcher(client).fetch(landing_url)

    assert calls == ["dart.fss.or.kr", "opendart.fss.or.kr"]
