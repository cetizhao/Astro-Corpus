"""Resolve, read, and plot release-qualified Gaia BP/RP spectra.

Two retrieval types are supported (preferred order: continuous > sampled):

  * XP_CONTINUOUS  -- basis-function coefficients; gaiaxpy reconstructs the
    absolute-flux spectrum. Higher resolution, used when available.
  * XP_SAMPLED     -- already-sampled wavelength/flux arrays. Fallback when
    the source has no continuous representation.

Version-2 product references pin exact FITS blobs in the shared library and
are authoritative. Legacy local ``gaia_xp_*`` FITS files remain readable only
when no applicable v2 reference exists. Optional downloads are delegated to
the generic Gaia product pipeline, which publishes the resulting references.
Rendered PNGs live under ``gaia_xp_png`` and carry a sidecar keyed by the
pinned FITS hash and renderer contract, so product refreshes invalidate them.

The high-level entry point ``get_xp_spectrum_png()`` returns a dict with a
``file://`` URL suitable for MCP tool consumption.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import numpy as np

from astro_pipeline.library.files import BlobIntegrityError, atomic_json
from astro_pipeline.library.refs import ProductReferenceError, resolve_product

logger = logging.getLogger(__name__)

CONTINUOUS_SUBDIR = "gaia_xp_continuous"
SAMPLED_SUBDIR = "gaia_xp_sampled"
PNG_SUBDIR = "gaia_xp_png"
XP_SAMPLED_WAVELENGTH_NM = np.arange(336.0, 1021.0, 2.0)
XP_PLOT_RENDERER_VERSION = "2"
XP_PLOT_OPTIONS = {
    "figsize": [10, 4],
    "dpi": 110,
    "line_color": "#60a5fa",
    "line_width": 1.0,
    "error_alpha": 0.2,
    "xlim": [330, 1100],
}


def _json_float_list(values: Any) -> list[float | None]:
    array = np.asarray(values, dtype=float)
    return [float(value) if np.isfinite(value) else None for value in array]


# --------------------------------------------------------------------- fetch
def fetch_xp_spectrum(
    gaia_id: str,
    cache_dir: Path | str,
    *,
    allow_download: bool = False,
    release: str = "dr3",
) -> dict:
    """Find (or download) a verified XP spectrum for ``gaia_id``.

    Explicit v2 product references are authoritative and pin the exact shared
    blob version.  Legacy local FITS files are used only when the relevant
    product has no v2 references.  Continuous is preferred over sampled.
    """
    cache = Path(cache_dir)
    attempts: list[str] = []
    products = [
        ("continuous", CONTINUOUS_SUBDIR, "XP_CONTINUOUS"),
        ("sampled", SAMPLED_SUBDIR, "XP_SAMPLED"),
    ]

    for fmt, _subdir, retrieval_type in products:
        try:
            resolved = resolve_product(
                cache,
                gaia_id,
                retrieval_type,
                release=release,
            )
        except (BlobIntegrityError, FileNotFoundError, ProductReferenceError, ValueError) as exc:
            attempts.append(f"{retrieval_type}: {exc}")
            continue
        return {
            "format": fmt,
            "source_id": str(gaia_id),
            **resolved,
        }

    if allow_download:
        # The generic GaiaSource downloader owns authentication, product
        # normalization, shared-library reuse, and reference publication.
        for fmt, subdir, retrieval_type in products:
            try:
                _download_via_datalink(
                    [str(gaia_id)],
                    cache / subdir,
                    retrieval_type,
                    release=release,
                )
                resolved = resolve_product(
                    cache,
                    gaia_id,
                    retrieval_type,
                    release=release,
                )
            except Exception as exc:
                logger.debug("download %s failed for %s: %s", fmt, gaia_id, exc)
                attempts.append(f"download {retrieval_type}: {exc}")
                continue
            logger.info("downloaded %s spectrum for %s", fmt, gaia_id)
            return {
                "format": fmt,
                "source_id": str(gaia_id),
                **resolved,
            }

    details = "; ".join(attempts[-4:])
    raise LookupError(
        f"No verified XP spectrum for Gaia {release.upper()} {gaia_id} under {cache}."
        + (f" {details}" if details else "")
    )


def _download_via_datalink(
    source_ids: list[str],
    out_dir: Path,
    retrieval_type: str,
    *,
    release: str,
) -> None:
    """Use the generic release-aware Gaia product downloader."""
    from astro_pipeline.config import GaiaConfig, PipelineConfig
    from astro_pipeline.library import get_library
    from astro_pipeline.sources.gaia import GaiaSource

    config = PipelineConfig.load()
    configured_release = config.gaia.release
    gaia_values = config.gaia.model_dump()
    gaia_values["release"] = release
    if release != configured_release:
        gaia_values["db_schema"] = None
    gaia_config = GaiaConfig(**gaia_values)
    library = get_library(config, create=True)
    gaia = GaiaSource(
        gaia_config,
        config.network,
        library_store=library,
        library_run_id=None,
        library_refresh=False,
    )
    gaia.download_spectra(
        source_ids,
        out_dir,
        resume=True,
        retrieval_type=retrieval_type,
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

    fig, ax = plt.subplots(
        figsize=tuple(XP_PLOT_OPTIONS["figsize"]),
        dpi=XP_PLOT_OPTIONS["dpi"],
    )

    wavelength = np.asarray(spectrum.get("wavelength", []), dtype=float)
    flux = np.asarray(spectrum.get("flux", []), dtype=float)
    flux_error = np.asarray(spectrum.get("flux_error", []), dtype=float)

    if wavelength.size:
        ax.plot(
            wavelength,
            flux,
            color=XP_PLOT_OPTIONS["line_color"],
            lw=XP_PLOT_OPTIONS["line_width"],
            label="XP sampled",
        )
        if flux_error.size == flux.size:
            ax.fill_between(
                wavelength, flux - flux_error, flux + flux_error,
                color=XP_PLOT_OPTIONS["line_color"],
                alpha=XP_PLOT_OPTIONS["error_alpha"],
                linewidth=0,
            )

    ax.set_xlabel("Wavelength [nm]")
    ax.set_ylabel(r"Flux [W m$^{-2}$ nm$^{-1}$]")
    ax.set_xlim(*XP_PLOT_OPTIONS["xlim"])
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
    release: str = "dr3",
) -> dict:
    """Fetch, read, and render an XP spectrum with content-aware PNG caching.

    A cached PNG is reusable only when its sidecar records the same verified
    FITS SHA-256 plus the current renderer version and visual options.  Product
    refreshes therefore invalidate plots even when the Gaia identifier and PNG
    filename are unchanged.
    """
    cache = Path(cache_dir)
    png_path = cache / PNG_SUBDIR / f"{gaia_id}.png"
    metadata_path = png_path.with_suffix(png_path.suffix + ".json")

    try:
        spec_meta = fetch_xp_spectrum(
            gaia_id,
            cache,
            allow_download=allow_download,
            release=release,
        )
    except (BlobIntegrityError, LookupError, ProductReferenceError, ValueError) as exc:
        return {"error": str(exc), "gaia_id": gaia_id}

    expected_metadata = _png_cache_metadata(spec_meta)
    if (
        png_path.is_file()
        and not force_refresh
        and _png_cache_matches(metadata_path, expected_metadata)
    ):
        cached_metadata = _read_json(metadata_path) or {}
        return _png_result(
            gaia_id,
            png_path,
            format="cached",
            cached=True,
            n_points=int(cached_metadata.get("n_points", 0)),
        )

    try:
        spectrum = read_xp_spectrum(spec_meta)
    except (LookupError, ValueError) as exc:
        return {"error": str(exc), "gaia_id": gaia_id}

    try:
        render_xp_plot(spectrum, png_path)
        atomic_json(
            metadata_path,
            {
                **expected_metadata,
                "n_points": len(spectrum.get("wavelength", [])),
            },
        )
    except Exception as exc:
        return {"error": f"plot render failed: {exc}", "gaia_id": gaia_id}

    return _png_result(
        gaia_id,
        png_path,
        format=spectrum["format"],
        cached=False,
        n_points=len(spectrum.get("wavelength", [])),
    )


def _png_cache_metadata(spec_meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source_sha256": str(spec_meta["sha256"]),
        "source_version_id": spec_meta.get("version_id"),
        "source_format": str(spec_meta["format"]),
        "renderer_version": XP_PLOT_RENDERER_VERSION,
        "renderer_options": XP_PLOT_OPTIONS,
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _png_cache_matches(path: Path, expected: dict[str, Any]) -> bool:
    actual = _read_json(path)
    return actual is not None and all(
        actual.get(key) == value for key, value in expected.items()
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
