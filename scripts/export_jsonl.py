#!/usr/bin/env python3
"""Export the TWA corpus to a simplified JSONL schema (one record per source).

This is the **demonstrable data storage** artifact: a single self-describing
JSONL file showing what the corpus currently knows about each Gaia source.
Schema is deliberately minimal and derived from what the data actually
contains -- it is meant to evolve as downstream training requirements mature.

Schema (one JSON object per line, per Gaia source):

    {
      "gaia_id":            str,   # Gaia DR3 source_id
      "ra_deg":             float, # ICRS RA from catalog
      "dec_deg":            float, # ICRS Dec
      "parallax_mas":       float, # Gaia DR3 parallax (mas)
      "parallax_error_mas": float,
      "ruwe":               float,
      "g_mag":              float, # Gaia G-band magnitude
      "pm_ra_masyr":        float, # proper motion (mas/yr)
      "pm_dec_masyr":       float,
      "quality": {
        "ruwe":                       float,
        "parallax_snr":               float,  # parallax / parallax_error
        "phot_bp_rp_excess_factor":   float,
        "astrometric_excess_noise":   float,
        "visibility_periods_used":    int,
        "astrometric_n_good_obs_al":  int,
        "duplicated_source":          bool,
        "astrometry_ok":              bool,   # ruwe<1.4 && parallax_snr>5 (suggested)
        "photometry_ok":              bool    # 1.0 < excess < 1.4 (rough, G<18)
      },
      "multimodal": {
        "gaia_xp": {
          "sampled": {
            "available": bool, "cached": bool,
            "retrieval_type": "XP_SAMPLED", "datalink_url": str|null,
            "path": str|null, "n_points": int|null
          },
          "continuous": {
            "available": bool, "cached": bool,
            "retrieval_type": "XP_CONTINUOUS", "datalink_url": str|null,
            "bp_coefficients": list, "bp_coefficient_errors": list,
            "rp_coefficients": list, "rp_coefficient_errors": list,
            "quality": dict, "path": str|null
          }
        },
        "lamost":  bool,           # LAMOST spectrum available
        "sdss":    bool            # SDSS spectrum available
      },
      "papers": [
        {"arxiv_id": str, "bibcode": str, "title": str, "year": int|null,
         "url": str},
        ...
      ],
      "n_papers":  int,
      "provenance": {"pipeline_version": str}
    }

The ``quality`` block is informational only -- the pipeline does no filtering.
Downstream consumers decide their own quality cuts using these flags.

Usage:
    python scripts/export_jsonl.py
    python scripts/export_jsonl.py --output path/to/out.jsonl --offline
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astro_pipeline.corpus import Corpus

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("export_jsonl")

PIPELINE_VERSION = "0.4.0"
DATA = Path("data/twa")
CATALOG = Path("TWAxgdr3.csv")
OUTPUT = DATA / "twa_corpus.jsonl"


def _datalink_url(gaia_id: str) -> str:
    designation = quote_plus(f"Gaia DR3 {gaia_id}")
    return f"https://gea.esac.esa.int/data-server/datalink/links?ID={designation}"


def to_record(
    lookup: dict,
    catalog_extras: dict,
    xp_sampled: dict | None = None,
    xp_continuous: dict | None = None,
    xp_sampled_available: bool | None = None,
    xp_continuous_available: bool | None = None,
) -> dict:
    """Flatten a corpus.lookup_source result into the simplified export schema."""
    ast = lookup.get("astrometry", {})
    mm = lookup.get("multimodal", {})
    papers = lookup.get("papers", [])
    extras = catalog_extras or {}

    def _f(d, key, default=None):
        v = d.get(key, default)
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        return v

    def _first(*values):
        for value in values:
            if value is not None:
                return value
        return None

    # Quality-control fields (no filtering -- downstream decides).
    plx = _first(_f(ast, "Plx_gdr3"), _f(extras, "Plx_gdr3"), _f(extras, "plx"))
    plx_err = _first(
        _f(ast, "e_Plx_gdr3"), _f(extras, "e_Plx_gdr3"), _f(extras, "e_plx"),
    )
    ruwe = _first(_f(ast, "RUWE_gdr3"), _f(extras, "RUWE_gdr3"), _f(extras, "RUWE"))
    parallax_snr = (plx / plx_err) if (plx and plx_err) else None
    excess = _f(extras, "phot_bp_rp_excess_factor")
    aen = _f(extras, "astrometric_excess_noise")
    vis_periods = _f(extras, "visibility_periods_used")
    n_good_al = _f(extras, "astrometric_n_good_obs_al")
    dup = _f(extras, "duplicated_source")
    g_mag = _first(_f(ast, "Gmag_gdr3"), _f(extras, "Gmag_gdr3"), _f(extras, "Gmag"))
    gaia_id = str(lookup.get("gaia_id"))
    sampled_available = (
        xp_sampled is not None if xp_sampled_available is None else xp_sampled_available
    )
    continuous_available = (
        xp_continuous is not None
        if xp_continuous_available is None
        else xp_continuous_available
    )
    datalink_url = _datalink_url(gaia_id)
    # Suggested quality cuts (informational; downstream may use stricter / looser).
    astrometry_ok = bool(ruwe is not None and ruwe < 1.4
                        and parallax_snr is not None and parallax_snr > 5)
    photometry_ok = bool(excess is not None and 1.0 < excess < 3.0
                         and g_mag is not None and g_mag < 18.0)

    return {
        "gaia_id": gaia_id,
        "ra_deg": _f(ast, "RAICRS"),
        "dec_deg": _f(ast, "DEICRS"),
        "parallax_mas": plx,
        "parallax_error_mas": plx_err,
        "ruwe": ruwe,
        "g_mag": g_mag,
        "pm_ra_masyr": _f(extras, "pmRA_gdr3") if "pmRA_gdr3" in extras else _f(extras, "pmRA"),
        "pm_dec_masyr": _f(extras, "pmDE_gdr3") if "pmDE_gdr3" in extras else _f(extras, "pmDE"),
        "quality": {
            "ruwe": ruwe,
            "parallax_snr": parallax_snr,
            "phot_bp_rp_excess_factor": excess,
            "astrometric_excess_noise": aen,
            "visibility_periods_used": vis_periods,
            "astrometric_n_good_obs_al": n_good_al,
            "duplicated_source": bool(dup) if dup is not None else None,
            "astrometry_ok": astrometry_ok,
            "photometry_ok": photometry_ok,
        },
        "multimodal": {
            "gaia_xp": {
                "sampled": {
                    "available": sampled_available,
                    "cached": xp_sampled is not None,
                    "retrieval_type": "XP_SAMPLED",
                    "datalink_url": datalink_url if sampled_available else None,
                    "path": f"gaia_xp_sampled/{lookup.get('gaia_id')}.fits" if xp_sampled else None,
                    "n_points": len(xp_sampled.get("wavelength", [])) if xp_sampled else None,
                },
                "continuous": {
                    "available": continuous_available,
                    "cached": xp_continuous is not None,
                    "retrieval_type": "XP_CONTINUOUS",
                    "datalink_url": datalink_url if continuous_available else None,
                    "bp_coefficients": xp_continuous.get("bp_coefficients", []) if xp_continuous else [],
                    "bp_coefficient_errors": xp_continuous.get("bp_coefficient_errors", []) if xp_continuous else [],
                    "rp_coefficients": xp_continuous.get("rp_coefficients", []) if xp_continuous else [],
                    "rp_coefficient_errors": xp_continuous.get("rp_coefficient_errors", []) if xp_continuous else [],
                    "quality": xp_continuous.get("quality", {}) if xp_continuous else {},
                    "path": f"gaia_xp_continuous/{lookup.get('gaia_id')}.fits" if xp_continuous else None,
                },
            },
            "lamost": bool(mm.get("lamost", {}).get("available", False)),
            "sdss": bool(mm.get("sdss", {}).get("available", False)),
        },
        "papers": [
            {
                "arxiv_id": p.get("arxiv_id") or "",
                "bibcode": p.get("bibcode") or "",
                "title": p.get("title") or "",
                "year": p.get("year"),
                "url": p.get("url") or "",
            }
            for p in papers
        ],
        "n_papers": len(papers),
        "provenance": {"pipeline_version": PIPELINE_VERSION},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=OUTPUT)
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--data-dir", type=Path, default=DATA)
    ap.add_argument("--offline", action="store_true", default=True,
                    help="Use cached parquets only (default; pass --no-offline to allow live queries).")
    ap.add_argument("--no-offline", dest="offline", action="store_false")
    ap.add_argument("--limit", type=int, default=0,
                    help="Export only first N sources (0 = all, for debugging).")
    args = ap.parse_args()

    if not args.catalog.exists():
        logger.error("Catalog not found: %s", args.catalog)
        sys.exit(1)

    catalog = pd.read_csv(args.catalog, dtype={"Gaia": str})
    catalog_extras = {str(r["Gaia"]): r.to_dict() for _, r in catalog.iterrows()}
    logger.info("Loaded %d catalog sources from %s", len(catalog), args.catalog)

    # Pre-load XP Sampled and Continuous metadata (coefficients/quality only).
    from astro_pipeline.spectra import read_xp_continuous_params, read_xp_sampled

    xp_sampled_root = args.data_dir / "gaia_xp_sampled"
    xp_cont_root = args.data_dir / "gaia_xp_continuous"
    xp_sampled_data: dict[str, dict] = {}
    xp_cont_data: dict[str, dict] = {}
    xp_availability: dict[str, dict[str, bool]] = {}
    availability_paths = [
        args.data_dir / "catalogs" / "gaia_xp_availability.parquet",
        args.data_dir / "catalogs" / "twa_gaia_xp_availability.parquet",
    ]
    availability_path = next((path for path in availability_paths if path.exists()), None)
    if availability_path is not None:
        availability = pd.read_parquet(availability_path)
        for _, availability_row in availability.iterrows():
            gid = str(availability_row["source_id"])
            xp_availability[gid] = {
                "sampled": str(availability_row.get("has_xp_sampled", False)).lower()
                in {"true", "1"},
                "continuous": str(availability_row.get("has_xp_continuous", False)).lower()
                in {"true", "1"},
            }
    for gid in catalog["Gaia"].astype(str):
        sp = xp_sampled_root / f"{gid}.fits"
        cp = xp_cont_root / f"{gid}.fits"
        if sp.exists():
            try:
                xp_sampled_data[gid] = read_xp_sampled(sp)
            except ValueError:
                pass
        if cp.exists():
            try:
                xp_cont_data[gid] = read_xp_continuous_params(cp)
            except ValueError:
                pass

    corpus = Corpus(data_dir=args.data_dir, catalog_path=args.catalog, offline=args.offline)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    n_written = 0
    n_with_arxiv = 0
    n_with_xp_sampled = 0
    n_with_xp_continuous = 0
    with args.output.open("w", encoding="utf-8") as f:
        for _, row in catalog.iterrows():
            gid = str(row["Gaia"])
            if args.limit and n_written >= args.limit:
                break
            lookup = corpus.lookup_source(gid)
            if not lookup.get("resolved"):
                logger.warning("  skipping unresolved source %s", gid)
                continue
            rec = to_record(
                lookup, catalog_extras.get(gid, {}),
                xp_sampled=xp_sampled_data.get(gid),
                xp_continuous=xp_cont_data.get(gid),
                xp_sampled_available=xp_availability.get(gid, {}).get("sampled"),
                xp_continuous_available=xp_availability.get(gid, {}).get("continuous"),
            )
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_written += 1
            if rec["papers"] and any(p["arxiv_id"] for p in rec["papers"]):
                n_with_arxiv += 1
            xp = rec["multimodal"]["gaia_xp"]
            if xp.get("sampled", {}).get("available"):
                n_with_xp_sampled += 1
            if xp.get("continuous", {}).get("available"):
                n_with_xp_continuous += 1

    size_kb = args.output.stat().st_size / 1024
    logger.info("Wrote %d records to %s (%.1f KB)", n_written, args.output, size_kb)
    logger.info("  sources with >=1 arxiv paper: %d/%d", n_with_arxiv, n_written)
    logger.info("  sources with Gaia XP Sampled: %d/%d", n_with_xp_sampled, n_written)
    logger.info("  sources with Gaia XP Continuous: %d/%d", n_with_xp_continuous, n_written)


if __name__ == "__main__":
    main()
