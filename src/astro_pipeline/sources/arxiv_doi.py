"""Bounded, provenance-checked DOI lookups through the official arXiv Atom API.

A completed batch returns an explicit ``None`` only after verifying *all* of
its returned entries and OpenSearch pagination. Failed batches never generate
negative observations; callers may pass completed values to
``library.papers.cached_doi_arxiv`` using its existing fenced per-DOI API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
import unicodedata
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlencode, urlsplit
import xml.etree.ElementTree as ET

from astro_pipeline.library.papers import normalize_doi
from astro_pipeline.sources import simbad_refs

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
_ARXIV_ID = re.compile(
    r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7}))(?:v\d+)?\Z",
    re.IGNORECASE,
)
_DOI = re.compile(r"10\.[0-9]{4,9}/[^\s\"\x00-\x1f]+\Z", re.IGNORECASE)
_MAX_BATCH_SIZE = 20
_MAX_FEED_BYTES = 5_000_000
_HEADERS = {"User-Agent": "astro-pipeline/0.4 (mailto:cetizhao@163.com)"}


@dataclass
class ArxivDoiResults:
    """Only ``resolved`` values are safe to promote via ``cached_doi_arxiv``.

    ``resolved`` includes explicit ``None`` for verified no-match DOIs. Failed
    DOIs and never-requested/deferred DOIs are disjoint and remain retryable.
    """

    resolved: dict[str, str | None] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    deferred: list[str] = field(default_factory=list)
    rate_limited: bool = False
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "rate_limited" if self.rate_limited else "partial" if self.failed else "completed"


@dataclass
class _BatchParsed:
    resolved: dict[str, str | None]
    failed: dict[str, str]
    evidence: dict[str, dict[str, Any]]


def _title_key(title: str) -> str:
    """Case/spacing/punctuation equivalence, never a substring or fuzzy match."""
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", title).casefold()))


def _expected_titles(
    expected_titles: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    if expected_titles is None:
        return normalized
    for raw_doi, titles in expected_titles.items():
        doi = _doi(raw_doi)
        if isinstance(titles, (str, bytes)) or not isinstance(titles, Sequence):
            raise ValueError(f"Expected titles for {doi} must be a list of strings")
        for title in titles:
            if not isinstance(title, str):
                raise ValueError(f"Expected titles for {doi} must be strings")
            if _title_key(title):
                normalized.setdefault(doi, []).append(title)
    return normalized


def _doi(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("DOI must be text")
    normalized = normalize_doi(value)
    if _DOI.fullmatch(normalized) is None:
        raise ValueError(f"Invalid DOI identity: {value!r}")
    return normalized


def _id_from_entry(entry: ET.Element) -> str:
    id_text = entry.findtext(f"{_ATOM}id", default="").strip()
    title = entry.findtext(f"{_ATOM}title", default="").strip()
    if title.casefold() == "error" or "/api/errors" in id_text.casefold():
        raise ValueError(f"arXiv returned an Atom error entry: {title or id_text}")
    address = urlsplit(id_text)
    if (address.scheme not in {"http", "https"}
            or address.hostname not in {"arxiv.org", "export.arxiv.org"}
            or not address.path.startswith("/abs/")
            or address.query or address.fragment):
        raise ValueError("arXiv entry has no valid /abs/ identity")
    match = _ARXIV_ID.fullmatch(unquote(address.path.removeprefix("/abs/")))
    if match is None:
        raise ValueError("arXiv entry has an invalid article identifier")
    return match["id"]


def _entry_dois(entry: ET.Element) -> set[str]:
    identities: set[str] = set()
    for field in entry.findall(f"{_ARXIV}doi"):
        if not field.text:
            raise ValueError("arXiv DOI element has no identity")
        identities.add(_doi(field.text))
    for link in entry.findall(f"{_ATOM}link"):
        href = link.get("href", "")
        parsed = urlsplit(href)
        tagged = link.get("title", "").casefold() == "doi"
        doi_host = parsed.hostname in {"doi.org", "dx.doi.org"}
        if not tagged and not doi_host:
            continue
        if doi_host and parsed.scheme in {"http", "https"} and not parsed.query and not parsed.fragment:
            identities.add(_doi(unquote(parsed.path.lstrip("/"))))
        elif tagged and not parsed.scheme:
            identities.add(_doi(unquote(href)))
        else:
            raise ValueError("arXiv DOI link has an invalid identity")
    if not identities:
        # An entry matched by the API query but without a DOI field could be a
        # requested preprint. Never turn that uncertainty into a no-match cache.
        raise ValueError("arXiv entry lacks a DOI identity")
    return identities


def _parse_complete_feed(
    xml: bytes, *, requested: list[str], expected_titles: Mapping[str, Sequence[str]],
) -> _BatchParsed:
    if len(xml) > _MAX_FEED_BYTES:
        raise ValueError("arXiv Atom feed exceeds the bounded response size")
    root = ET.fromstring(xml)
    if root.tag != f"{_ATOM}feed":
        raise ValueError("arXiv response is not an Atom feed")
    entries = root.findall(f"{_ATOM}entry")
    # Error entries may be accompanied by incomplete/absent OpenSearch fields.
    for entry in entries:
        id_text = entry.findtext(f"{_ATOM}id", default="").strip()
        if (entry.findtext(f"{_ATOM}title", default="").strip().casefold() == "error"
                or "/api/errors" in id_text.casefold()):
            raise ValueError("arXiv returned an Atom error entry")
    try:
        total = int(root.findtext(f"{_OPENSEARCH}totalResults", default=""))
        start = int(root.findtext(f"{_OPENSEARCH}startIndex", default=""))
    except ValueError as exc:
        raise ValueError("arXiv response lacks valid OpenSearch pagination") from exc
    if start != 0 or total < 0 or len(entries) != total:
        raise ValueError(
            "arXiv response is truncated or offset: "
            f"totalResults={total}, startIndex={start}, entries={len(entries)}"
        )
    requested_set = set(requested)
    # Parse every entry before returning *any* observation. A missing identity
    # anywhere in the feed invalidates no-match conclusions for the batch.
    hits: dict[str, dict[str, set[str]]] = {doi: {} for doi in requested}
    for entry in entries:
        aid = _id_from_entry(entry)
        title_element = entry.find(f"{_ATOM}title")
        title = "" if title_element is None else "".join(title_element.itertext()).strip()
        for doi in _entry_dois(entry) & requested_set:
            hits[doi].setdefault(aid, set()).add(title)

    resolved: dict[str, str | None] = {}
    failed: dict[str, str] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for doi in requested:
        candidates = hits[doi]
        if len(candidates) <= 1:
            resolved[doi] = next(iter(candidates), None)
            continue
        expected = expected_titles.get(doi, ())
        expected_keys = {_title_key(value) for value in expected}
        candidate_evidence = [
            {
                "arxiv_id": aid,
                "titles": sorted(titles),
                "normalized_titles": sorted({_title_key(title) for title in titles if _title_key(title)}),
            }
            for aid, titles in sorted(candidates.items())
        ]
        matches = [
            aid for aid, titles in candidates.items()
            if titles and all(_title_key(title) for title in titles)
            and any(_title_key(title) in expected_keys for title in titles)
        ]
        # If any candidate lacks a title, we cannot exclude its identity with
        # title evidence. Duplicate expected titles are harmless only if they
        # still identify precisely one arXiv ID.
        chosen = (
            matches[0]
            if len(matches) == 1
            and not any(not all(_title_key(title) for title in titles) for titles in candidates.values())
            else None
        )
        evidence[doi] = {
            "candidates": candidate_evidence,
            "expected_titles": list(expected),
            "normalized_expected_titles": sorted(key for key in expected_keys if key),
            "selection": (
                {"method": "exact_normalized_title", "arxiv_id": chosen}
                if chosen is not None else None
            ),
        }
        if chosen is None:
            failed[doi] = "arXiv returned multiple article IDs for one requested DOI without a unique exact title match"
        else:
            resolved[doi] = chosen
    return _BatchParsed(resolved, failed, evidence)


def _request_batch(
    dois: list[str], *, timeout: float, max_retries: int,
    expected_titles: Mapping[str, Sequence[str]],
) -> _BatchParsed:
    query = " OR ".join(f'doi:"{doi}"' for doi in dois)
    # More than one article may carry a DOI; a bounded surplus lets us detect
    # ambiguities while OpenSearch totals flag responses larger than the cap.
    max_results = min(200, max(20, len(dois) * 5))
    url = "https://export.arxiv.org/api/query?" + urlencode({
        "search_query": query, "start": 0, "max_results": max_results,
    })
    for attempt in range(max_retries):
        response = simbad_refs._arxiv_http_get(url, timeout=timeout, headers=_HEADERS)
        if response.status_code == 429:
            retry_after = simbad_refs._arxiv_retry_after(response.headers.get("Retry-After", ""))
            if (attempt >= 1 or attempt + 1 >= max_retries
                    or retry_after is not None and retry_after > simbad_refs._ARXIV_MAX_RETRY_AFTER):
                raise simbad_refs.ArxivRateLimitError(
                    "arXiv HTTP 429 for DOI batch; defer remaining DOI lookups "
                    f"(Retry-After={retry_after})"
                )
            time.sleep(retry_after if retry_after is not None else 5.0)
            continue
        response.raise_for_status()
        return _parse_complete_feed(response.content, requested=dois, expected_titles=expected_titles)
    raise RuntimeError("arXiv DOI batch exhausted retries")


def arxiv_ids_from_dois(
    dois: Iterable[str], *, batch_size: int = 20, timeout: float = 30.0,
    max_retries: int = 4, expected_titles: Mapping[str, Sequence[str]] | None = None,
) -> ArxivDoiResults:
    """Resolve explicit DOIs in serialized OR batches of at most 20.

    Values are keyed by exact normalized DOI, never result ordering. Only fully
    checked feeds contribute results (including explicit ``None``). When one DOI
    has multiple arXiv IDs, only a unique exactly normalized title match against
    its supplied expected_titles may resolve it; otherwise only that DOI fails.
    Structural/transport failures invalidate their entire batch and defer later
    batches. This function does not write a cache or published snapshot.
    """
    if not 1 <= batch_size <= _MAX_BATCH_SIZE:
        raise ValueError("batch_size must be between 1 and 20")
    if not 1 <= max_retries <= 4 or not 0 < timeout <= 120:
        raise ValueError("max_retries must be 1-4 and timeout must be 0-120 seconds")
    unique = list(dict.fromkeys(_doi(doi) for doi in dois))
    expected = _expected_titles(expected_titles)
    outcome = ArxivDoiResults()
    for start in range(0, len(unique), batch_size):
        batch = unique[start:start + batch_size]
        try:
            parsed = _request_batch(
                batch, timeout=timeout, max_retries=max_retries, expected_titles=expected,
            )
        except Exception as exc:
            outcome.failed.update({doi: str(exc) for doi in batch})
            outcome.deferred.extend(unique[start + len(batch):])
            outcome.rate_limited = isinstance(exc, simbad_refs.ArxivRateLimitError)
            break
        outcome.resolved.update(parsed.resolved)
        outcome.failed.update(parsed.failed)
        outcome.evidence.update(parsed.evidence)
    return outcome
