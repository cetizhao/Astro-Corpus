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

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

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
REF_COLUMNS = [
    "gaia_id",
    "main_id",
    "bibcode",
    "arxiv_id",
    "title",
    "year",
    "journal",
    "doi",
]


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
    error: Optional[str] = None


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
    headers = {"User-Agent": "astro-pipeline/0.4 (mailto:cetizhao@163.com)"}

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
        sp.error = str(e)
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
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
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
    cache_path : if given, the incrementally updated reference rows are cached
        here as Parquet. A sibling ``*.checkpoint.json`` records sources that
        were queried successfully but had zero references, plus retryable
        failures. Both are reused on subsequent runs.
    enrich : if False, skip the per-bibcode metadata fetch.
    resolve_doi : if False, skip the (rate-limited) arXiv DOI->id lookup. Set to
        False for a fast first pass; bibcodes/DOIs/titles are still captured.
    """
    gaia_ids = list(dict.fromkeys(str(x) for x in catalog_df[id_col].astype(str)))
    target_ids = set(gaia_ids)

    # ----- Stage 1: source -> bibcodes (cached) -----
    cache = pd.DataFrame(columns=REF_COLUMNS)
    cache_file = Path(cache_path) if cache_path else None
    checkpoint_file = _simbad_checkpoint_path(cache_file) if cache_file else None
    if cache_file and cache_file.exists():
        try:
            cache = _normalize_refs(pd.read_parquet(cache_file))
            logger.info("Loaded %d cached SIMBAD refs from %s", len(cache), cache_file)
        except Exception as e:
            logger.warning("Could not read SIMBAD ref cache: %s", e)
            cache = pd.DataFrame(columns=REF_COLUMNS)

    checkpoint = _load_simbad_checkpoint(checkpoint_file)
    all_completed = {str(value) for value in checkpoint.get("completed", [])}
    all_failures = {
        str(key): str(value)
        for key, value in checkpoint.get("failed", {}).items()
    }
    # Old caches predate the checkpoint file. Any source with stored rows was
    # necessarily queried successfully, so it is safe to infer completion.
    if not cache.empty:
        all_completed.update(cache["gaia_id"].dropna().astype(str))
    completed = all_completed & target_ids
    failures = {
        key: value for key, value in all_failures.items() if key in target_ids
    }
    for gid in completed:
        failures.pop(gid, None)

    processed_since_flush = 0

    def flush(*, final: bool = False) -> None:
        nonlocal processed_since_flush
        if cache_file is not None:
            _write_parquet_atomic(_normalize_refs(cache), cache_file)
        if checkpoint_file is not None:
            persisted_completed = (all_completed - target_ids) | completed
            persisted_failures = {
                key: value
                for key, value in all_failures.items()
                if key not in target_ids
            }
            persisted_failures.update(failures)
            _write_simbad_checkpoint(
                checkpoint_file,
                completed=persisted_completed,
                failures=persisted_failures,
                total=len(persisted_completed | set(persisted_failures) | target_ids),
            )
        if progress_callback:
            progress_callback(
                {
                    "status": (
                        "partial"
                        if final and failures
                        else "completed"
                        if final
                        else "running"
                    ),
                    "total": len(gaia_ids),
                    "completed": len(completed),
                    "failed": len(failures),
                    "remaining": len(target_ids - completed),
                }
            )
        processed_since_flush = 0

    if progress_callback:
        progress_callback(
            {
                "status": "running",
                "total": len(gaia_ids),
                "completed": len(completed),
                "failed": len(failures),
                "remaining": len(target_ids - completed),
            }
        )

    try:
        for gid in gaia_ids:
            if gid in completed:
                continue
            try:
                sp = get_source_bibcodes(gid)
            except Exception as exc:
                logger.warning("SIMBAD lookup failed for %s: %s", gid, exc)
                sp = SourcePapers(gaia_id=gid, error=str(exc))

            if sp.error:
                failures[gid] = sp.error
            else:
                new_rows = [
                    {
                        "gaia_id": gid,
                        "main_id": sp.main_id,
                        "bibcode": paper.bibcode,
                        "arxiv_id": paper.arxiv_id or "",
                        "year": paper.year,
                        "journal": paper.journal,
                        "title": paper.title,
                        "doi": paper.doi,
                    }
                    for paper in sp.papers
                ]
                if new_rows:
                    cache = _normalize_refs(
                        pd.concat([cache, pd.DataFrame(new_rows)], ignore_index=True)
                    )
                # A successful query with no SIMBAD object or no bibliography
                # is still complete. The checkpoint records it without adding
                # a fake/empty paper row to the training table.
                completed.add(gid)
                failures.pop(gid, None)

            processed_since_flush += 1
            if progress_every and processed_since_flush >= progress_every:
                flush()
                logger.info(
                    "SIMBAD progress: %d/%d sources (%d retryable failures)",
                    len(completed),
                    len(gaia_ids),
                    len(failures),
                )
            if sleep_seconds:
                time.sleep(sleep_seconds)
    finally:
        # Cache rows before completion state. If the second atomic replace is
        # interrupted, a rerun may repeat a query but cannot silently lose rows.
        if processed_since_flush or cache_file is not None:
            flush(final=True)

    refs_table = _normalize_refs(
        cache[cache["gaia_id"].astype(str).isin(target_ids)].copy()
        if not cache.empty
        else cache
    )
    source_progress = {
        "total": len(gaia_ids),
        "completed": len(completed),
        "failed": len(failures),
        "remaining": len(target_ids - completed),
        "checkpoint_path": str(checkpoint_file.resolve()) if checkpoint_file else "",
    }

    if refs_table.empty:
        # Empty result must still carry the full schema so downstream (viz,
        # JSONL) can group by bibcode without KeyError on zero-edge corpora.
        edge_cols = ["source_id", "arxiv_id", "bibcode", "title", "year", "journal", "doi", "main_id"]
        edges = PaperEdgeTable()
        edges.df = pd.DataFrame(columns=edge_cols)
        refs_table.attrs["source_progress"] = source_progress
        return edges, refs_table

    outside_target = (
        cache[~cache["gaia_id"].astype(str).isin(target_ids)].copy()
        if not cache.empty
        else pd.DataFrame(columns=REF_COLUMNS)
    )

    # ----- Stage 2: per-bibcode metadata enrichment (title/DOI/arXiv id) -----
    if enrich:
        refs_table = _enrich_refs_table(
            refs_table, sleep_seconds=sleep_seconds, resolve_doi=resolve_doi,
            cache_path=cache_path, preserved_refs=outside_target,
        )

    # Persist cache (includes enriched metadata now).
    if cache_path:
        persisted_refs = _merge_ref_frames(outside_target, refs_table)
        _write_parquet_atomic(persisted_refs, Path(cache_path))
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
    refs_table.attrs["source_progress"] = source_progress
    return edges, refs_table


def _enrich_refs_table(
    refs_table: pd.DataFrame,
    sleep_seconds: float = 0.3,
    resolve_doi: bool = True,
    cache_path: Optional[Path] = None,
    preserved_refs: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Fill in title/DOI/arxiv_id for each unique bibcode in ``refs_table``.

    Already-complete rows (non-empty title AND (arxiv_id OR not resolve_doi))
    are skipped. Progress is checkpointed to ``cache_path`` periodically so a
    rate-limit interruption is never lost work.
    """
    df = refs_table.copy()
    need_title = df["title"].astype(str).str.strip() == ""
    need_arxiv = df["arxiv_id"].astype(str).str.strip() == ""
    need = df[need_title | (need_arxiv if resolve_doi else False)]
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
                _write_parquet_atomic(
                    _merge_ref_frames(preserved_refs, df), Path(cache_path)
                )
            logger.info("Enrich progress: %d/%d bibcodes", i, len(unique))
        if sleep_seconds:
            time.sleep(sleep_seconds)

    return _apply_meta(df, meta)


def _normalize_refs(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in REF_COLUMNS:
        if column not in frame.columns:
            frame[column] = None if column == "year" else ""
    frame = frame[REF_COLUMNS]
    for column in (set(REF_COLUMNS) - {"year"}):
        frame[column] = frame[column].fillna("")
    if not frame.empty:
        frame["gaia_id"] = frame["gaia_id"].astype(str)
        frame = frame.drop_duplicates(subset=["gaia_id", "bibcode"], keep="last")
        frame = frame.reset_index(drop=True)
    return frame


def _merge_ref_frames(*frames: Optional[pd.DataFrame]) -> pd.DataFrame:
    present = [frame for frame in frames if frame is not None and not frame.empty]
    if not present:
        return pd.DataFrame(columns=REF_COLUMNS)
    return _normalize_refs(pd.concat(present, ignore_index=True))


def _simbad_checkpoint_path(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.stem}.checkpoint.json")


def _load_simbad_checkpoint(path: Optional[Path]) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"completed": [], "failed": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read SIMBAD checkpoint %s: %s", path, exc)
        return {"completed": [], "failed": {}}
    if not isinstance(data.get("failed", {}), dict):
        data["failed"] = {str(value): "previous lookup failed" for value in data["failed"]}
    return data


def _write_simbad_checkpoint(
    path: Path,
    *,
    completed: set[str],
    failures: dict[str, str],
    total: int,
) -> None:
    payload = {
        "schema_version": "1.0",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": total,
        "completed": sorted(completed),
        "failed": dict(sorted(failures.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_parquet_atomic(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


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
