"""SIMBAD-grounded source ↔ paper linking.

This module replaces the previous substring-matching paper linker, which produced
garbage results (94 papers × all sources) because a Gaia source_id never appears
in an abstract and the coordinate fallback matched *any* paper in the region.

SIMBAD maintains a **curated** bibliography per object: every bibcode it lists is
a paper that genuinely cites/studies that source. We resolve each catalog source
(via its Gaia DR3 id) to SIMBAD, read its reference list, and convert each
bibcode to an arXiv id where possible. The result is an authoritative, many-to-many
edge table: (gaia_id, bibcode, arxiv_id, title, year, first_author).

For papers without an arXiv id we keep the bibcode + journal reference so the
link is never lost.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from astro_pipeline.models.paper import PaperEdgeTable

logger = logging.getLogger(__name__)

# After the 'arXiv' marker in an ADS bibcode the identifier is encoded WITHOUT a
# dot: <YYMM><NNNNN>. Old (pre-2007) identifiers are <YYMM><NNN>.
_ARXIV_BIB_RE = re.compile(r"^\d{4}arXiv(\d{4})\.?(\d{3,5})", re.IGNORECASE)
# Fall back: an explicit dot form that occasionally appears.
_ARXIV_PLAIN_RE = re.compile(r"^\d{4}\.\d{4,5}$")

GAIA_ID_COLUMN = "Gaia"
RA_COLUMN = "RAICRS"
DEC_COLUMN = "DEICRS"


@dataclass
class PaperRecord:
    bibcode: str
    arxiv_id: Optional[str] = None
    title: str = ""
    year: Optional[int] = None
    first_author: str = ""
    journal: str = ""
    doi: str = ""
    url: str = ""

    def best_url(self) -> str:
        """A clickable URL for the paper, preferring arXiv then DOI."""
        if self.arxiv_id:
            return f"https://arxiv.org/abs/{self.arxiv_id}"
        if self.doi:
            return f"https://doi.org/{self.doi}"
        return f"https://ui.adsabs.harvard.edu/abs/{self.bibcode}"


@dataclass
class SourcePapers:
    gaia_id: str
    main_id: str = ""
    bibcodes: list[str] = field(default_factory=list)
    papers: list[PaperRecord] = field(default_factory=list)
    resolved: bool = False


def bibcode_to_arxiv(bibcode: str) -> Optional[str]:
    """Extract an arXiv id from an ADS bibcode, or None.

    arXiv preprint bibcodes encode the id directly, e.g. ``2018arXiv180605059X``
    → ``1806.05059``. Pure offline, no network.
    """
    if not bibcode:
        return None
    m = _ARXIV_BIB_RE.match(bibcode)
    if m:
        yymm, nnnn = m.group(1), m.group(2)
        return f"{yymm}.{nnnn}"
    if _ARXIV_PLAIN_RE.match(bibcode):
        return bibcode
    return None


def _bibcode_year(bibcode: str) -> Optional[int]:
    if not bibcode or len(bibcode) < 4:
        return None
    try:
        y = int(bibcode[:4])
    except ValueError:
        return None
    return y if 1800 <= y <= 2100 else None


def _bibcode_journal(bibcode: str) -> str:
    """Best-effort journal token from a bibcode, e.g. 2018A&A...620A.172Z -> 'A&A'."""
    if len(bibcode) < 5:
        return ""
    tail = bibcode[4:]
    m = re.match(r"^([A-Za-z&.+\- ]+?)\d", tail)
    return m.group(1).rstrip(".") if m else ""


def arxiv_id_from_doi(
    doi: str,
    timeout: float = 30.0,
    max_retries: int = 4,
) -> Optional[str]:
    """Resolve a DOI to its arXiv id via the arXiv API (no key required).

    Returns None if no matching arXiv preprint exists. Handles the arXiv API's
    aggressive rate limiting (HTTP 429) with exponential backoff. arXiv asks
    clients to wait >=3s between requests, so on 429 we back off and retry.
    Pure network fallback used only for journal bibcodes that don't already
    encode an arXiv id.
    """
    if not doi:
        return None
    import time
    import urllib.parse
    import httpx
    import xml.etree.ElementTree as ET

    params = urllib.parse.urlencode({"search_query": f"doi:{doi}", "max_results": 1})
    url = f"https://export.arxiv.org/api/query?{params}"
    headers = {"User-Agent": "astro-pipeline/0.2 (mailto:cetizhao@163.com)"}

    for attempt in range(max_retries):
        try:
            resp = httpx.get(url, timeout=timeout, headers=headers)
            if resp.status_code == 429:
                # Respect arXiv rate limiting: back off, then retry.
                wait = 5 * (attempt + 1)
                logger.debug("arXiv 429 for DOI %s; backing off %ds", doi, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entry = root.find("atom:entry", ns)
            if entry is None:
                return None
            id_el = entry.find("atom:id", ns)
            if id_el is None or not id_el.text:
                return None
            m = re.search(r"arxiv\.org/abs/(.+?)(?:v\d+)?$", id_el.text.strip())
            return m.group(1) if m else None
        except Exception as e:
            logger.debug("arXiv DOI lookup failed for %s: %s", doi, e)
            return None
    logger.debug("arXiv DOI lookup exhausted retries for %s", doi)
    return None


def enrich_bibcode(bibcode: str, existing_arxiv: Optional[str] = None, resolve_doi: bool = True) -> PaperRecord:
    """Fetch title/DOI/year/journal for a bibcode from SIMBAD, and resolve an
    arXiv id when one is not already embedded in the bibcode.

    Network: one SIMBAD ``query_bibcode`` call + (only if ``resolve_doi`` and a
    DOI exists, with no embedded arXiv id) one arXiv DOI lookup. The SIMBAD call
    is reliable/unthrottled; the arXiv DOI lookup is rate-limited and optional.
    On any failure a PaperRecord with just the bibcode is returned so enrichment
    never breaks the pipeline.
    """
    rec = PaperRecord(
        bibcode=bibcode,
        arxiv_id=existing_arxiv or bibcode_to_arxiv(bibcode),
        year=_bibcode_year(bibcode),
        journal=_bibcode_journal(bibcode),
    )
    try:
        from astroquery.simbad import Simbad  # local import

        simbad = Simbad()
        res = simbad.query_bibcode(bibcode)
        if res is not None and len(res) > 0:
            row = res.to_pandas().iloc[0]
            rec.title = str(row.get("title", "") or "").strip()
            rec.doi = str(row.get("doi", "") or "").strip()
            rec.journal = str(row.get("journal", "") or "").strip() or rec.journal
            y = row.get("year", None)
            if y is not None and str(y).strip() != "":
                try:
                    rec.year = int(float(y))
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        logger.debug("SIMBAD query_bibcode failed for %s: %s", bibcode, e)

    # If still no arXiv id but we have a DOI, ask the arXiv API (optional).
    if resolve_doi and not rec.arxiv_id and rec.doi:
        rec.arxiv_id = arxiv_id_from_doi(rec.doi)

    return rec


def resolve_source(gaia_id: str, timeout: Optional[int] = None) -> tuple[str, str]:
    """Resolve a Gaia DR3 source id to (simbad_main_id, simbad_name_query).

    Returns a query string usable with ``Simbad.query_object``.
    Raises if the source is not in SIMBAD.
    """
    from astroquery.simbad import Simbad  # local import: heavy + networked

    simbad = Simbad()
    simbad.add_votable_fields("bibcodelist")
    query = f"Gaia DR3 {gaia_id}"
    res = simbad.query_object(query)
    if res is None or len(res) == 0:
        raise LookupError(f"SIMBAD has no object for Gaia DR3 {gaia_id}")
    df = res.to_pandas()
    main_id = str(df["main_id"].iloc[0])
    return main_id, query


def get_source_bibcodes(gaia_id: str) -> SourcePapers:
    """Fetch SIMBAD's curated reference list for one Gaia DR3 source.

    Returns a SourcePapers with ``resolved=False`` if SIMBAD has no entry.
    """
    from astroquery.simbad import Simbad  # local import

    sp = SourcePapers(gaia_id=str(gaia_id))
    simbad = Simbad()
    simbad.add_votable_fields("bibcodelist")
    query = f"Gaia DR3 {gaia_id}"
    try:
        res = simbad.query_object(query)
    except Exception as e:  # network / server error
        logger.warning("SIMBAD query failed for %s: %s", gaia_id, e)
        return sp
    if res is None or len(res) == 0:
        logger.debug("SIMBAD: no entry for Gaia DR3 %s", gaia_id)
        return sp

    df = res.to_pandas()
    sp.main_id = str(df["main_id"].iloc[0])
    raw = df["biblio"].iloc[0] if "biblio" in df.columns else ""
    bibcodes = [b.strip() for b in str(raw).split("|") if b.strip()] if raw else []
    sp.bibcodes = bibcodes
    sp.resolved = True

    for bc in bibcodes:
        sp.papers.append(
            PaperRecord(
                bibcode=bc,
                arxiv_id=bibcode_to_arxiv(bc),
                year=_bibcode_year(bc),
                journal=_bibcode_journal(bc),
            )
        )
    logger.info("SIMBAD %s (%s): %d references", gaia_id, sp.main_id, len(bibcodes))
    return sp


def build_paper_edges(
    catalog_df: pd.DataFrame,
    id_col: str = GAIA_ID_COLUMN,
    sleep_seconds: float = 0.3,
    progress_every: int = 10,
    cache_path: Optional[Path] = None,
    enrich: bool = True,
    resolve_doi: bool = True,
) -> tuple[PaperEdgeTable, pd.DataFrame]:
    """Build authoritative source↔paper edges for a catalog via SIMBAD.

    Two-stage:
      1. For each source, fetch SIMBAD's curated reference list (cached to
         ``cache_path`` so the slow per-source queries are never repeated).
      2. (optional, ``enrich=True``) For each unique bibcode, fetch
         title/DOI/year/journal (SIMBAD, reliable) and, if ``resolve_doi``,
         resolve an arXiv id via the rate-limited arXiv API. Deduplicated
         globally because the same paper appears under many sources.

    Parameters
    ----------
    catalog_df : catalog with a Gaia source_id column.
    id_col : name of the Gaia source_id column in ``catalog_df``.
    sleep_seconds : pause between SIMBAD requests (be polite to the service).
    cache_path : if given, the (incrementally updated) reference table is cached
        here as parquet and re-used on subsequent runs (offline-friendly).
    enrich : if False, skip the per-bibcode metadata fetch.
    resolve_doi : if False, skip the (rate-limited) arXiv DOI->id lookup. Set to
        False for a fast first pass; bibcodes/DOIs/titles are still captured.
    """
    gaia_ids = [str(x) for x in catalog_df[id_col].astype(str).tolist()]

    # ----- Stage 1: source -> bibcodes (cached) -----
    cached: pd.DataFrame = pd.DataFrame()
    if cache_path and Path(cache_path).exists():
        try:
            cached = pd.read_parquet(cache_path)
            logger.info("Loaded %d cached SIMBAD refs from %s", len(cached), cache_path)
        except Exception as e:
            logger.warning("Could not read SIMBAD ref cache: %s", e)
            cached = pd.DataFrame()

    rows: list[dict] = []
    for i, gid in enumerate(gaia_ids, start=1):
        cached_for_source = (
            cached[cached["gaia_id"].astype(str) == gid] if not cached.empty else pd.DataFrame()
        )
        if not cached_for_source.empty:
            main_id = str(cached_for_source["main_id"].iloc[0])
            for _, r in cached_for_source.iterrows():
                rows.append({
                    "gaia_id": gid, "main_id": main_id,
                    "bibcode": str(r["bibcode"]),
                    "arxiv_id": (r.get("arxiv_id") or bibcode_to_arxiv(str(r["bibcode"])) or ""),
                    "year": r.get("year"),
                    "journal": r.get("journal", ""),
                    "title": r.get("title", ""),
                    "doi": r.get("doi", ""),
                })
            continue

        try:
            sp = get_source_bibcodes(gid)
        except Exception as e:
            logger.warning("SIMBAD lookup failed for %s: %s", gid, e)
            continue
        for p in sp.papers:
            rows.append({
                "gaia_id": gid, "main_id": sp.main_id,
                "bibcode": p.bibcode, "arxiv_id": p.arxiv_id or "",
                "year": p.year, "journal": p.journal,
                "title": "", "doi": "",
            })
        if progress_every and i % progress_every == 0:
            logger.info("SIMBAD progress: %d/%d sources", i, len(gaia_ids))
        if sleep_seconds:
            time.sleep(sleep_seconds)

    if not rows:
        return PaperEdgeTable(), pd.DataFrame(
            columns=["gaia_id", "main_id", "bibcode", "arxiv_id", "title", "year", "journal", "doi"]
        )

    refs_table = pd.DataFrame(rows)

    # ----- Stage 2: per-bibcode metadata enrichment (title/DOI/arXiv id) -----
    if enrich:
        refs_table = _enrich_refs_table(
            refs_table, sleep_seconds=sleep_seconds, resolve_doi=resolve_doi,
            cache_path=cache_path,
        )

    # Persist cache (includes enriched metadata now).
    if cache_path:
        refs_table.to_parquet(cache_path, index=False)
        logger.info("Cached %d SIMBAD refs to %s", len(refs_table), cache_path)

    # ----- Build the edge table -----
    edges = PaperEdgeTable()
    edge_df = refs_table.rename(columns={"gaia_id": "source_id"}).copy()
    edge_df["arxiv_id"] = edge_df["arxiv_id"].fillna("").astype(str)
    keep = ["source_id", "arxiv_id", "bibcode", "title", "year", "journal", "doi", "main_id"]
    edge_df = edge_df[[c for c in keep if c in edge_df.columns]]
    edges.df = edge_df.drop_duplicates(subset=["source_id", "bibcode"]).reset_index(drop=True)

    n_arxiv = int((edges.df["arxiv_id"].astype(str) != "").sum())
    logger.info(
        "Built %d paper edges across %d sources (%d edges have an arXiv id)",
        len(edges), refs_table["gaia_id"].nunique(), n_arxiv,
    )
    return edges, refs_table


def _enrich_refs_table(
    refs_table: pd.DataFrame,
    sleep_seconds: float = 0.3,
    resolve_doi: bool = True,
    cache_path: Optional[Path] = None,
) -> pd.DataFrame:
    """Fill in title/DOI/arxiv_id for each unique bibcode in ``refs_table``.

    Already-complete rows (non-empty title AND (arxiv_id OR not resolve_doi))
    are skipped. Progress is checkpointed to ``cache_path`` periodically so a
    rate-limit interruption is never lost work.
    """
    df = refs_table.copy()
    need = df[(df["title"].astype(str) == "") | (df["arxiv_id"].astype(str) == "")]
    unique = need["bibcode"].drop_duplicates().tolist()
    if not unique:
        logger.info("All %d bibcodes already enriched; skipping metadata fetch", df["bibcode"].nunique())
        return df
    logger.info("Enriching metadata for %d unique bibcodes (resolve_doi=%s)...", len(unique), resolve_doi)

    meta: dict[str, PaperRecord] = {}
    checkpoint_every = 25
    for i, bc in enumerate(unique, start=1):
        existing = df.loc[df["bibcode"] == bc, "arxiv_id"].dropna()
        existing_arxiv = existing.iloc[0] if len(existing) and str(existing.iloc[0]) else None
        try:
            meta[bc] = enrich_bibcode(bc, existing_arxiv=existing_arxiv, resolve_doi=resolve_doi)
        except Exception as e:
            logger.warning("enrich_bibcode failed for %s: %s", bc, e)
            meta[bc] = PaperRecord(bibcode=bc)
        if i % checkpoint_every == 0:
            df = _apply_meta(df, meta)
            if cache_path:
                df.to_parquet(cache_path, index=False)
            logger.info("Enrich progress: %d/%d bibcodes", i, len(unique))
        if sleep_seconds:
            time.sleep(sleep_seconds)

    return _apply_meta(df, meta)


def _apply_meta(df: pd.DataFrame, meta: dict[str, PaperRecord]) -> pd.DataFrame:
    for bc, rec in meta.items():
        mask = df["bibcode"] == bc
        if rec.title:
            df.loc[mask, "title"] = rec.title
        if rec.doi:
            df.loc[mask, "doi"] = rec.doi
        if rec.arxiv_id:
            df.loc[mask, "arxiv_id"] = rec.arxiv_id
        if rec.year is not None:
            df.loc[mask, "year"] = rec.year
        if rec.journal:
            df.loc[mask, "journal"] = rec.journal
    return df
