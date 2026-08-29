"""SEC/DART 공시 원문 fetch와 본문 정리 경계 테스트."""

from __future__ import annotations

import httpx
import pytest

from app.services.disclosures.content_fetcher import (
    DisclosureContentError,
    DisclosureTextFetcher,
    extract_disclosure_text,
)


class FakeRateLimiter:
    def __init__(self) -> None:
        self.acquire_count = 0

    async def acquire(self) -> None:
        self.acquire_count += 1


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
async def test_dart_landing_resolves_viewer_and_extracts_actual_body() -> None:
    calls: list[tuple[str, str | None]] = []

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
