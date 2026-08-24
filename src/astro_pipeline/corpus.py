"""Bidirectional source ↔ paper corpus query API and CLI.

Two primary capabilities (the project's core deliverable):

  * ``lookup_source(gaia_id)``  -> astrometry + multimodal spectra availability
    (Gaia XP / LAMOST / SDSS) + the SIMBAD-curated list of papers studying it.
  * ``lookup_paper(query)``     -> paper metadata + the list of catalog sources
    it studies, each with its own multimodal availability.

Multimodal data is fetched live when possible, and falls back to the cached
parquet files under ``data/twa`` when a survey service is unavailable or
``offline=True``. A single survey failing never breaks a result; that modality
is returned with ``status="unavailable"``.

The paper linking is **authoritative**: it comes from SIMBAD's curated
bibliography per object (see sources/simbad_refs.py), not substring matching.

CLI:
    python -m astro_pipeline.corpus source <gaia_id> [--offline] [--json]
    python -m astro_pipeline.corpus paper  <arxiv_id|bibcode|title> [--offline] [--json]
    python -m astro_pipeline.corpus summary
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

from astro_pipeline.io.exporter import find_gaia_id_col
from astro_pipeline.gaia_releases import normalize_gaia_release
from astro_pipeline.models.paper import PaperEdgeTable

logger = logging.getLogger(__name__)

# Canonical column names in the TWA member catalog (TWAxgdr3.csv).
GAIA_ID_COL = "Gaia"
RA_COL = "RAICRS"
DEC_COL = "DEICRS"

DEFAULT_DATA_DIR = Path("data/twa")
DEFAULT_CATALOG = Path("TWAxgdr3.csv")


@dataclass
class Modality:
    survey: str
    available: bool
    status: str  # "live" | "cached" | "unavailable" | "not_in_catalog"
    rows: int = 0
    data: dict = field(default_factory=dict)


@dataclass
class PaperInfo:
    arxiv_id: str
    bibcode: str
    title: str
    year: Optional[int]
    journal: str
    url: str


@dataclass
class SourceResult:
    gaia_id: str
    name: str = ""
    astrometry: dict = field(default_factory=dict)
    multimodal: dict = field(default_factory=dict)  # survey -> Modality
    papers: list = field(default_factory=list)
    resolved: bool = False


def _safe_record(row: pd.Series, cols: list[str]) -> dict:
    out = {}
    for c in cols:
        if c in row.index:
            v = row[c]
            try:
                if pd.isna(v):
                    out[c] = None
                else:
                    out[c] = v.item() if hasattr(v, "item") else v
            except (TypeError, ValueError):
                out[c] = None if pd.isna(v) else v
    return out


class Corpus:
    """Bidirectional query interface over a cached catalog + survey tables.

    Parameters
    ----------
    data_dir : where the cached parquet files live (catalogs/, paper_edges.parquet, etc.)
    catalog_path : the member catalog CSV (with Gaia/RAICRS/DEICRS columns).
    offline : if True, never attempt live network queries — only use cache.
    """

    def __init__(
        self,
        data_dir: Path | str = DEFAULT_DATA_DIR,
        catalog_path: Path | str = DEFAULT_CATALOG,
        offline: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.offline = offline
        self.catalog_path = Path(catalog_path)

        self.catalog = self._load_catalog()
        self.gaia_release = self._infer_gaia_release()
        self.paper_edges = self._load_paper_edges()
        self._cached_surveys = self._load_cached_surveys()

    # ------------------------------------------------------------------ loaders
    def _load_catalog(self) -> pd.DataFrame:
        path = self.catalog_path
        if not path.exists():
            # Allow data-dir-relative fallback.
            alt = self.data_dir / "twa_members.parquet"
            if alt.exists():
                return pd.read_parquet(alt)
            raise FileNotFoundError(f"Member catalog not found: {path}")
        return pd.read_csv(path)

    def _load_paper_edges(self) -> PaperEdgeTable:
        path = self.data_dir / "paper_edges.parquet"
        if path.exists():
            return PaperEdgeTable(path)
        logger.warning("No paper_edges.parquet in %s; paper links will be empty", self.data_dir)
        return PaperEdgeTable()

    def _infer_gaia_release(self) -> str:
        if "gaia_release" in self.catalog.columns:
            releases = {
                normalize_gaia_release(value)
                for value in self.catalog["gaia_release"].dropna().astype(str)
                if value.strip()
            }
            if len(releases) > 1:
                raise ValueError(
                    f"member catalog mixes Gaia releases: {sorted(releases)}"
                )
            if releases:
                return next(iter(releases))
        manifest_path = self.data_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                return normalize_gaia_release(
                    manifest.get("corpus", {}).get("gaia_release", "dr3")
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Could not infer Gaia release from manifest: %s", exc)
        return "dr3"

    def _load_cached_surveys(self) -> dict[str, pd.DataFrame]:
        """Load all cached survey parquets we may fall back to."""
        cat_dir = self.data_dir / "catalogs"
        out: dict[str, pd.DataFrame] = {}
        mapping = {
            "gaia_xp": "twa_gaia_xp.parquet",
            "lamost": "twa_lamost.parquet",
            "sdss": "twa_sdss.parquet",
        }
        for key, name in mapping.items():
            p = cat_dir / name
            if p.exists():
                try:
                    df = pd.read_parquet(p)
                    if not _looks_like_html(df):
                        out[key] = df
                except Exception as e:
                    logger.debug("Could not load cached %s: %s", name, e)
        return out

    # ------------------------------------------------------------------ helpers
    def _row_for_source(self, gaia_id: str) -> Optional[pd.Series]:
        if GAIA_ID_COL not in self.catalog.columns:
            return None
        mask = self.catalog[GAIA_ID_COL].astype(str) == str(gaia_id)
        if not mask.any():
            return None
        return self.catalog[mask].iloc[0]

    def _survey_modality(
        self, survey: str, gaia_id: str, id_cols: tuple[str, ...], value_cols: list[str]
    ) -> Modality:
        """Return a Modality for ``gaia_id`` from a survey, live-first then cached."""
        # Try live first.
        if not self.offline:
            try:
                data = self._fetch_live_survey(survey, gaia_id, value_cols)
                if data is not None:
                    return Modality(
                        survey=survey, available=True, status="live",
                        rows=int(data.get("_rows", 1)), data=data,
                    )
            except Exception as e:
                logger.debug("Live %s failed for %s: %s", survey, gaia_id, e)

        # Fall back to cache.
        cached = self._cached_surveys.get(survey)
        if cached is None:
            return Modality(survey=survey, available=False, status="unavailable")
        id_col = None
        for c in id_cols:
            if c in cached.columns:
                id_col = c
                break
        if id_col is None:
            id_col = find_gaia_id_col(cached)
        if id_col is None:
            return Modality(survey=survey, available=False, status="unavailable")
        match = cached[cached[id_col].astype(str) == str(gaia_id)]
        if match.empty:
            return Modality(survey=survey, available=False, status="not_in_catalog")
        data = _safe_record(match.iloc[0], value_cols)
        data["_rows"] = int(len(match))
        return Modality(survey=survey, available=True, status="cached", rows=len(match), data=data)

    def _fetch_live_survey(self, survey: str, gaia_id: str, value_cols: list[str]) -> Optional[dict]:
        """Live single-source fetch. Returns a dict of selected fields or None.

        Implemented per-survey; keep these light (single-source queries) so the
        demo stays responsive. All failures are caught by the caller.
        """
        from astro_pipeline.config import PipelineConfig
        cfg = PipelineConfig()

        if survey == "gaia_xp":
            from astro_pipeline.sources.gaia import GaiaSource
            from astro_pipeline.models.query import SourceIdQuery
            src = GaiaSource(cfg.gaia, cfg.network)
            res = src.query_xp_sources(SourceIdQuery(source_ids=[str(gaia_id)]))
            df = res.data
            if df.empty:
                return None
            row = df.iloc[0]
            out = _safe_record(row, ["source_id", "ra", "dec", "phot_g_mean_mag", "has_xp_sampled"])
            out["_rows"] = int(len(df))
            return out

        if survey == "lamost":
            # LAMOST has no direct source-id query; query by the source position
            # with a small radius and pick the closest. This is honest about what
            # the survey supports.
            row = self._row_for_source(gaia_id)
            if row is None:
                return None
            from astropy import units as u
            from astropy.coordinates import SkyCoord
            from astro_pipeline.sources.lamost import LamostSource
            from astro_pipeline.models.query import ConeSearchQuery
            src = LamostSource(cfg.lamost, cfg.network)
            res = src.query_catalog(
                ConeSearchQuery(ra=float(row[RA_COL]), dec=float(row[DEC_COL]), radius_deg=0.02)
            )
            df = res.data
            if df.empty:
                return None
            ra_col = next((c for c in df.columns if c.lower() in ("raj2000", "ra")), "raj2000")
            dec_col = next((c for c in df.columns if c.lower() in ("dej2000", "dec")), "dej2000")
            coord = SkyCoord(ra=float(row[RA_COL]) * u.deg, dec=float(row[DEC_COL]) * u.deg)
            rows = df.copy()
            rows["_sep"] = SkyCoord(ra=rows[ra_col].astype(float).values * u.deg,
                                    dec=rows[dec_col].astype(float).values * u.deg).separation(coord).arcsec
            best = rows.sort_values("_sep").iloc[0]
            if float(best["_sep"]) > 3.0:
                return None
            out = _safe_record(best, ["obsid", "snrg", "snrr", "snri", ra_col, dec_col, "gaiadr3"])
            out["_sep_arcsec"] = float(best["_sep"])
            out["_rows"] = int(len(df))
            return out

        if survey == "sdss":
            row = self._row_for_source(gaia_id)
            if row is None:
                return None
            from astro_pipeline.sources.sdss import SdssSource
            from astro_pipeline.models.query import ConeSearchQuery
            src = SdssSource(cfg.sdss, cfg.network)
            res = src.query_catalog(
                ConeSearchQuery(ra=float(row[RA_COL]), dec=float(row[DEC_COL]), radius_deg=0.01, limit=5)
            )
            df = res.data
            if df.empty:
                return None
            best = df.iloc[0]
            out = _safe_record(best, ["specobjid", "plate", "mjd", "fiberid", "redshift", "class"])
            out["_rows"] = int(len(df))
            return out

        return None

    # ------------------------------------------------------------------ public API
    def lookup_source(self, gaia_id: str, surveys: tuple[str, ...] = ("gaia_xp", "lamost", "sdss")) -> dict:
        """Source -> multimodal data + papers."""
        gid = str(gaia_id)
        result = {
            "gaia_release": self.gaia_release,
            "gaia_id": gid,
            "resolved": False,
            "astrometry": {},
            "multimodal": {},
            "papers": [],
        }
        row = self._row_for_source(gid)
        if row is None:
            result["error"] = f"Gaia source {gid} not in catalog"
            return result
        result["resolved"] = True
        result["astrometry"] = _safe_record(
            row, [GAIA_ID_COL, RA_COL, DEC_COL, "Plx_gdr3", "e_Plx_gdr3", "RUWE_gdr3", "Gmag_gdr3"]
        )
        result["name"] = ""

        survey_specs = {
            "gaia_xp": (("source_id", "gaia_source_id", "Gaia"),
                        ["source_id", "ra", "dec", "phot_g_mean_mag", "has_xp_sampled"]),
            "lamost": (("gaiadr3", "gaia_source_id", "Gaia"),
                       ["obsid", "snrg", "snrr", "snri", "raj2000", "dej2000"]),
            "sdss": (("specobjid",), ["specobjid", "plate", "mjd", "fiberid", "redshift", "class"]),
        }
        for s in surveys:
            id_cols, val_cols = survey_specs.get(s, ((), []))
            if not id_cols:
                continue
            mod = self._survey_modality(s, gid, id_cols, val_cols)
            result["multimodal"][s] = _modality_to_dict(mod)

        result["papers"] = self._papers_for_source(gid)
        return result

    def lookup_paper(self, query: str, surveys: tuple[str, ...] = ("gaia_xp", "lamost", "sdss")) -> dict:
        """Paper (arxiv_id|bibcode|title) -> sources with multimodal data."""
        q = str(query).strip()
        edge_df = self.paper_edges.df
        result = {"query": q, "resolved": False, "paper": {}, "sources": []}
        if edge_df.empty:
            result["error"] = "No paper edges loaded (build paper_edges.parquet first)"
            return result

        # Match by arxiv_id first, then bibcode, then title substring.
        arxiv_series = edge_df["arxiv_id"].astype(str)
        bib_series = edge_df.get("bibcode", pd.Series([""] * len(edge_df))).astype(str)
        title_series = edge_df.get("title", pd.Series([""] * len(edge_df))).astype(str)

        mask = arxiv_series == q
        if not mask.any():
            mask = bib_series == q
        if not mask.any():
            mask = title_series.str.lower().str.contains(q.lower(), na=False)

        matched = edge_df[mask]
        if matched.empty:
            result["error"] = f"No paper matched '{q}'"
            return result

        # Paper metadata: take the first matched row, preferring one with a title.
        with_title = matched[matched["title"].astype(str) != ""] if "title" in matched.columns else matched
        meta_row = with_title.iloc[0] if not with_title.empty else matched.iloc[0]
        result["resolved"] = True
        result["paper"] = {
            "arxiv_id": str(meta_row.get("arxiv_id", "") or ""),
            "bibcode": str(meta_row.get("bibcode", "") or ""),
            "title": str(meta_row.get("title", "") or ""),
            "year": (int(meta_row["year"]) if pd.notna(meta_row.get("year")) else None),
            "journal": str(meta_row.get("journal", "") or ""),
            "url": _paper_url(meta_row),
        }

        for gid in sorted(matched["source_id"].astype(str).unique()):
            src = self.lookup_source(gid, surveys=surveys)
            result["sources"].append({
                "gaia_release": src.get("gaia_release", self.gaia_release),
                "gaia_id": gid,
                "name": src.get("name", ""),
                "astrometry": src.get("astrometry", {}),
                "multimodal": src.get("multimodal", {}),
            })
        return result

    def summary(self) -> dict:
        edge_df = self.paper_edges.df
        has_bib = "bibcode" in edge_df.columns if not edge_df.empty else False
        return {
            "gaia_release": self.gaia_release,
            "catalog_sources": int(len(self.catalog)),
            "paper_edges": int(len(edge_df)),
            "unique_papers": int(edge_df["bibcode"].nunique()) if has_bib else 0,
            "papers_with_arxiv": int((edge_df["arxiv_id"].astype(str) != "").sum()) if not edge_df.empty else 0,
            "cached_surveys": sorted(self._cached_surveys.keys()),
            "gaia_xp_in_cache": int(len(self._cached_surveys.get("gaia_xp", pd.DataFrame()))),
            "lamost_in_cache": int(len(self._cached_surveys.get("lamost", pd.DataFrame()))),
            "sdss_in_cache": int(len(self._cached_surveys.get("sdss", pd.DataFrame()))),
        }

    def _papers_for_source(self, gaia_id: str) -> list[dict]:
        edge_df = self.paper_edges.df
        if edge_df.empty:
            return []
        rows = edge_df[edge_df["source_id"].astype(str) == str(gaia_id)]
        # Deduplicate by bibcode when present, prefer rows that have a title.
        if "title" in rows.columns:
            rows = rows.sort_values("title", key=lambda s: s.astype(str) == "", ascending=False)
        if "bibcode" in rows.columns:
            rows = rows.drop_duplicates(subset=["bibcode"])
        papers = []
        for _, r in rows.iterrows():
            papers.append({
                "arxiv_id": str(r.get("arxiv_id", "") or ""),
                "bibcode": str(r.get("bibcode", "") or ""),
                "title": str(r.get("title", "") or ""),
                "year": (int(r["year"]) if "year" in r.index and pd.notna(r.get("year")) else None),
                "journal": str(r.get("journal", "") or ""),
                "doi": str(r.get("doi", "") or ""),
                "url": _paper_url(r),
            })
        return papers


# ---------------------------------------------------------------- module helpers
def re_escape(s: str) -> str:
    import re
    return re.escape(s)


def _paper_url(row: pd.Series) -> str:
    arxiv = str(row.get("arxiv_id", "") or "")
    if arxiv:
        return f"https://arxiv.org/abs/{arxiv}"
    doi = str(row.get("doi", "") or "")
    if doi:
        return f"https://doi.org/{doi}"
    bib = str(row.get("bibcode", "") or "")
    if bib:
        return f"https://ui.adsabs.harvard.edu/abs/{bib}"
    return ""


def _modality_to_dict(m: Modality) -> dict:
    return {"survey": m.survey, "available": m.available, "status": m.status, "rows": m.rows, "data": m.data}


def _looks_like_html(df: pd.DataFrame) -> bool:
    """Guard against cached HTML-error-page parquets."""
    if len(df.columns) == 1:
        col = str(df.columns[0]).lstrip().lower()
        if col.startswith("<html") or col.startswith("<!doctype"):
            return True
    return False


# ---------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="astro_pipeline.corpus",
        description="Bidirectional source ↔ paper corpus query.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("source", help="Look up a Gaia source's multimodal data + papers.")
    ps.add_argument("gaia_id")
    ps.add_argument("--offline", action="store_true")
    ps.add_argument("--json", action="store_true")
    ps.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ps.add_argument("--catalog", default=str(DEFAULT_CATALOG))

    pp = sub.add_parser("paper", help="Look up a paper's sources + their multimodal data.")
    pp.add_argument("query", help="arxiv_id, bibcode, or title substring")
    pp.add_argument("--offline", action="store_true")
    pp.add_argument("--json", action="store_true")
    pp.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    pp.add_argument("--catalog", default=str(DEFAULT_CATALOG))

    psum = sub.add_parser("summary", help="Print corpus coverage summary.")
    psum.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    psum.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    args = _build_parser().parse_args(argv)

    if args.cmd == "summary":
        c = Corpus(data_dir=args.data_dir, catalog_path=args.catalog, offline=True)
        print(json.dumps(c.summary(), indent=2, default=str))
        return 0

    corpus = Corpus(data_dir=args.data_dir, catalog_path=args.catalog, offline=args.offline)

    if args.cmd == "source":
        res = corpus.lookup_source(args.gaia_id)
    else:
        res = corpus.lookup_paper(args.query)

    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        _print_human(res, args.cmd)
    return 0 if res.get("resolved") else 1


def _print_human(res: dict, cmd: str) -> None:
    if not res.get("resolved"):
        print(f"[!] {res.get('error', 'not resolved')}")
        return
    if cmd == "source":
        print(f"Gaia DR3 {res['gaia_id']}")
        ast = res.get("astrometry", {})
        if ast:
            print(f"  RA={ast.get(RA_COL)}, Dec={ast.get(DEC_COL)}, Plx={ast.get('Plx_gdr3')}, G={ast.get('Gmag_gdr3')}")
        for survey, mod in res.get("multimodal", {}).items():
            tag = "OK" if mod["available"] else "--"
            print(f"  [{tag}] {survey:8s} ({mod['status']}, rows={mod['rows']})")
        papers = res.get("papers", [])
        print(f"  Papers: {len(papers)}")
        for p in papers:
            label = p["arxiv_id"] or p["bibcode"]
            print(f"    - {label}  ({p.get('year')}) {p['title'][:70]}")
            if p["url"]:
                print(f"        {p['url']}")
    else:
        paper = res.get("paper", {})
        print(f"Paper: {paper.get('title','')}")
        print(f"  arxiv={paper.get('arxiv_id','')}  bibcode={paper.get('bibcode','')}  ({paper.get('year')}) {paper.get('journal','')}")
        if paper.get("url"):
            print(f"  {paper['url']}")
        for s in res.get("sources", []):
            ast = s.get("astrometry", {})
            n_ok = sum(1 for m in s.get("multimodal", {}).values() if m.get("available"))
            print(f"  - Gaia {s['gaia_id']}  RA={ast.get(RA_COL)} Dec={ast.get(DEC_COL)}  multimodal_ok={n_ok}")


if __name__ == "__main__":
    sys.exit(main())
