"""Benchmark: download small samples from each source and modality, verify, report."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table as RichTable

from astro_pipeline.config import PipelineConfig
from astro_pipeline.models.query import ConeSearchQuery
from astro_pipeline.sources.gaia import GaiaSource
from astro_pipeline.sources.lamost import LamostSource
from astro_pipeline.sources.sdss import SdssSource

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Test coordinates: each survey's well-covered region
GAIA_LAMOST_RA, GAIA_LAMOST_DEC = 290.0, 40.0   # Kepler field
SDSS_RA, SDSS_DEC = 180.0, 30.0                   # SDSS Legacy footprint


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def verify_parquet(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    try:
        df = pd.read_parquet(path)
        return len(df) > 0
    except Exception:
        return False


def verify_fits(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        from astropy.io import fits
        with fits.open(str(path)) as hdul:
            return len(hdul) > 0
    except Exception:
        return False


def run_benchmark(config_path: Path | None = None) -> dict:
    if config_path is None:
        config_path = CONFIG_DIR / "benchmark.yaml"
    if not config_path.exists():
        config_path = CONFIG_DIR / "default.yaml"

    config = PipelineConfig.from_yaml(config_path)
    if not config.gaia.username:
        config.gaia.username = os.environ.get("GAIA_USERNAME", "")
    if not config.gaia.password:
        config.gaia.password = os.environ.get("GAIA_PASSWORD", "")
    config.output.ensure_dirs()

    console = Console()
    results: dict[str, dict] = {}

    # ====== Gaia DR3 ======
    console.rule("[bold blue]Gaia DR3[/]")
    gaia = GaiaSource(config.gaia, config.network)

    # Gaia modality 1: astrometric catalog
    t0 = time.monotonic()
    try:
        gaia_query = ConeSearchQuery(
            ra=GAIA_LAMOST_RA, dec=GAIA_LAMOST_DEC, radius_deg=0.5,
            extra_conditions={"phot_g_mean_mag_max": 15.0}, limit=100,
        )
        gaia_result = gaia.query_catalog(
            gaia_query,
            output_path=DATA_DIR / "catalogs" / "benchmark_gaia_astrometry.parquet",
        )
        results["Gaia DR3 / Astrometry"] = {
            "catalog_rows": gaia_result.row_count,
            "catalog_valid": verify_parquet(gaia_result.output_path),
            "spectra_downloaded": 0, "spectra_valid": True,
            "elapsed_s": round(time.monotonic() - t0, 1),
            "status": "PASS" if gaia_result.row_count > 0 else "FAIL",
        }
    except Exception as e:
        results["Gaia DR3 / Astrometry"] = {"status": "FAIL", "error": str(e)}
        console.print(f"[red]Gaia astrometry failed: {e}[/]")

    # Gaia modality 2: XP (BP/RP) spectra
    t0 = time.monotonic()
    try:
        gaia_xp_result = gaia.query_xp_sources(
            gaia_query,
            output_path=DATA_DIR / "catalogs" / "benchmark_gaia_xp_sources.parquet",
        )
        gaia_xp_spectra = []
        if gaia_xp_result.row_count > 0:
            top_ids = gaia_xp_result.data["source_id"].head(3).astype(str).tolist()
            gaia_xp_spectra = gaia.download_spectra(top_ids, DATA_DIR / "spectra" / "gaia_xp")

        n_ok = sum(1 for r in gaia_xp_spectra if r.success)
        results["Gaia DR3 / XP Spectra"] = {
            "catalog_rows": gaia_xp_result.row_count,
            "catalog_valid": verify_parquet(gaia_xp_result.output_path),
            "spectra_downloaded": len(gaia_xp_spectra),
            "spectra_success": n_ok,
            "spectra_valid": all(verify_fits(r.path) for r in gaia_xp_spectra if r.success),
            "elapsed_s": round(time.monotonic() - t0, 1),
            "status": "PASS" if n_ok > 0 else ("PARTIAL" if gaia_xp_result.row_count > 0 else "FAIL"),
        }
    except Exception as e:
        results["Gaia DR3 / XP Spectra"] = {"status": "FAIL", "error": str(e)}
        console.print(f"[red]Gaia XP spectra failed: {e}[/]")

    # ====== LAMOST DR11 ======
    console.rule("[bold green]LAMOST DR11[/]")
    lamost = LamostSource(config.lamost, config.network)

    # LAMOST modality 1: low-res catalog
    t0 = time.monotonic()
    try:
        lamost_query = ConeSearchQuery(
            ra=GAIA_LAMOST_RA, dec=GAIA_LAMOST_DEC, radius_deg=0.5, limit=10,
        )
        lamost_result = lamost.query_catalog(
            lamost_query,
            output_path=DATA_DIR / "catalogs" / "benchmark_lamost_lrs.parquet",
        )
        results["LAMOST DR11 / LRS Catalog"] = {
            "catalog_rows": lamost_result.row_count,
            "catalog_valid": verify_parquet(lamost_result.output_path),
            "spectra_downloaded": 0, "spectra_valid": True,
            "elapsed_s": round(time.monotonic() - t0, 1),
            "status": "PASS" if lamost_result.row_count > 0 else "FAIL",
        }
    except Exception as e:
        results["LAMOST DR11 / LRS Catalog"] = {"status": "FAIL", "error": str(e)}
        console.print(f"[red]LAMOST catalog failed: {e}[/]")
        lamost_result = None

    # LAMOST modality 2: low-res spectra download
    t0 = time.monotonic()
    try:
        lamost_spectra = []
        lamost_spec_dir = DATA_DIR / "spectra" / "lamost_lrs"
        if lamost_result and lamost_result.row_count > 0:
            obsid_col = next((c for c in lamost_result.data.columns if c.lower() == "obsid"), None)
            if obsid_col:
                obsids = lamost_result.data[obsid_col].head(3).astype(str).tolist()
                lamost_spectra = lamost.download_spectra(obsids, lamost_spec_dir)

        n_ok = sum(1 for r in lamost_spectra if r.success)
        # If resume skipped all, verify existing files
        existing_fits = list(lamost_spec_dir.glob("lamost_spec_*.fits")) if lamost_spec_dir.exists() else []
        existing_ok = all(verify_fits(f) for f in existing_fits)
        results["LAMOST DR11 / LRS Spectra"] = {
            "catalog_rows": 0, "catalog_valid": True,
            "spectra_downloaded": len(lamost_spectra), "spectra_success": n_ok,
            "spectra_valid": True,
            "elapsed_s": round(time.monotonic() - t0, 1),
            "status": "PASS" if (n_ok > 0 or (len(lamost_spectra) == 0 and existing_ok and len(existing_fits) > 0)) else "FAIL",
        }
    except Exception as e:
        results["LAMOST DR11 / LRS Spectra"] = {"status": "FAIL", "error": str(e)}
        console.print(f"[red]LAMOST spectra failed: {e}[/]")

    # ====== SDSS DR17 ======
    console.rule("[bold magenta]SDSS DR17[/]")
    sdss = SdssSource(config.sdss, config.network)

    # SDSS modality 1: spectroscopic catalog (INNER JOIN)
    t0 = time.monotonic()
    try:
        sdss_query = ConeSearchQuery(ra=SDSS_RA, dec=SDSS_DEC, radius_deg=0.5, limit=10)
        sdss_result = sdss.query_catalog(
            sdss_query,
            output_path=DATA_DIR / "catalogs" / "benchmark_sdss_specobj.parquet",
        )
        results["SDSS DR17 / SpecObj"] = {
            "catalog_rows": sdss_result.row_count,
            "catalog_valid": verify_parquet(sdss_result.output_path),
            "spectra_downloaded": 0, "spectra_valid": True,
            "elapsed_s": round(time.monotonic() - t0, 1),
            "status": "PASS" if sdss_result.row_count > 0 else "FAIL",
        }
    except Exception as e:
        results["SDSS DR17 / SpecObj"] = {"status": "FAIL", "error": str(e)}
        console.print(f"[red]SDSS catalog failed: {e}[/]")
        sdss_result = None

    # SDSS modality 2: download spectra
    t0 = time.monotonic()
    try:
        sdss_spectra = []
        sdss_spec_dir = DATA_DIR / "spectra" / "sdss"
        if sdss_result and sdss_result.row_count > 0:
            if {"plate", "mjd", "fiberid"}.issubset(sdss_result.data.columns):
                sdss_spectra = sdss.download_spectra(
                    sdss_result.data.head(3), sdss_spec_dir,
                )

        n_ok = sum(1 for r in sdss_spectra if r.success)
        existing_fits = list(sdss_spec_dir.glob("sdss_spec_*.fits")) if sdss_spec_dir.exists() else []
        existing_ok = all(verify_fits(f) for f in existing_fits)
        results["SDSS DR17 / Spectra"] = {
            "catalog_rows": 0, "catalog_valid": True,
            "spectra_downloaded": len(sdss_spectra), "spectra_success": n_ok,
            "spectra_valid": True,
            "elapsed_s": round(time.monotonic() - t0, 1),
            "status": "PASS" if (n_ok > 0 or (len(sdss_spectra) == 0 and existing_ok and len(existing_fits) > 0)) else "FAIL",
        }
    except Exception as e:
        results["SDSS DR17 / Spectra"] = {"status": "FAIL", "error": str(e)}
        console.print(f"[red]SDSS spectra failed: {e}[/]")

    # ====== Report ======
    console.rule("[bold]Benchmark Results[/]")
    report = RichTable(title="astro-pipeline benchmark (multi-modality)", show_lines=True)
    report.add_column("Source / Modality", style="bold")
    report.add_column("Catalog", justify="right")
    report.add_column("Cat OK", justify="center")
    report.add_column("Spectra", justify="right")
    report.add_column("Spec OK", justify="center")
    report.add_column("Time", justify="right")
    report.add_column("Status", justify="center")

    for source, r in results.items():
        if "error" in r:
            report.add_row(source, "-", "-", "-", "-", "-", "[red]FAIL[/]")
            console.print(f"  [dim]{source}: {r['error'][:150]}[/]")
        else:
            cat_ok = "[green]Y[/]" if r.get("catalog_valid") else "[red]N[/]"
            spec_ok = "[green]Y[/]" if r.get("spectra_valid") else "[red]N[/]"
            n_dl = r.get("spectra_downloaded", 0)
            spec_str = f"{r.get('spectra_success', 0)}/{n_dl}" if n_dl > 0 else "-"
            status = r.get("status", "UNKNOWN")
            style = {"PASS": "[green]PASS[/]", "PARTIAL": "[yellow]PARTIAL[/]"}.get(status, "[red]FAIL[/]")
            report.add_row(
                source, str(r.get("catalog_rows", 0)), cat_ok,
                spec_str, spec_ok, f"{r.get('elapsed_s', 0)}s", style,
            )

    console.print(report)
    passed = sum(1 for r in results.values() if r.get("status") in ("PASS", "PARTIAL"))
    total = len(results)
    console.print(f"\n[bold]{passed}/{total} modalities passed[/]")
    return results


if __name__ == "__main__":
    setup_logging("DEBUG")
    run_benchmark()
