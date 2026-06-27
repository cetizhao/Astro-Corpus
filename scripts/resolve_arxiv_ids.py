#!/usr/bin/env python3
"""Backfill ``arxiv_id`` in paper_edges.parquet via the batched arXiv DOI API.

The build script ``build_twa_paper_edges.py`` fills ``title/doi/year/journal``
via SIMBAD, but resolving the arXiv id serially (one DOI per request, 3s rate
limit) takes ~2.6h for ~3000 unique papers. arXiv's Atom API accepts a batched
boolean OR query (``doi:X OR doi:Y OR ...``), so we resolve ~20 DOIs per
request and finish in ~15 minutes instead.

Resumable: re-running skips bibcodes whose arxiv_id is already filled.
Idempotent: a one-time ``.bak`` backup of each target file is created.

Usage:
    python scripts/resolve_arxiv_ids.py                 # default: 20/batch, 3s sleep
    python scripts/resolve_arxiv_ids.py --max-batches 3 # smoke test (3 batches only)
"""
from __future__ import annotations

import argparse
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("resolve_arxiv")

DATA = Path("data/twa")
DEFAULT_TARGETS = [DATA / "paper_edges.parquet", DATA / "simbad_refs.parquet"]
UA = "astro-pipeline/0.2 (mailto:cetizhao@163.com)"


def fetch_batch(dois: list[str], timeout: float = 30.0) -> dict[str, str]:
    """Query arXiv once with a batched OR query; return {doi: arxiv_id}."""
    if not dois:
        return {}
    q = " OR ".join(f"doi:{d}" for d in dois)
    params = urllib.parse.urlencode({"search_query": q, "max_results": len(dois) * 2})
    url = f"https://export.arxiv.org/api/query?{params}"
    resp = httpx.get(url, timeout=timeout, headers={"User-Agent": UA})
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    out: dict[str, str] = {}
    for e in root.findall("atom:entry", ns):
        id_el = e.find("atom:id", ns)
        if id_el is None or not id_el.text:
            continue
        m = re.search(r"arxiv\.org/abs/(.+?)(?:v\d+)?$", id_el.text.strip())
        if not m:
            continue
        aid = m.group(1)
        for link in e.findall("atom:link", ns):
            if link.attrib.get("title") == "doi":
                href = link.attrib.get("href", "")
                doi = href.replace("http://dx.doi.org/", "").replace("https://doi.org/", "")
                if doi:
                    out[doi] = aid
    return out


def _backup_once(path: Path) -> None:
    bak = path.with_suffix(path.suffix + ".bak")
    if not bak.exists() and path.exists():
        bak.write_bytes(path.read_bytes())
        logger.info("  backed up %s -> %s", path.name, bak.name)


def apply_map(path: Path, doi_to_arxiv: dict[str, str]) -> int:
    """Apply {doi: arxiv_id} to a parquet with bibcode/doi/arxiv_id columns."""
    if not path.exists():
        return 0
    df = pd.read_parquet(path)
    if "doi" not in df.columns or "arxiv_id" not in df.columns:
        return 0
    df["arxiv_id"] = df["arxiv_id"].fillna("").astype(str)
    df["doi"] = df["doi"].fillna("").astype(str)
    _backup_once(path)
    changed = 0
    for doi, aid in doi_to_arxiv.items():
        mask = (df["doi"] == doi) & (df["arxiv_id"] == "")
        n = int(mask.sum())
        if n:
            df.loc[mask, "arxiv_id"] = aid
            changed += n
    if changed:
        df.to_parquet(path, index=False)
    return changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=3.0, help="Seconds between batches (arXiv asks >=3s).")
    ap.add_argument("--max-batches", type=int, default=0, help="Stop after N batches (0 = no limit).")
    ap.add_argument("--target", type=Path, action="append", help="Add a target parquet (default: paper_edges + simbad_refs).")
    args = ap.parse_args()

    targets = args.target if args.target else DEFAULT_TARGETS
    edges_path = targets[0]
    if not edges_path.exists():
        logger.error("Primary target not found: %s", edges_path)
        raise SystemExit(1)

    df = pd.read_parquet(edges_path)
    df["arxiv_id"] = df["arxiv_id"].fillna("").astype(str)
    df["doi"] = df["doi"].fillna("").astype(str)

    need_mask = (df["arxiv_id"] == "") & (df["doi"] != "")
    unique_dois = sorted(df.loc[need_mask, "doi"].drop_duplicates().tolist())
    total_batches = (len(unique_dois) + args.batch_size - 1) // args.batch_size
    logger.info("Edges: %d; rows needing arxiv_id: %d; unique DOIs to query: %d (in %d batches)",
                len(df), int(need_mask.sum()), len(unique_dois), total_batches)

    _backup_once(edges_path)
    hits_total = 0
    for i in range(0, len(unique_dois), args.batch_size):
        batch = unique_dois[i:i + args.batch_size]
        batch_no = i // args.batch_size + 1
        m: dict[str, str] = {}
        for attempt in range(4):
            try:
                t0 = time.time()
                m = fetch_batch(batch)
                elapsed = time.time() - t0
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    wait = 5 * (attempt + 1)
                    logger.warning("  batch %d: 429; backing off %ds", batch_no, wait)
                    time.sleep(wait)
                    continue
                raise
        else:
            logger.warning("  batch %d: exhausted retries; skipping", batch_no)
            m = {}

        # Apply to all targets.
        for path in targets:
            n = apply_map(path, m)
            if n and path == edges_path:
                hits_total += n

        # Re-read edges to keep the running df in sync with what's on disk.
        df = pd.read_parquet(edges_path)
        df["arxiv_id"] = df["arxiv_id"].fillna("").astype(str)
        n_filled = int((df["arxiv_id"] != "").sum())
        logger.info("  batch %d/%d: %d/%d hits, %.1fs; edges with arxiv_id now %d/%d",
                    batch_no, total_batches, len(m), len(batch), elapsed, n_filled, len(df))

        if args.max_batches and batch_no >= args.max_batches:
            logger.info("  reached --max-batches=%d; stopping", args.max_batches)
            break

        time.sleep(args.sleep)

    n_arxiv = int((df["arxiv_id"] != "").sum())
    n_unique = int(df.loc[df["arxiv_id"] != "", "bibcode"].nunique())
    logger.info("Done. %d/%d edges (%d unique papers) now have arxiv_id",
                n_arxiv, len(df), n_unique)


if __name__ == "__main__":
    main()
