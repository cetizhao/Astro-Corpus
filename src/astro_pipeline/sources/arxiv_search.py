"""arXiv keyword search for paper *discovery* only.

NOTE: This module is for finding candidate papers by topic (e.g.
``search_arxiv("TW Hydrae association")``). It does NOT establish which sources
a paper studies — that is the job of the authoritative SIMBAD-grounded linker
in :mod:`astro_pipeline.sources.simbad_refs`.

The previous version of this file contained ``match_papers_to_catalog`` /
``build_paper_edges`` / ``search_and_link``, which matched a source to a paper
when the source's id (or a truncated coordinate) appeared as a *substring* of
the paper text. That produced garbage (94 papers × all 49 sources) and has been
removed. Use ``simbad_refs.build_paper_edges`` for source↔paper membership.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NAMESPACE = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARXIV_ID_RE = re.compile(r"(?:arxiv:|https?://(?:export\.)?arxiv\.org/abs/)((?:\d{4}\.\d{4,5}|astro-ph/\d{7})(?:v\d+)?)$", re.IGNORECASE)
USER_AGENT = "astro-pipeline/0.2 (mailto:cetizhao@163.com)"


class ArxivSearchRateLimited(RuntimeError):
    """Explicit rate limit, not an empty search result or a parser failure."""


@dataclass
class ArxivPaper:
    arxiv_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    published: str = ""


def search_arxiv(
    search_query: str,
    max_results: int = 50,
    start: int = 0,
    max_retries: int = 3,
) -> list[ArxivPaper]:
    """Search arXiv by keyword. Returns candidate papers for *discovery* only.

    This does NOT tell you which sources a paper studies. For authoritative
    source↔paper membership use
    :func:`astro_pipeline.sources.simbad_refs.build_paper_edges`.
    """
    params = {
        "search_query": search_query,
        "start": start,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
    logger.info("arXiv discovery search: %s", search_query[:100])

    for attempt in range(max_retries):
        try:
            with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
                resp = client.get(url)
                if resp.status_code == 429:
                    raise ArxivSearchRateLimited(
                        "arXiv HTTP 429：服务限流，请稍后重试；Retry-After="
                        + resp.headers.get("Retry-After", "未提供")
                    )
                resp.raise_for_status()
                try:
                    root = ET.fromstring(resp.content)
                except ET.ParseError as exc:
                    raise RuntimeError("arXiv 返回非XML内容（可能为代理或服务错误页），本次未完成检索") from exc
                if root.tag != "{http://www.w3.org/2005/Atom}feed":
                    raise RuntimeError("arXiv 返回的内容不是论文检索Atom feed")
                break
        except ArxivSearchRateLimited:
            raise
        except Exception as e:
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 5
                logger.warning("arXiv request failed: %s, retrying in %ds...", e, wait)
                time.sleep(wait)
            else:
                raise

    if max_retries < 1:
        raise ValueError("max_retries must be positive")
    papers: list[ArxivPaper] = []

    for entry in root.findall("atom:entry", ARXIV_NAMESPACE):
        arxiv_id = _extract_id(entry)
        if not arxiv_id:
            continue
        title = _text(entry, "atom:title")
        abstract = _text(entry, "atom:summary")
        published = _text(entry, "atom:published")
        authors = [_text(a, "atom:name") for a in entry.findall("atom:author", ARXIV_NAMESPACE)]
        papers.append(ArxivPaper(
            arxiv_id=arxiv_id, title=title, authors=authors,
            abstract=abstract, published=published,
        ))

    logger.info("arXiv returned %d candidate papers for query: %s", len(papers), search_query[:60])
    return papers


def _extract_id(entry: ET.Element) -> str:
    id_elem = entry.find("atom:id", ARXIV_NAMESPACE)
    if id_elem is not None and id_elem.text:
        m = ARXIV_ID_RE.search(id_elem.text)
        if m:
            return m.group(1)
    return ""


def _text(elem: ET.Element, tag: str) -> str:
    child = elem.find(tag, ARXIV_NAMESPACE)
    if child is not None and child.text:
        return child.text.strip()
    return ""
