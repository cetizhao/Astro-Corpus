from __future__ import annotations

import gzip
import re

import httpx
import pytest

from astro_pipeline.sources.paper_fulltext import (
    ArxivClient,
    DownloadedArtifact,
    PaperDownloadError,
    PaperUnavailable,
    normalize_arxiv_id,
    split_arxiv_id,
)


def test_slow_continuous_stream_has_a_total_request_budget():
    timer = FakeTime()
    calls = []

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b"%PDF-1.7\\n", b"still streaming", b"more"):
                timer.value += 65
                yield chunk

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "application/pdf"}, stream=SlowStream())

    with client_for(handler, fake_time=timer, total_timeout=120) as client:
        with pytest.raises(PaperDownloadError, match="total time budget"):
            client.fetch("2305.03557v1", "pdf")
    assert len(calls) == 1
    assert timer.value == 130


def test_retry_after_cannot_exceed_remaining_total_budget():
    timer = FakeTime()
    calls = []

    def handler(request):
        calls.append(str(request.url))
        timer.value += 20
        return httpx.Response(429, headers={"retry-after": "20"})

    with client_for(handler, fake_time=timer, total_timeout=30) as client:
        with pytest.raises(PaperDownloadError, match="total time budget"):
            client.fetch("2305.03557v1", "pdf")
    assert len(calls) == 1
    assert timer.sleeps == []


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def client_for(handler, *, fake_time: FakeTime | None = None, **kwargs) -> ArxivClient:
    time_source = fake_time or FakeTime()
    transport = httpx.MockTransport(handler)
    return ArxivClient(
        transport=transport,
        clock=time_source.clock,
        sleep=time_source.sleep,
        **kwargs,
    )


def html_response(request: httpx.Request, *, identifier: str = "2401.01234v2") -> httpx.Response:
    body = f"""<!doctype html><html><head>
      <meta name="citation_arxiv_id" content="{identifier}">
      <meta name="citation_title" content="A Test Paper">
      <meta name="citation_author" content="Ada Lovelace">
      <meta name="citation_author" content="Grace Hopper">
      <meta name="citation_doi" content="10.1234/example">
      <meta name="citation_license" content="http://creativecommons.org/licenses/by/4.0/">
      <link rel="canonical" href="https://arxiv.org/abs/{identifier}">
      </head><body><article><h1>A Test Paper</h1><p>{'A meaningful article paragraph. ' * 8}</p></article></body></html>""".encode()
    return httpx.Response(200, request=request, content=body, headers={"content-type": "text/html; charset=utf-8"})


def test_normalize_modern_legacy_prefix_and_official_url() -> None:
    assert normalize_arxiv_id("  arXiv:2401.01234v2 ") == "2401.01234v2"
    assert normalize_arxiv_id("astro-ph/0704123v1") == "astro-ph/0704123v1"
    assert normalize_arxiv_id("https://arxiv.org/abs/2401.01234v2") == "2401.01234v2"
    assert normalize_arxiv_id("https://arxiv.org/abs/astro-ph/0704123v1") == "astro-ph/0704123v1"
    assert split_arxiv_id("arXiv:2401.01234v2") == ("2401.01234", 2)
    assert split_arxiv_id("astro-ph/0704123") == ("astro-ph/0704123", None)


@pytest.mark.parametrize(
    "value",
    [
        "2401.012",  # modern identifiers require four or five sequence digits
        "2401.012345",
        "astro-ph/070412",
        "astro-ph/07041234",
        "2401.01234v0",
        "2401.01234v01",
        "https://evil.example/abs/2401.01234",
        "https://arxiv.org/pdf/2401.01234",
        "https://arxiv.org/abs/2401.01234?download=1",
    ],
)
def test_rejects_malformed_or_unsafe_identifiers(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_arxiv_id(value)


def test_resolve_pins_metadata_version_and_preserves_license_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "arxiv.org"
        assert request.url.path == "/abs/2401.01234"
        return html_response(request)

    with client_for(handler, request_delay=0.1) as client:
        result = client.resolve("arXiv:2401.01234")

    assert result["arxiv_id"] == "2401.01234"
    assert result["version"] == 2
    assert result["versioned_id"] == "2401.01234v2"
    assert result["title"] == "A Test Paper"
    assert result["authors"] == ["Ada Lovelace", "Grace Hopper"]
    assert result["doi"] == "10.1234/example"
    assert result["license"] == "http://creativecommons.org/licenses/by/4.0/"
    assert result["license_evidence"]
    assert isinstance(result["_metadata_bytes"], bytes)
    assert result["_metadata_content_type"].startswith("text/html")
    assert result["metadata_url"] == "https://arxiv.org/abs/2401.01234"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT.*Z", result["resolved_at"])


def test_resolve_rejects_mismatched_abstract_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return html_response(request, identifier="2401.99999v2")

    with client_for(handler) as client, pytest.raises(PaperDownloadError, match="identity mismatch"):
        client.resolve("2401.01234v2")


def test_resolve_uses_official_api_when_abs_page_does_not_expose_exact_version() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/abs/2401.01234":
            return httpx.Response(
                200,
                request=request,
                content=b'<html><head><meta name="citation_title" content="API title"><meta name="citation_author" content="Author One"></head></html>',
                headers={"content-type": "text/html"},
            )
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(
            200,
            request=request,
            content=b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry><id>https://arxiv.org/abs/2401.01234v3</id><title>API title</title><author><name>Author One</name></author><author><name>Author Two</name></author><arxiv:doi>10.1234/api</arxiv:doi></entry></feed>''',
            headers={"content-type": "application/atom+xml"},
        )

    with client_for(handler) as client:
        result = client.resolve("2401.01234")
    assert result["versioned_id"] == "2401.01234v3"
    assert result["doi"] == "10.1234/api"
    assert result["_metadata_content_type"] == "application/atom+xml"
    assert result["metadata_url"] == "https://export.arxiv.org/api/query?id_list=2401.01234&max_results=1"
    assert result["metadata_url"] == result["_metadata_responses"][1]["url"]
    assert result["_metadata_bytes"] == result["_metadata_responses"][1]["data"]
    assert len(result["_metadata_responses"]) == 2
    assert len(calls) == 2


def test_explicit_old_version_is_used_for_api_fallback() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/abs/2401.01234v2":
            return httpx.Response(
                200,
                request=request,
                content=b'<html><head><meta name="citation_title" content="Old version"></head></html>',
                headers={"content-type": "text/html"},
            )
        assert request.url.path == "/api/query"
        assert request.url.params["id_list"] == "2401.01234v2"
        return httpx.Response(
            200,
            request=request,
            content=b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/2401.01234v2</id><title>Old version</title><author><name>Author v2</name></author></entry></feed>''',
            headers={"content-type": "application/atom+xml"},
        )

    with client_for(handler) as client:
        result = client.resolve("2401.01234v2")
    assert result["versioned_id"] == "2401.01234v2"
    assert result["authors"] == ["Author v2"]
    assert "id_list=2401.01234v2" in requests[1]
    assert result["metadata_url"] == result["_metadata_responses"][1]["url"]


def test_bare_metadata_redirect_can_resolve_exact_version() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/abs/2401.01234":
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://arxiv.org/abs/2401.01234v4"},
            )
        return html_response(request, identifier="2401.01234v4")

    with client_for(handler) as client:
        result = client.resolve("2401.01234")
    assert result["versioned_id"] == "2401.01234v4"
    assert result["metadata_url"] == "https://arxiv.org/abs/2401.01234v4"
    assert len(calls) == 2


def test_license_evidence_captures_concrete_licenses_not_help_links() -> None:
    body = b'''<!doctype html><html><head>
      <meta name="citation_arxiv_id" content="2401.01234v2">
      <meta name="citation_title" content="Licensing Test">
      <meta name="citation_author" content="Ada Author">
      <meta name="citation_pdf_url" content="https://arxiv.org/pdf/2401.01234v2">
      </head><body>
      <a href="https://arxiv.org/help/license">License help</a>
      <a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0 1.0 Universal</a>
      <a href="https://arxiv.org/licenses/nonexclusive-distrib/1.0/">arXiv.org perpetual, non-exclusive license</a>
      </body></html>'''

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=body, headers={"content-type": "text/html"})

    with client_for(handler) as client:
        result = client.resolve("2401.01234v2")
    evidence = result["license_evidence"]
    assert len(evidence) == 2
    assert len(result["_metadata_responses"]) == 1
    assert result["metadata_url"] == result["_metadata_responses"][0]["url"]
    assert result["_metadata_bytes"] == result["_metadata_responses"][0]["data"]
    assert any("publicdomain/zero/1.0" in item["href"] for item in evidence)
    assert any("nonexclusive-distrib/1.0" in item["href"] for item in evidence)
    assert any("perpetual, non-exclusive" in item["text"] for item in evidence)
    assert all("help/license" not in item["href"] for item in evidence)


def test_request_rate_limit_is_floored_and_serialized_for_resolve_calls() -> None:
    fake_time = FakeTime()
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "astro-pipeline/0.5"
        starts.append(fake_time.clock())
        return html_response(request)

    with client_for(handler, fake_time=fake_time, request_delay=0.01) as client:
        client.resolve("2401.01234v2")
        client.resolve("2401.01234v2")
    assert starts == [0.0, 3.0]
    assert client.request_delay == 3.0
    assert fake_time.sleeps == [3.0]


def test_retry_after_beyond_budget_fails_without_early_retry() -> None:
    fake_time = FakeTime()
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(fake_time.clock())
        return httpx.Response(429, request=request, headers={"Retry-After": "999"})

    with client_for(handler, fake_time=fake_time, max_retries=2) as client, pytest.raises(
        PaperDownloadError, match="exceeds.*wait budget.*retry later"
    ):
        client.resolve("2401.01234v2")
    assert starts == [0.0]
    assert fake_time.sleeps == []


def test_retry_after_within_budget_is_respected() -> None:
    fake_time = FakeTime()
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(fake_time.clock())
        if len(starts) == 1:
            return httpx.Response(429, request=request, headers={"Retry-After": "30"})
        return html_response(request)

    with client_for(handler, fake_time=fake_time, max_retries=2) as client:
        result = client.resolve("2401.01234v2")
    assert result["versioned_id"] == "2401.01234v2"
    assert starts == [0.0, 30.0]
    assert fake_time.sleeps == [30.0]


def test_retryable_status_retries_no_more_than_configured() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    with client_for(handler, max_retries=2) as client, pytest.raises(PaperDownloadError, match="after retries"):
        client.resolve("2401.01234v2")
    assert calls == 3


def test_redirect_allowlist_and_version_pin() -> None:
    def external(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, request=request, headers={"location": "https://evil.example/pdf/2401.01234v2"})

    with client_for(external) as client, pytest.raises(PaperDownloadError, match="official HTTPS"):
        client.fetch("2401.01234v2", "pdf")

    def unversioned(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, request=request, headers={"location": "https://arxiv.org/pdf/2401.01234"})

    with client_for(unversioned) as client, pytest.raises(PaperDownloadError, match="unversioned or mismatched"):
        client.fetch("2401.01234v2", "pdf")

    def mismatched(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, request=request, headers={"location": "https://export.arxiv.org/pdf/2401.99999v2"})

    with client_for(mismatched) as client, pytest.raises(PaperDownloadError, match="unversioned or mismatched"):
        client.fetch("2401.01234v2", "pdf")


def test_unversioned_fetch_and_missing_version_fail_before_network() -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("fetch without a version must not make a request")

    with client_for(unexpected) as client, pytest.raises(PaperDownloadError, match="explicit"):
        client.fetch("2401.01234", "pdf")


def test_only_404_and_410_are_paper_unavailable() -> None:
    for status in (404, 410):
        def handler(request: httpx.Request, status=status) -> httpx.Response:
            return httpx.Response(status, request=request)

        with client_for(handler, max_retries=0) as client, pytest.raises(PaperUnavailable):
            client.fetch("2401.01234v2", "pdf")

    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    with client_for(server_error, max_retries=0) as client, pytest.raises(PaperDownloadError) as exc:
        client.fetch("2401.01234v2", "pdf")
    assert not isinstance(exc.value, PaperUnavailable)


def test_pdf_magic_and_actual_streamed_size_are_validated() -> None:
    def not_pdf(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"<html>not pdf</html>")

    with client_for(not_pdf) as client, pytest.raises(PaperDownloadError, match="magic"):
        client.fetch("2401.01234v2", "pdf")

    def too_large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"%PDF-" + b"x" * 32)

    with client_for(too_large, max_bytes=20) as client, pytest.raises(PaperDownloadError, match="byte limit"):
        client.fetch("2401.01234v2", "pdf")


def test_decompressed_transfer_bytes_are_also_bounded() -> None:
    compressed = gzip.compress(b"%PDF-" + b"x" * 500)

    def compressed_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            content=compressed,
            headers={
                "content-type": "application/pdf",
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
            },
        )

    with client_for(compressed_response, max_bytes=100) as client, pytest.raises(
        PaperDownloadError, match="decompressed.*byte limit"
    ):
        client.fetch("2401.01234v2", "pdf")


def test_fulltext_html_requires_article_and_checks_identity() -> None:
    abstract_only = b"<html><head><meta name='citation_arxiv_id' content='2401.01234v2'></head><body><div class='abstract'>Only abstract</div></body></html>"

    def abstract_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=abstract_only, headers={"content-type": "text/html"})

    with client_for(abstract_handler) as client, pytest.raises(PaperDownloadError, match="article structure"):
        client.fetch("2401.01234v2", "html")

    def mismatch_handler(request: httpx.Request) -> httpx.Response:
        body = html_response(request, identifier="2401.99999v2").content
        return httpx.Response(200, request=request, content=body, headers={"content-type": "text/html"})

    with client_for(mismatch_handler) as client, pytest.raises(PaperDownloadError, match="identity mismatch"):
        client.fetch("2401.01234v2", "html")

    def valid_handler(request: httpx.Request) -> httpx.Response:
        return html_response(request)

    with client_for(valid_handler) as client:
        artifact = client.fetch("2401.01234v2", "html")
    assert isinstance(artifact, DownloadedArtifact)
    assert artifact.kind == "html"
    assert artifact.versioned_id == "2401.01234v2"
    assert artifact.url == "https://arxiv.org/html/2401.01234v2"
    assert artifact.final_url == artifact.url
    assert artifact.data


def test_source_rejects_html_error_but_accepts_tex_and_archive_shapes() -> None:
    def html_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"<html><body>Please log in</body></html>", headers={"content-type": "text/html"})

    with client_for(html_handler) as client, pytest.raises(PaperDownloadError, match="HTML/XML"):
        client.fetch("2401.01234v2", "source")

    def tex_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"\\documentclass{article}\n\\begin{document}Hello\\end{document}", headers={"content-type": "application/x-tex"})

    with client_for(tex_handler) as client:
        artifact = client.fetch("2401.01234v2", "source")
    assert artifact.data.startswith(b"\\documentclass")


def test_nonofficial_and_mismatched_metadata_api_redirects_are_rejected() -> None:
    def api_external(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/abs/2401.01234":
            return httpx.Response(200, request=request, content=b'<html><head><meta name="citation_title" content="Title"><meta name="citation_author" content="Author"></head></html>', headers={"content-type": "text/html"})
        return httpx.Response(302, request=request, headers={"location": "https://evil.example/api/query?id_list=2401.01234"})

    with client_for(api_external) as client, pytest.raises(PaperDownloadError, match="official HTTPS"):
        client.resolve("2401.01234")
