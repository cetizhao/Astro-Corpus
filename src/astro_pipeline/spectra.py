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

    Thin wrapper around astroquery; reused from sources/gaia.py logic but kept
    standalone so this module has no hard dependency on the project's source
    abstraction.
    """
    from astroquery.gaia import Gaia  # local import: heavy + networked

    out_dir.mkdir(parents=True, exist_ok=True)
    dl = Gaia.load_data(
        ids=source_ids,
        data_release="DR3",
        data_structure="RAW",
        retrieval_type=retrieval_type,
        format="fits",
    )
    for _key, tables in (dl or {}).items():
        for table in tables:
            try:
                data = table.data if hasattr(table, "data") else table[1].data
                sid = str(data["source_id"][0]) if "source_id" in data.columns.names else None
            except Exception:
                sid = None
            if not sid:
                continue
            from astropy.io import fits as afits

            out = out_dir / f"{sid}.fits"
            if hasattr(table, "writeto"):
                table.writeto(str(out), overwrite=True)
            else:
                afits.writeto(str(out), table.data, overwrite=True)


# --------------------------------------------------------------------- read
def read_xp_continuous(fits_path: Path | str) -> dict:
    """Parse an XP_CONTINUOUS FITS via gaiaxpy; return sampled BP/RP arrays.

    Uses gaiaxpy.calibrate to reconstruct the absolute-flux spectrum from
    basis-function coefficients. Output is uniformly sampled to a default
    grid by gaiaxpy.
    """
    from gaiaxpy import calibrate  # local import: heavy

    sampled, _sampling = calibrate(str(fits_path))
    if len(sampled) == 0:
        raise ValueError(f"gaiaxpy returned no spectra for {fits_path}")

    # gaiaxpy returns one row per source with 'bp' and 'rp' columns holding
    # arrays of (wavelength, flux).
    row = sampled.iloc[0]
    return {
        "format": "continuous",
        "source_id": str(row.get("source_id", "")),
        "bp_wavelength": np.asarray(row["bp"]["wavelength"]).tolist(),
        "bp_flux": np.asarray(row["bp"]["flux"]).tolist(),
        "rp_wavelength": np.asarray(row["rp"]["wavelength"]).tolist(),
        "rp_flux": np.asarray(row["rp"]["flux"]).tolist(),
    }


def read_xp_sampled(fits_path: Path | str) -> dict:
    """Parse an XP_SAMPLED FITS as written by astroquery/Gaia DataLink.

    Each file is one BinTable HDU per source with columns ``source_id``,
    ``bp_wavelength``, ``bp_flux``, ``rp_wavelength``, ``rp_flux`` (float
    arrays). Tolerates column-name variation.
    """
    from astropy.io import fits as afits

    with afits.open(str(fits_path)) as hdul:
        # Find the BinTable that has wavelength/flux columns.
        for hdu in hdul:
            if not hasattr(hdu, "columns"):
                continue
            names = {n.lower(): n for n in hdu.columns.names}
            bp_wl = names.get("bp_wavelength")
            bp_fx = names.get("bp_flux")
            rp_wl = names.get("rp_wavelength")
            rp_fx = names.get("rp_flux")
            if not (bp_wl and bp_fx and rp_wl and rp_fx):
                continue
            sid_col = names.get("source_id")
            data = hdu.data
            return {
                "format": "sampled",
                "source_id": str(data[sid_col][0]) if sid_col else "",
                "bp_wavelength": np.asarray(data[bp_wl][0]).tolist(),
                "bp_flux": np.asarray(data[bp_fx][0]).tolist(),
                "rp_wavelength": np.asarray(data[rp_wl][0]).tolist(),
                "rp_flux": np.asarray(data[rp_fx][0]).tolist(),
            }
    raise ValueError(
        f"No HDU with bp_wavelength/bp_flux/rp_wavelength/rp_flux in {fits_path}"
    )


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
    """Render BP/RP spectrum to a PNG file. Returns the PNG path."""
    import matplotlib
    matplotlib.use("Agg")  # headless backend (server context)
    import matplotlib.pyplot as plt

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 4), dpi=110)

    bp_wl = np.asarray(spectrum.get("bp_wavelength", []))
    bp_fx = np.asarray(spectrum.get("bp_flux", []))
    rp_wl = np.asarray(spectrum.get("rp_wavelength", []))
    rp_fx = np.asarray(spectrum.get("rp_flux", []))

    if bp_wl.size:
        ax.plot(bp_wl, bp_fx, color="#3b82f6", lw=0.8, label="BP", alpha=0.85)
    if rp_wl.size:
        ax.plot(rp_wl, rp_fx, color="#ef4444", lw=0.8, label="RP", alpha=0.85)

    ax.set_xlabel("Wavelength [nm]")
    ax.set_ylabel(r"Flux [$10^{-17}$ W m$^{-2}$ nm$^{-1}$]")
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
    bp_n_points, rp_n_points. On failure: {"error": str}.

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

    return _png_result(gaia_id, png_path, format=spectrum["format"], cached=False,
                       bp_n=len(spectrum.get("bp_wavelength", [])),
                       rp_n=len(spectrum.get("rp_wavelength", [])))


def _png_result(
    gaia_id: str, png_path: Path, *, format: str, cached: bool,
    bp_n: int = 0, rp_n: int = 0,
) -> dict:
    return {
        "gaia_id": gaia_id,
        "url": f"file://{png_path.resolve()}",
        "format": format,
        "cached": cached,
        "path_png": str(png_path),
        "bp_n_points": bp_n,
        "rp_n_points": rp_n,
    }
