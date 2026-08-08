"""Gaia DR3 BP/RP spectrum fetching and plotting.

Two retrieval types are supported (preferred order: continuous > sampled):

  * XP_CONTINUOUS  -- basis-function coefficients; gaiaxpy reconstructs the
    absolute-flux spectrum. Higher resolution, used when available.
  * XP_SAMPLED     -- already-sampled wavelength/flux arrays. Fallback when
    the source has no continuous representation.

Caching layout under ``cache_dir``::

    cache_dir/gaia_xp_continuous/<gaia_id>.fits    # XP_CONTINUOUS source file
    cache_dir/gaia_xp_sampled/<gaia_id>.fits       # XP_SAMPLED source file
    cache_dir/gaia_xp_png/<gaia_id>.png            # rendered plot

The fetcher checks for the continuous file first, then sampled; if neither
exists, it tries to download continuous via astroquery.gaia (which requires
Gaia archive credentials -- see README §13.2 for bootstrap).

The high-level entry point ``get_xp_spectrum_png()`` returns a dict with a
``file://`` URL suitable for MCP tool consumption.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

CONTINUOUS_SUBDIR = "gaia_xp_continuous"
SAMPLED_SUBDIR = "gaia_xp_sampled"
PNG_SUBDIR = "gaia_xp_png"
XP_SAMPLED_WAVELENGTH_NM = np.arange(336.0, 1021.0, 2.0)


def _json_float_list(values: Any) -> list[float | None]:
    array = np.asarray(values, dtype=float)
    return [float(value) if np.isfinite(value) else None for value in array]


# --------------------------------------------------------------------- fetch
def fetch_xp_spectrum(
    gaia_id: str,
    cache_dir: Path | str,
    *,
    allow_download: bool = False,
) -> dict:
    """Find (or download) the cached XP spectrum for ``gaia_id``.

    Returns a dict with ``format`` ("continuous" | "sampled"), ``path`` to
    the source FITS, and ``source_id``. Raises ``LookupError`` if neither
    cached format exists (and ``allow_download=False`` or download failed).
    """
    cache = Path(cache_dir)
    cont_path = cache / CONTINUOUS_SUBDIR / f"{gaia_id}.fits"
    samp_path = cache / SAMPLED_SUBDIR / f"{gaia_id}.fits"

    if cont_path.exists():
        return {"format": "continuous", "path": cont_path, "source_id": gaia_id}
    if samp_path.exists():
        return {"format": "sampled", "path": samp_path, "source_id": gaia_id}

    if allow_download:
        # Try continuous first, then sampled. Both require Gaia archive auth.
        for fmt, subdir, retrieval_type in [
            ("continuous", CONTINUOUS_SUBDIR, "XP_CONTINUOUS"),
            ("sampled", SAMPLED_SUBDIR, "XP_SAMPLED"),
        ]:
            try:
                _download_via_datalink([gaia_id], cache / subdir, retrieval_type)
            except Exception as e:
                logger.debug("download %s failed for %s: %s", fmt, gaia_id, e)
                continue
            path = cache / subdir / f"{gaia_id}.fits"
            if path.exists():
                logger.info("downloaded %s spectrum for %s", fmt, gaia_id)
                return {"format": fmt, "path": path, "source_id": gaia_id}

    raise LookupError(
        f"No cached XP spectrum for Gaia DR3 {gaia_id} under {cache} "
        f"(checked {CONTINUOUS_SUBDIR}/ and {SAMPLED_SUBDIR}/). "
        f"Run examples/twa_sample.py or set Gaia credentials to enable download."
    )


def _download_via_datalink(
    source_ids: list[str], out_dir: Path, retrieval_type: str
) -> None:
    """Download via astroquery.gaia (requires auth for XP_*).

    Reads credentials from config/local.yaml via PipelineConfig.load().
    Thin wrapper around astroquery; reused from sources/gaia.py logic but kept
    standalone so this module has no hard dependency on the project's source
    abstraction.
    """
    from astroquery.gaia import Gaia  # local import: heavy + networked

    from astro_pipeline.config import PipelineConfig

    cfg = PipelineConfig.load()
    if cfg.gaia.username:
        Gaia.login(user=cfg.gaia.username, password=cfg.gaia.password)
        logger.info("Logged in to Gaia archive as %s", cfg.gaia.username)

    out_dir.mkdir(parents=True, exist_ok=True)
    dl = Gaia.load_data(
        ids=source_ids,
        data_release="Gaia DR3",
        data_structure="RAW",
        retrieval_type=retrieval_type,
        format="fits",
    )
    for _key, tables in (dl or {}).items():
        for table in tables:
            from astropy.table import Table

            if not isinstance(table, Table):
                table = Table(table)
            names = {name.lower(): name for name in table.colnames}
            sid_col = names.get("source_id") or names.get("sourceid")
            if not sid_col:
                continue
            for row_index in range(len(table)):
                sid = str(table[sid_col][row_index])
                table[row_index : row_index + 1].write(
                    str(out_dir / f"{sid}.fits"), format="fits", overwrite=True,
                )


# --------------------------------------------------------------------- read
def read_xp_continuous(fits_path: Path | str) -> dict:
    """Parse an XP_CONTINUOUS FITS via gaiaxpy on a common wavelength grid.

    Uses gaiaxpy.calibrate to reconstruct the absolute-flux spectrum from
    basis-function coefficients. Output is uniformly sampled to a default
    grid by gaiaxpy.
    """
    from gaiaxpy import calibrate  # local import: heavy

    sampled, sampling = calibrate(str(fits_path), save_file=False)
    if len(sampled) == 0:
        raise ValueError(f"gaiaxpy returned no spectra for {fits_path}")

    row = sampled.iloc[0]
    return {
        "format": "continuous",
        "source_id": str(row.get("source_id", "")),
        "wavelength": _json_float_list(sampling),
        "flux": _json_float_list(row["flux"]),
        "flux_error": _json_float_list(row["flux_error"]),
    }


def read_xp_continuous_params(fits_path: Path | str) -> dict:
    """Extract coefficients, errors, and quality params from XP_CONTINUOUS FITS."""
    from astropy.table import Table

    table = Table.read(str(fits_path), format="fits")
    if len(table) == 0:
        raise ValueError(f"Empty XP_CONTINUOUS table in {fits_path}")
    names = {name.lower(): name for name in table.colnames}
    sid_col = names.get("source_id") or names.get("sourceid")
    required = (
        "bp_coefficients", "bp_coefficient_errors",
        "rp_coefficients", "rp_coefficient_errors",
    )
    missing = [column for column in required if column not in names]
    if missing:
        raise ValueError(f"Missing XP_CONTINUOUS columns in {fits_path}: {missing}")

    return {
        "format": "continuous",
        "source_id": str(table[sid_col][0]) if sid_col else "",
        "bp_coefficients": _json_float_list(table[names["bp_coefficients"]][0]),
        "bp_coefficient_errors": _json_float_list(table[names["bp_coefficient_errors"]][0]),
        "rp_coefficients": _json_float_list(table[names["rp_coefficients"]][0]),
        "rp_coefficient_errors": _json_float_list(table[names["rp_coefficient_errors"]][0]),
        "quality": {
            column: _first_numeric(table, column)
            for column in (
                "bp_basis_function_id", "bp_degrees_of_freedom", "bp_n_parameters",
                "bp_n_measurements", "bp_n_rejected_measurements",
                "bp_standard_deviation", "bp_chi_squared", "bp_n_relevant_bases",
                "bp_relative_shrinking", "rp_basis_function_id",
                "rp_degrees_of_freedom", "rp_n_parameters", "rp_n_measurements",
                "rp_n_rejected_measurements", "rp_standard_deviation",
                "rp_chi_squared", "rp_n_relevant_bases", "rp_relative_shrinking",
            )
        },
    }


def _first_numeric(table, col_name_lower: str, default=None):
    names = {name.lower(): name for name in table.colnames}
    col = names.get(col_name_lower)
    if not col:
        return default
    val = table[col][0]
    if isinstance(val, (np.integer, int)):
        return int(val)
    if isinstance(val, (np.floating, float)):
        return float(val) if np.isfinite(float(val)) else default
    return default


def read_xp_sampled(fits_path: Path | str) -> dict:
    """Parse an XP_SAMPLED FITS produced by Gaia DataLink RAW serialization."""
    from astropy.table import Table

    table = Table.read(str(fits_path), format="fits")
    names = {name.lower(): name for name in table.colnames}
    flux_col = names.get("flux")
    error_col = names.get("flux_error")
    if not flux_col or not error_col or len(table) == 0:
        raise ValueError(f"No flux/flux_error arrays in {fits_path}")

    first_flux = table[flux_col][0]
    if np.ndim(first_flux) > 0:
        flux = np.asarray(first_flux, dtype=float)
        flux_error = np.asarray(table[error_col][0], dtype=float)
    else:
        flux = np.asarray(table[flux_col], dtype=float)
        flux_error = np.asarray(table[error_col], dtype=float)

    wavelength_col = names.get("wavelength")
    if wavelength_col:
        first_wavelength = table[wavelength_col][0]
        wavelength = (
            np.asarray(first_wavelength, dtype=float)
            if np.ndim(first_wavelength) > 0
            else np.asarray(table[wavelength_col], dtype=float)
        )
    elif len(flux) == len(XP_SAMPLED_WAVELENGTH_NM):
        wavelength = XP_SAMPLED_WAVELENGTH_NM
    else:
        wavelength = np.linspace(336.0, 1020.0, len(flux))

    sid_col = names.get("source_id") or names.get("sourceid")
    return {
        "format": "sampled",
        "source_id": str(table[sid_col][0]) if sid_col else "",
        "wavelength": _json_float_list(wavelength),
        "flux": _json_float_list(flux),
        "flux_error": _json_float_list(flux_error),
    }


def read_xp_spectrum(spec_meta: dict) -> dict:
    """Dispatch to the right reader based on the format from fetch_xp_spectrum."""
    fmt = spec_meta["format"]
    if fmt == "continuous":
        return read_xp_continuous(spec_meta["path"])
    if fmt == "sampled":
        return read_xp_sampled(spec_meta["path"])
    raise ValueError(f"unknown XP spectrum format: {fmt!r}")


# --------------------------------------------------------------------- plot
def render_xp_plot(spectrum: dict, out_path: Path | str, *, title: Optional[str] = None) -> Path:
    """Render a sampled XP spectrum to a PNG file. Returns the PNG path."""
    import matplotlib
    matplotlib.use("Agg")  # headless backend (server context)
    import matplotlib.pyplot as plt

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 4), dpi=110)

    wavelength = np.asarray(spectrum.get("wavelength", []), dtype=float)
    flux = np.asarray(spectrum.get("flux", []), dtype=float)
    flux_error = np.asarray(spectrum.get("flux_error", []), dtype=float)

    if wavelength.size:
        ax.plot(wavelength, flux, color="#60a5fa", lw=1.0, label="XP sampled")
        if flux_error.size == flux.size:
            ax.fill_between(
                wavelength, flux - flux_error, flux + flux_error,
                color="#60a5fa", alpha=0.2, linewidth=0,
            )

    ax.set_xlabel("Wavelength [nm]")
    ax.set_ylabel(r"Flux [W m$^{-2}$ nm$^{-1}$]")
    ax.set_xlim(330, 1100)
    ax.legend(loc="upper right", framealpha=0.9)
    ax.grid(alpha=0.25)

    if title:
        ax.set_title(title, fontsize=10)
    elif spectrum.get("source_id"):
        ax.set_title(
            f"Gaia DR3 {spectrum['source_id']}  ({spectrum.get('format', '?')} XP)",
            fontsize=10,
        )

    fig.tight_layout()
    fig.savefig(str(out))
    plt.close(fig)
    return out


# --------------------------------------------------------------------- top-level
def get_xp_spectrum_png(
    gaia_id: str,
    cache_dir: Path | str,
    *,
    force_refresh: bool = False,
    allow_download: bool = False,
) -> dict:
    """High-level orchestrator: fetch + read + render, returning a result dict.

    Returns
    -------
    dict with: url (file://), format, cached (PNG), path_png, gaia_id,
    n_points. On failure: {"error": str}.

    PNG rendering is cached at ``cache_dir/gaia_xp_png/<gaia_id>.png``.
    Pass ``force_refresh=True`` to overwrite.
    """
    cache = Path(cache_dir)
    png_path = cache / PNG_SUBDIR / f"{gaia_id}.png"

    if png_path.exists() and not force_refresh:
        # PNG already rendered; return immediately.
        return _png_result(gaia_id, png_path, format="cached", cached=True)

    try:
        spec_meta = fetch_xp_spectrum(gaia_id, cache, allow_download=allow_download)
        spectrum = read_xp_spectrum(spec_meta)
    except (LookupError, ValueError) as e:
        return {"error": str(e), "gaia_id": gaia_id}

    try:
        render_xp_plot(spectrum, png_path)
    except Exception as e:
        return {"error": f"plot render failed: {e}", "gaia_id": gaia_id}

    return _png_result(
        gaia_id, png_path, format=spectrum["format"], cached=False,
        n_points=len(spectrum.get("wavelength", [])),
    )


def _png_result(
    gaia_id: str, png_path: Path, *, format: str, cached: bool,
    n_points: int = 0,
) -> dict:
    return {
        "gaia_id": gaia_id,
        "url": f"file://{png_path.resolve()}",
        "format": format,
        "cached": cached,
        "path_png": str(png_path),
        "n_points": n_points,
    }
