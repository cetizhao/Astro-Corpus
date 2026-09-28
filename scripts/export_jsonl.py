#!/usr/bin/env python3
"""Export a corpus to JSONL, preserving one record per catalog/sample row.

Repeated input rows that resolve to the same Gaia source remain separate records
with their own ``input_row_id``, ``run_id``, and raw-row provenance. The output
shows what the corpus knows about each sample row and its resolved Gaia source.
Schema is deliberately minimal and derived from what the data actually
contains -- it is meant to evolve as downstream training requirements mature.

Schema (one JSON object per line, per sample row):

    {
      "gaia_release":       str,   # release-qualified identity, e.g. "dr3"
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
from astro_pipeline.gaia_releases import get_gaia_release_profile
from astro_pipeline.library.refs import ProductReferenceError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("export_jsonl")

PIPELINE_VERSION = "0.5.0"
DATA = Path("data/twa")
CATALOG = Path("TWAxgdr3.csv")
OUTPUT = DATA / "twa_corpus.jsonl"


def _datalink_url(gaia_id: str, gaia_release: str = "dr3") -> str:
    profile = get_gaia_release_profile(gaia_release)
    designation = quote_plus(f"{profile.archive_tag} {gaia_id}")
    return f"https://gea.esac.esa.int/data-server/datalink/links?ID={designation}"


def to_record(
    lookup: dict,
    catalog_extras: dict,
    xp_sampled: dict | None = None,
    xp_continuous: dict | None = None,
    xp_sampled_available: bool | None = None,
    xp_continuous_available: bool | None = None,
    xp_sampled_path: str | None = None,
    xp_continuous_path: str | None = None,
    input_provenance: dict | None = None,
) -> dict:
    """Flatten a corpus.lookup_source result into the simplified export schema."""
    ast = lookup.get("astrometry", {})
    mm = lookup.get("multimodal", {})
    papers = lookup.get("papers", [])
    extras = catalog_extras or {}
    input_info = input_provenance or {}

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
    gaia_release = str(
        _first(_f(extras, "gaia_release"), lookup.get("gaia_release"), "dr3")
    )
    gaia_release = get_gaia_release_profile(gaia_release).key
    sampled_available = (
        xp_sampled is not None if xp_sampled_available is None else xp_sampled_available
    )
    continuous_available = (
        xp_continuous is not None
        if xp_continuous_available is None
        else xp_continuous_available
    )
    datalink_url = _datalink_url(gaia_id, gaia_release)
    # Suggested quality cuts (informational; downstream may use stricter / looser).
    astrometry_ok = bool(ruwe is not None and ruwe < 1.4
                        and parallax_snr is not None and parallax_snr > 5)
    photometry_ok = bool(excess is not None and 1.0 < excess < 3.0
                         and g_mag is not None and g_mag < 18.0)
    input_row_id = _first(
        _f(input_info, "input_row_id"),
        _f(extras, "input_row_id"),
    )
    run_id = _first(_f(input_info, "run_id"), _f(extras, "run_id"))
    raw_input_row = input_info.get("raw_input_row")

    return {
        "gaia_release": gaia_release,
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
                    "path": (
                        xp_sampled_path
                        or f"gaia_xp_sampled/{lookup.get('gaia_id')}.fits"
                    ) if xp_sampled else None,
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
                    "path": (
                        xp_continuous_path
                        or f"gaia_xp_continuous/{lookup.get('gaia_id')}.fits"
                    ) if xp_continuous else None,
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
        "provenance": {
            "pipeline_version": PIPELINE_VERSION,
            "gaia_release": gaia_release,
            "input_row_id": None if input_row_id is None else str(input_row_id),
            "run_id": None if run_id is None else str(run_id),
            "input_status": input_info.get("status"),
            "raw_input_row": raw_input_row if isinstance(raw_input_row, dict) else None,
        },
    }


def _stable_text(value) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text or None


def _load_input_provenance(data_dir: Path) -> dict[str, list[dict]]:
    """Load row sidecar records keyed by stable input_row_id."""
    candidates = [data_dir / "input_rows.parquet", data_dir / "input_rows.csv"]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        return {}
    frame = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    result: dict[str, list[dict]] = {}
    for _, sidecar_row in frame.iterrows():
        input_row_id = _stable_text(sidecar_row.get("input_row_id"))
        if input_row_id is None:
            continue
        raw_value = sidecar_row.get("raw_json")
        raw_input_row = None
        if isinstance(raw_value, str):
            try:
                parsed = json.loads(raw_value)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                raw_input_row = parsed
        elif isinstance(raw_value, dict):
            raw_input_row = raw_value
        result.setdefault(input_row_id, []).append(
            {
                "input_row_id": input_row_id,
                "run_id": _stable_text(sidecar_row.get("run_id")),
                "status": _stable_text(sidecar_row.get("status")),
                "raw_input_row": raw_input_row,
            }
        )
    return result


def _input_provenance_for_row(
    row: pd.Series,
    sidecar_by_input_row: dict[str, list[dict]],
) -> dict:
    input_row_id = _stable_text(row.get("input_row_id"))
    run_id = _stable_text(row.get("run_id"))
    if input_row_id is None:
        return {"run_id": run_id} if run_id is not None else {}
    candidates = sidecar_by_input_row.get(input_row_id, [])
    if run_id is not None:
        matching_run = [candidate for candidate in candidates if candidate.get("run_id") == run_id]
        if len(matching_run) == 1:
            return matching_run[0]
    if len(candidates) == 1:
        selected = dict(candidates[0])
        selected["run_id"] = selected.get("run_id") or run_id
        return selected
    return {"input_row_id": input_row_id, "run_id": run_id}


def _read_source_product(
    data_dir: Path,
    gaia_id: str,
    release: str,
    retrieval_type: str,
    reader,
) -> tuple[dict | None, str | None]:
    from astro_pipeline.library.files import BlobIntegrityError
    from astro_pipeline.library.refs import resolve_product

    try:
        reference = resolve_product(
            data_dir,
            gaia_id,
            retrieval_type,
            release=release,
        )
        return reader(reference["path"]), str(reference["path"])
    except (BlobIntegrityError, FileNotFoundError, ProductReferenceError, ValueError):
        return None, None


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

    try:
        corpus = Corpus(
            data_dir=args.data_dir,
            catalog_path=args.catalog,
            offline=args.offline,
        )
    except (FileNotFoundError, ProductReferenceError) as exc:
        logger.error("Corpus is not readable: %s", exc)
        sys.exit(1)

    catalog = corpus.catalog
    data_dir = corpus.data_dir
    input_sidecar = _load_input_provenance(data_dir)
    logger.info("Loaded %d catalog rows from %s", len(catalog), corpus.catalog_path)

    # Pre-load XP Sampled and Continuous metadata through pinned product refs.
    from astro_pipeline.spectra import read_xp_continuous_params, read_xp_sampled

    xp_sampled_data: dict[tuple[str, str], dict] = {}
    xp_cont_data: dict[tuple[str, str], dict] = {}
    xp_sampled_paths: dict[tuple[str, str], str] = {}
    xp_cont_paths: dict[tuple[str, str], str] = {}
    xp_availability: dict[tuple[str, str], dict[str, bool]] = {}
    product_keys_checked: set[tuple[str, str]] = set()
    availability_paths = [
        data_dir / "catalogs" / "gaia_xp_availability.parquet",
        data_dir / "catalogs" / "twa_gaia_xp_availability.parquet",
    ]
    availability_path = next((path for path in availability_paths if path.exists()), None)
    if availability_path is not None:
        availability = pd.read_parquet(availability_path)
        for _, availability_row in availability.iterrows():
            gid = _stable_text(availability_row.get("source_id"))
            if gid is None:
                continue
            release = get_gaia_release_profile(
                _stable_text(availability_row.get("gaia_release")) or "dr3"
            ).key
            xp_availability[(release, gid)] = {
                "sampled": str(availability_row.get("has_xp_sampled", False)).lower()
                in {"true", "1"},
                "continuous": str(availability_row.get("has_xp_continuous", False)).lower()
                in {"true", "1"},
            }
    for _, catalog_row in catalog.iterrows():
        gid = str(catalog_row["Gaia"])
        release = get_gaia_release_profile(
            _stable_text(catalog_row.get("gaia_release")) or "dr3"
        ).key
        key = (release, gid)
        if key in product_keys_checked:
            continue
        product_keys_checked.add(key)
        sampled, sampled_path = _read_source_product(
            data_dir,
            gid,
            release,
            "XP_SAMPLED",
            read_xp_sampled,
        )
        continuous, continuous_path = _read_source_product(
            data_dir,
            gid,
            release,
            "XP_CONTINUOUS",
            read_xp_continuous_params,
        )
        if sampled is not None:
            xp_sampled_data[key] = sampled
            assert sampled_path is not None
            xp_sampled_paths[key] = sampled_path
        if continuous is not None:
            xp_cont_data[key] = continuous
            assert continuous_path is not None
            xp_cont_paths[key] = continuous_path

    args.output.parent.mkdir(parents=True, exist_ok=True)
    n_written = 0
    n_with_arxiv = 0
    n_with_xp_sampled = 0
    n_with_xp_continuous = 0
    with args.output.open("w", encoding="utf-8") as f:
        for _, row in catalog.iterrows():
            gid = str(row["Gaia"])
            release = get_gaia_release_profile(
                _stable_text(row.get("gaia_release")) or "dr3"
            ).key
            source_key = (release, gid)
            if args.limit and n_written >= args.limit:
                break
            lookup = corpus.lookup_source(gid)
            if not lookup.get("resolved"):
                logger.warning("  skipping unresolved source %s", gid)
                continue
            input_provenance = _input_provenance_for_row(row, input_sidecar)
            raw_input_row = input_provenance.get("raw_input_row")
            row_extras = {
                **(raw_input_row if isinstance(raw_input_row, dict) else {}),
                **row.to_dict(),
            }
            rec = to_record(
                lookup,
                row_extras,
                xp_sampled=xp_sampled_data.get(source_key),
                xp_continuous=xp_cont_data.get(source_key),
                xp_sampled_available=xp_availability.get(source_key, {}).get("sampled"),
                xp_continuous_available=xp_availability.get(source_key, {}).get("continuous"),
                xp_sampled_path=xp_sampled_paths.get(source_key),
                xp_continuous_path=xp_cont_paths.get(source_key),
                input_provenance=input_provenance,
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
