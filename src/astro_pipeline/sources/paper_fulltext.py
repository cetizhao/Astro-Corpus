"""Version-pinned arXiv metadata and full-text downloads.

This client intentionally talks only to arxiv.org and export.arxiv.org. Its
rate limiter is serialized within one client process; callers sharing an
arXiv workload across processes must also apply an application-level limiter.
"""
from __future__ import annotations

import email.utils
import io
import math
import re
import tarfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable, Literal

import httpx


ARXIV_HOSTS = frozenset({"arxiv.org", "export.arxiv.org"})
USER_AGENT = "astro-pipeline/0.5"
DEFAULT_TIMEOUT = 45.0
DEFAULT_MAX_BYTES = 50 * 1024**2
MIN_REQUEST_DELAY = 3.0
MAX_RETRY_AFTER = 60.0
MAX_REDIRECTS = 5

_MODERN_ID = re.compile(r"^(\d{4}\.\d{4,5})(?:v([1-9]\d*))?$", re.IGNORECASE)
_LEGACY_ID = re.compile(r"^(astro-ph/\d{7})(?:v([1-9]\d*))?$", re.IGNORECASE)
_ANY_ID = re.compile(r"(?:\d{4}\.\d{4,5}|astro-ph/\d{7})(?:v\d+)?", re.IGNORECASE)


class PaperDownloadError(RuntimeError):
    """A retryable, protocol, or validation error while accessing arXiv."""


class PaperUnavailable(PaperDownloadError):
    """The requested paper/version was explicitly absent (HTTP 404 or 410)."""


@dataclass(frozen=True)
class DownloadedArtifact:
    data: bytes
    url: str
    final_url: str
    content_type: str
    retrieved_at: str
    kind: str
    versioned_id: str


def normalize_arxiv_id(value: str) -> str:
    """Return a canonical arXiv identifier, preserving an optional version.

    Accepted forms are modern ``YYMM.NNNN[N]``, legacy ``astro-ph/YYMMNNN``,
    an optional ``arXiv:`` prefix, and an HTTPS arxiv.org ``/abs/`` URL.
    Other URLs, URL decorations, and unrecognized identifiers are rejected.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("arXiv identifier must be a non-empty string")
    candidate = value.strip()
    if candidate.lower().startswith("arxiv:"):
        candidate = candidate[6:].strip()
    if "://" in candidate:
        parsed = urllib.parse.urlsplit(candidate)
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() not in ARXIV_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("only an official HTTPS arXiv /abs/ URL is accepted")
        match = re.fullmatch(r"/abs/(astro-ph/\d{7}(?:v\d+)?|[^/]+)/?", parsed.path, flags=re.IGNORECASE)
        if not match:
            raise ValueError("official arXiv URL must use the /abs/{identifier} path")
        candidate = urllib.parse.unquote(match.group(1))

    match = _MODERN_ID.fullmatch(candidate)
    if match:
        return f"{match.group(1)}" + (f"v{match.group(2)}" if match.group(2) else "")
    match = _LEGACY_ID.fullmatch(candidate)
    if match:
        return f"astro-ph/{match.group(1).split('/')[-1]}" + (
            f"v{match.group(2)}" if match.group(2) else ""
        )
    raise ValueError(f"unsupported or malformed arXiv identifier: {value!r}")


def split_arxiv_id(value: str) -> tuple[str, int | None]:
    """Return ``(unversioned_work_id, version_number_or_none)``."""
    normalized = normalize_arxiv_id(value)
    match = _MODERN_ID.fullmatch(normalized) or _LEGACY_ID.fullmatch(normalized)
    assert match is not None  # normalize_arxiv_id has validated this value.
    return match.group(1), int(match.group(2)) if match.group(2) else None


def _recognized_license(href: str, text: str) -> bool:
    """Accept concrete article-license evidence, not generic license help links."""
    href_lower = href.strip().lower()
    text_lower = " ".join(text.lower().split())
    combined = f"{href_lower} {text_lower}"
    if "creativecommons.org/publicdomain/zero/1.0" in combined:
        return True
    if re.search(
        r"creativecommons\.org/licenses/(?:by|by-sa|by-nc|by-nd|by-nc-sa|by-nc-nd)/[0-9]+(?:\.[0-9]+)?/?",
        href_lower,
    ):
        return True
    if re.search(r"arxiv\.org/licenses?/nonexclusive-distrib/1\.0/?", href_lower):
        return True
    if "arxiv.org perpetual, non-exclusive license" in text_lower:
        return "arxiv.org" in href_lower or not href_lower
    if re.search(r"\bcc0\b|creative commons zero", text_lower):
        return True
    if re.search(r"\bcc by(?:-sa|-nc|-nd)?\s+[0-9]+(?:\.[0-9]+)?\b", text_lower):
        return True
    return False


def _license_evidence(source: str, href: str, text: str, *, name: str = "") -> dict[str, str] | None:
    value = href or text.strip()
    if not _recognized_license(href or value, text or value):
        return None
    result = {"source": source, "href": href, "text": text.strip(), "value": value}
    if name:
        result["name"] = name
    return result


class _PageParser(HTMLParser):
    """Collect citation metadata, license evidence, and article structure."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.canonical_urls: list[str] = []
        self.license_links: list[dict[str, str]] = []
        self._license_anchor_index: int | None = None
        self._in_article = False
        self.article_text: list[str] = []
        self.article_elements = 0
        self._in_title = False
        self.title_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "meta":
            name = (attrs_map.get("name") or attrs_map.get("property") or "").lower()
            content = attrs_map.get("content", "").strip()
            if name and content:
                self.meta.setdefault(name, []).append(content)
        elif tag == "link" and "canonical" in attrs_map.get("rel", "").lower().split():
            if attrs_map.get("href"):
                self.canonical_urls.append(attrs_map["href"])
        elif tag == "a":
            self.license_links.append({"href": attrs_map.get("href", ""), "text": ""})
            self._license_anchor_index = len(self.license_links) - 1
        if tag == "article":
            self._in_article = True
            self.article_elements += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "article":
            self._in_article = False
        elif tag == "a":
            self._license_anchor_index = None

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_text.append(text)
        if self._in_article:
            self.article_text.append(text)
        if self._license_anchor_index is not None:
            link = self.license_links[self._license_anchor_index]
            link["text"] = (link["text"] + " " + text).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _arxiv_id_from_url(url: str, *, abs_only: bool = False) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.hostname or "").lower() not in ARXIV_HOSTS or parsed.scheme.lower() != "https":
        return None
    path = urllib.parse.unquote(parsed.path)
    if abs_only:
        match = re.fullmatch(r"/abs/(astro-ph/\d{7}(?:v\d+)?|[^/]+)/?", path, re.IGNORECASE)
        if not match:
            return None
        candidate = match.group(1)
    else:
        match = re.search(
            r"/(?:abs|html|src|pdf|e-print|format)/(astro-ph/\d{7}(?:v\d+)?|[^/]+?)(?:\.pdf)?/?$",
            path,
            re.IGNORECASE,
        )
        if not match:
            return None
        candidate = match.group(1)
    try:
        return normalize_arxiv_id(candidate)
    except ValueError:
        return None


def _identity_matches(actual: str, work_id: str, version: int | None = None) -> bool:
    try:
        actual_work, actual_version = split_arxiv_id(actual)
    except ValueError:
        return False
    return actual_work == work_id and (version is None or actual_version == version)


def _identity_compatible(actual: str, work_id: str, version: int | None) -> bool:
    """A bare metadata ID may corroborate, but cannot override, an exact URL."""
    try:
        actual_work, actual_version = split_arxiv_id(actual)
    except ValueError:
        return False
    return actual_work == work_id and (
        actual_version is None or version is None or actual_version == version
    )


def _parse_api_entry(payload: bytes) -> dict[str, object] | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise PaperDownloadError("arXiv API returned malformed XML") from exc
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    entry = root.find("atom:entry", ns)
    if entry is None:
        return None

    def text(path: str) -> str:
        node = entry.find(path, ns)
        return (node.text or "").strip() if node is not None else ""

    identifier_url = text("atom:id")
    identifier = _arxiv_id_from_url(identifier_url, abs_only=True) if identifier_url else None
    if not identifier and identifier_url:
        # Some official Atom feeds still serialize the absolute link as HTTP;
        # parse it as data only (all network requests remain HTTPS-only).
        parsed_id = urllib.parse.urlsplit(identifier_url)
        if (
            parsed_id.scheme.lower() in {"http", "https"}
            and (parsed_id.hostname or "").lower() == "arxiv.org"
            and not parsed_id.username
            and not parsed_id.password
        ):
            match = re.fullmatch(
                r"/abs/(astro-ph/\d{7}(?:v\d+)?|[^/]+)/?",
                urllib.parse.unquote(parsed_id.path),
                flags=re.IGNORECASE,
            )
            if match:
                try:
                    identifier = normalize_arxiv_id(match.group(1))
                except ValueError:
                    identifier = None
    if not identifier:
        raise PaperDownloadError("arXiv API entry has no official abstract identifier")
    authors = [
        (node.text or "").strip()
        for node in entry.findall("atom:author/atom:name", ns)
        if (node.text or "").strip()
    ]
    doi = text("arxiv:doi") or None
    license_el = entry.find("arxiv:license", ns)
    license_value = ""
    license_href = ""
    if license_el is not None:
        license_value = (license_el.text or "").strip()
        license_href = license_el.attrib.get("{http://www.w3.org/1999/xlink}href", "")
    license_evidence = []
    if license_value or license_href:
        evidence = _license_evidence("arxiv_api", license_href, license_value)
        if evidence:
            license_evidence.append(evidence)
    return {
        "arxiv_id": identifier,
        "title": " ".join(text("atom:title").split()),
        "authors": authors,
        "doi": doi,
        "license": license_evidence[0]["value"] if license_evidence else None,
        "license_evidence": license_evidence,
    }


class ArxivClient:
    """Throttled, version-aware client for official arXiv endpoints only.

    ``request_delay`` is floored at three seconds. The limiter serializes
    request-start scheduling inside this instance, not across processes.
    ``max_retries`` counts retries after the initial attempt.
    """

    def __init__(
        self,
        request_delay: float = 3.0,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_retries: int = 2,
        *,
        total_timeout: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if not math.isfinite(total_timeout) or total_timeout <= 0:
            raise ValueError("total_timeout must be positive and finite")
        if not math.isfinite(request_delay) or request_delay < 0:
            raise ValueError("request_delay must be non-negative and finite")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.request_delay = max(MIN_REQUEST_DELAY, float(request_delay))
        self.timeout = float(timeout)
        self.total_timeout = float(total_timeout)
        self.max_bytes = int(max_bytes)
        self.max_retries = int(max_retries)
        self._clock = clock
        self._sleep = sleep
        self._transport = transport
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._next_request_at = 0.0

    def __enter__(self) -> "ArxivClient":
        self._get_client()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _get_client(self) -> httpx.Client:
        with self._client_lock:
            if self._client is None:
                self._client = httpx.Client(
                    timeout=self.timeout,
                    follow_redirects=False,
                    headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
                    transport=self._transport,
                )
            return self._client

    def _pace(self) -> None:
        with self._rate_lock:
            now = self._clock()
            wait = max(0.0, self._next_request_at - now)
            if wait:
                self._sleep(wait)
                now = self._clock()
            self._next_request_at = max(now, self._next_request_at) + self.request_delay

    def _retry_after(self, raw: str | None) -> float | None:
        if not raw:
            return None
        try:
            seconds = float(raw.strip())
        except (TypeError, ValueError):
            try:
                date = email.utils.parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = date.timestamp() - datetime.now(timezone.utc).timestamp()
            except (TypeError, ValueError, OverflowError):
                return None
        if seconds > MAX_RETRY_AFTER:
            raise PaperDownloadError(
                f"arXiv Retry-After exceeds the {MAX_RETRY_AFTER:g}s wait budget; retry later"
            )
        return max(MIN_REQUEST_DELAY, seconds)

    @staticmethod
    def _validate_official_url(url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() not in ARXIV_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise PaperDownloadError("arXiv request/redirect left the official HTTPS host allowlist")

    def _validate_redirect(
        self,
        location: str,
        current_url: str,
        work_id: str,
        version: int | None,
        *,
        api: bool,
        api_identifier: str | None = None,
    ) -> str:
        target = urllib.parse.urljoin(current_url, location)
        self._validate_official_url(target)
        parsed = urllib.parse.urlsplit(target)
        if api:
            if parsed.path != "/api/query":
                raise PaperDownloadError("arXiv API redirected to an unexpected endpoint")
            requested = urllib.parse.parse_qs(parsed.query).get("id_list", [""])[0]
            if requested != (api_identifier or work_id):
                raise PaperDownloadError("arXiv API redirect changed the requested identifier")
        else:
            actual = _arxiv_id_from_url(target)
            matches = (
                _identity_compatible(actual, work_id, None)
                if version is None
                else _identity_matches(actual or "", work_id, version)
            )
            if not actual or not matches:
                raise PaperDownloadError("arXiv redirected to an unversioned or mismatched paper")
        return target

    def _one_request(
        self,
        url: str,
        work_id: str,
        version: int | None,
        *,
        api: bool = False,
        api_identifier: str | None = None,
        deadline: float | None = None,
    ) -> tuple[bytes, str, str, str]:
        deadline = deadline if deadline is not None else self._clock() + self.total_timeout
        current_url = url
        redirects = 0
        while True:
            self._validate_official_url(current_url)
            self._pace()
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise PaperDownloadError("arXiv request exceeded its total time budget")
            try:
                with self._get_client().stream("GET", current_url, timeout=min(self.timeout, remaining)) as response:
                    status = response.status_code
                    headers = response.headers
                    if status in (301, 302, 303, 307, 308):
                        location = headers.get("location")
                        if not location:
                            raise PaperDownloadError("arXiv returned a redirect without Location")
                        if redirects >= MAX_REDIRECTS:
                            raise PaperDownloadError("too many arXiv redirects")
                        current_url = self._validate_redirect(
                            location,
                            current_url,
                            work_id,
                            version,
                            api=api,
                            api_identifier=api_identifier,
                        )
                        redirects += 1
                        continue
                    if status in (404, 410):
                        raise PaperUnavailable(f"arXiv returned HTTP {status} for {current_url}")
                    if status == 429 or status in (408, 425) or 500 <= status <= 599:
                        raise _RetryableStatus(status, headers.get("retry-after"))
                    if not 200 <= status < 300:
                        raise PaperDownloadError(f"arXiv returned HTTP {status} for {current_url}")
                    declared = headers.get("content-length")
                    if declared:
                        try:
                            if int(declared) > self.max_bytes:
                                raise PaperDownloadError("arXiv response exceeds configured byte limit")
                        except ValueError:
                            pass
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        if self._clock() >= deadline:
                            raise PaperDownloadError("arXiv request exceeded its total time budget")
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise PaperDownloadError("decompressed arXiv response exceeds configured byte limit")
                        chunks.append(chunk)
                    content_type = headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
                    return b"".join(chunks), current_url, content_type, headers.get("content-type", "application/octet-stream")
            except _RetryableStatus as exc:
                raise exc

    def _request(
        self,
        url: str,
        work_id: str,
        version: int | None,
        *,
        api: bool = False,
        api_identifier: str | None = None,
    ) -> tuple[bytes, str, str, str]:
        last_error: Exception | None = None
        deadline = self._clock() + self.total_timeout
        for attempt in range(self.max_retries + 1):
            try:
                return self._one_request(
                    url,
                    work_id,
                    version,
                    api=api,
                    api_identifier=api_identifier,
                    deadline=deadline,
                )
            except PaperUnavailable:
                raise
            except _RetryableStatus as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                retry_after = self._retry_after(exc.retry_after)
                wait = max(self.request_delay, retry_after or 0.0)
                if self._clock() + wait >= deadline:
                    raise PaperDownloadError("arXiv retry exceeds the total time budget; retry later") from exc
                self._sleep(wait)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                wait = self.request_delay * (attempt + 1)
                if self._clock() + wait >= deadline:
                    raise PaperDownloadError("arXiv request exceeded its total time budget") from exc
                self._sleep(wait)
            except httpx.HTTPError as exc:
                raise PaperDownloadError(f"arXiv HTTP protocol error: {exc}") from exc
        raise PaperDownloadError(f"arXiv request failed after retries: {last_error}") from last_error

    def resolve(self, identifier: str) -> dict[str, object]:
        """Resolve metadata and pin the requested/latest exact arXiv version."""
        normalized = normalize_arxiv_id(identifier)
        work_id, requested_version = split_arxiv_id(normalized)
        requested_token = normalized
        url = f"https://arxiv.org/abs/{urllib.parse.quote(requested_token, safe='/')}"
        payload, final_url, content_type, raw_content_type = self._request(
            url, work_id, requested_version
        )
        if "html" not in content_type and not payload.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise PaperDownloadError("arXiv abstract endpoint did not return HTML")
        parser = _PageParser()
        try:
            parser.feed(payload.decode("utf-8", errors="replace"))
        except Exception as exc:
            raise PaperDownloadError("could not parse arXiv abstract metadata") from exc

        page_ids: list[str] = []
        for value in parser.meta.get("citation_arxiv_id", []):
            match = _ANY_ID.search(value)
            if match:
                page_ids.append(normalize_arxiv_id(match.group(0)))
        for value in parser.meta.get("citation_pdf_url", []):
            page_id = _arxiv_id_from_url(value)
            if page_id:
                page_ids.append(page_id)
        for value in parser.canonical_urls:
            page_id = _arxiv_id_from_url(urllib.parse.urljoin(final_url, value), abs_only=True)
            if page_id:
                page_ids.append(page_id)
        final_id = _arxiv_id_from_url(final_url, abs_only=True)
        if final_id:
            page_ids.append(final_id)
        for page_id in page_ids:
            if not _identity_compatible(page_id, work_id, requested_version):
                raise PaperDownloadError(
                    f"arXiv abstract identity mismatch: requested {normalized}, page reports {page_id}"
                )

        exact_id = next((item for item in page_ids if split_arxiv_id(item)[1] is not None), None)
        title = next(iter(parser.meta.get("citation_title", [])), "").strip()
        authors = [value.strip() for value in parser.meta.get("citation_author", []) if value.strip()]
        doi = next(iter(parser.meta.get("citation_doi", [])), "").strip() or None
        license_evidence: list[dict[str, str]] = []
        for key in ("citation_license", "license", "dc.rights", "dcterms.rights"):
            for value in parser.meta.get(key, []):
                evidence = _license_evidence("abs_html_meta", value, value, name=key)
                if evidence:
                    license_evidence.append(evidence)
        for link in parser.license_links:
            evidence = _license_evidence("abs_html_link", link["href"], link["text"])
            if evidence:
                license_evidence.append(evidence)
        license_value = license_evidence[0].get("value") if license_evidence else None

        api_metadata: dict[str, object] | None = None
        api_payload = b""
        api_type = ""
        api_url = ""
        metadata_url = final_url
        metadata_responses: list[dict[str, object]] = [
            {"data": payload, "url": final_url, "content_type": raw_content_type}
        ]
        if not exact_id or not title or not authors:
            api_identifier = normalized if requested_version is not None else work_id
            api_metadata, api_payload, api_type, api_url = self._resolve_api(api_identifier)
            metadata_responses.append(
                {"data": api_payload, "url": api_url, "content_type": api_type}
            )
            metadata_url = api_url
            api_id = str(api_metadata["arxiv_id"])
            if not _identity_matches(api_id, work_id, requested_version):
                raise PaperDownloadError(
                    f"arXiv API identity mismatch: requested {normalized}, API reports {api_id}"
                )
            api_version = split_arxiv_id(api_id)[1]
            if requested_version is not None and api_version != requested_version:
                raise PaperDownloadError("arXiv API did not return the requested exact version")
            exact_id = exact_id or api_id
            title = title or str(api_metadata.get("title") or "")
            authors = authors or list(api_metadata.get("authors") or [])
            doi = doi or api_metadata.get("doi")  # type: ignore[assignment]
            if not license_value and api_metadata.get("license"):
                license_value = str(api_metadata["license"])
                license_evidence.extend(api_metadata.get("license_evidence", []))
        else:
            api_payload = b""
            api_type = ""

        if requested_version is not None:
            version = requested_version
            versioned_id = f"{work_id}v{version}"
        else:
            if exact_id is None:
                raise PaperDownloadError("arXiv metadata did not identify an exact version")
            parsed_version = split_arxiv_id(exact_id)[1]
            if parsed_version is None:
                raise PaperDownloadError("arXiv metadata did not identify an exact version")
            version = parsed_version
            versioned_id = f"{work_id}v{version}"

        if not title:
            title = " ".join(parser.title_text).strip()
        if not title or not authors:
            raise PaperDownloadError("arXiv metadata is missing a title or authors")
        if not license_value and license_evidence:
            license_value = license_evidence[0].get("value") or license_evidence[0].get("href") or None

        result: dict[str, object] = {
            "arxiv_id": work_id,
            "version": version,
            "versioned_id": versioned_id,
            "title": " ".join(title.split()),
            "authors": authors,
            "doi": doi,
            "license": license_value,
            "license_evidence": license_evidence,
            "metadata_url": metadata_url,
            "resolved_at": _utc_now(),
            "_metadata_bytes": api_payload if api_payload else payload,
            "_metadata_content_type": api_type if api_payload else raw_content_type,
            "_metadata_responses": metadata_responses,
        }
        return result

    def _resolve_api(self, identifier: str) -> tuple[dict[str, object], bytes, str, str]:
        normalized = normalize_arxiv_id(identifier)
        work_id, version = split_arxiv_id(normalized)
        query = urllib.parse.urlencode({"id_list": normalized, "max_results": "1"})
        url = f"https://export.arxiv.org/api/query?{query}"
        payload, final_url, content_type, raw_type = self._request(
            url,
            work_id,
            version,
            api=True,
            api_identifier=normalized,
        )
        if not content_type.endswith("xml") and b"<feed" not in payload[:512]:
            raise PaperDownloadError("arXiv API did not return Atom XML")
        metadata = _parse_api_entry(payload)
        if metadata is None:
            raise PaperDownloadError(f"arXiv API returned no metadata entry for {normalized}")
        return metadata, payload, raw_type, final_url

    def fetch(
        self, versioned_id: str, kind: Literal["html", "source", "pdf"]
    ) -> DownloadedArtifact:
        """Download and validate a version-pinned HTML, source, or PDF artifact."""
        if kind not in {"html", "source", "pdf"}:
            raise ValueError("kind must be 'html', 'source', or 'pdf'")
        normalized = normalize_arxiv_id(versioned_id)
        work_id, version = split_arxiv_id(normalized)
        if version is None:
            raise PaperDownloadError("fetch requires an explicit positive arXiv version")
        canonical_id = f"{work_id}v{version}"
        url = f"https://arxiv.org/{'src' if kind == 'source' else kind}/{urllib.parse.quote(canonical_id, safe='/')}"
        payload, final_url, content_type, raw_content_type = self._request(
            url, work_id, version
        )
        if kind == "pdf":
            if not payload.startswith(b"%PDF-"):
                raise PaperDownloadError("arXiv PDF response has no PDF magic header")
        elif kind == "html":
            self._validate_fulltext_html(payload, canonical_id, work_id, version)
        else:
            self._validate_source(payload, content_type, raw_content_type)
        return DownloadedArtifact(
            data=payload,
            url=url,
            final_url=final_url,
            content_type=raw_content_type,
            retrieved_at=_utc_now(),
            kind=kind,
            versioned_id=canonical_id,
        )

    @staticmethod
    def _validate_fulltext_html(payload: bytes, canonical_id: str, work_id: str, version: int) -> None:
        if not payload.lstrip().lower().startswith((b"<!doctype html", b"<html")) and b"<html" not in payload[:512].lower():
            raise PaperDownloadError("arXiv HTML response is not an HTML document")
        parser = _PageParser()
        parser.feed(payload.decode("utf-8", errors="replace"))
        page_ids: list[str] = []
        for value in parser.meta.get("citation_arxiv_id", []):
            match = _ANY_ID.search(value)
            if match:
                page_ids.append(normalize_arxiv_id(match.group(0)))
        for value in parser.meta.get("citation_pdf_url", []):
            page_id = _arxiv_id_from_url(value)
            if page_id:
                page_ids.append(page_id)
        for value in parser.canonical_urls:
            page_id = _arxiv_id_from_url(urllib.parse.urljoin("https://arxiv.org/", value), abs_only=True)
            if page_id:
                page_ids.append(page_id)
        if page_ids and not all(_identity_compatible(item, work_id, version) for item in page_ids):
            raise PaperDownloadError(
                f"arXiv HTML identity mismatch: requested {canonical_id}, page reports {page_ids[0]}"
            )
        article_text = " ".join(parser.article_text)
        if parser.article_elements == 0 or len(article_text.strip()) < 40:
            raise PaperDownloadError("arXiv HTML response lacks full article structure")

    @staticmethod
    def _validate_source(payload: bytes, content_type: str, raw_content_type: str) -> None:
        if not payload:
            raise PaperDownloadError("arXiv source response is empty")
        prefix = payload[:2048].lstrip().lower()
        if content_type == "text/html" or prefix.startswith((b"<!doctype html", b"<html", b"<?xml")):
            raise PaperDownloadError("arXiv source endpoint returned HTML/XML instead of source")
        if any(marker in prefix[:1024] for marker in (b"please log in", b"sign in to continue", b"request blocked", b"not found")):
            raise PaperDownloadError("arXiv source endpoint returned an error/login page")
        if payload.startswith((b"\x1f\x8b", b"PK\x03\x04", b"%PDF-")):
            return
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:"):
                return
        except (tarfile.TarError, OSError):
            pass
        text = payload[:8192].decode("utf-8", errors="ignore").lstrip("\ufeff\n\r \t")
        if not re.search(r"\\(documentclass|begin\s*\{document\}|def|input|include)\b", text):
            if "text/plain" not in raw_content_type.lower() and "application/x-tex" not in raw_content_type.lower():
                raise PaperDownloadError("arXiv source response is not a recognized source archive")
            if len(text.strip()) < 8:
                raise PaperDownloadError("arXiv source response is not valid TeX text")


class _RetryableStatus(Exception):
    def __init__(self, status: int, retry_after: str | None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


__all__ = [
    "ArxivClient",
    "DownloadedArtifact",
    "PaperDownloadError",
    "PaperUnavailable",
    "normalize_arxiv_id",
    "split_arxiv_id",
]
