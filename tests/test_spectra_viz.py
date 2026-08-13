from pathlib import Path

import numpy as np
import pandas as pd
from astropy.table import Table

from astro_pipeline.spectra import (
    XP_SAMPLED_WAVELENGTH_NM,
    read_xp_continuous_params,
    read_xp_sampled,
)
from astro_pipeline.viz.source_paper import compute_overview, render_html
from scripts.export_jsonl import to_record


def test_read_xp_sampled_raw_fits(tmp_path: Path) -> None:
    path = tmp_path / "123.fits"
    flux = np.linspace(1e-17, 2e-17, len(XP_SAMPLED_WAVELENGTH_NM)).astype("float32")
    error = np.full_like(flux, 1e-19)
    Table({"source_id": [123], "flux": [flux], "flux_error": [error]}).write(path)

    spectrum = read_xp_sampled(path)

    assert spectrum["source_id"] == "123"
    assert spectrum["format"] == "sampled"
    assert len(spectrum["wavelength"]) == 343
    assert len(spectrum["flux"]) == 343
    assert len(spectrum["flux_error"]) == 343


def test_read_xp_continuous_params(tmp_path: Path) -> None:
    path = tmp_path / "continuous.fits"
    coefficients = np.arange(55, dtype="float64")
    errors = np.full(55, 0.25, dtype="float32")
    Table({
        "source_id": [123],
        "bp_coefficients": [coefficients],
        "bp_coefficient_errors": [errors],
        "rp_coefficients": [coefficients + 1],
        "rp_coefficient_errors": [errors * 2],
        "bp_basis_function_id": [56],
        "bp_degrees_of_freedom": [100],
        "bp_n_parameters": [55],
        "bp_n_measurements": [200],
        "bp_n_rejected_measurements": [3],
        "bp_standard_deviation": [1.5],
        "bp_chi_squared": [12.0],
        "bp_n_relevant_bases": [20],
        "bp_relative_shrinking": [0.1],
        "rp_basis_function_id": [57],
        "rp_degrees_of_freedom": [110],
        "rp_n_parameters": [55],
        "rp_n_measurements": [220],
        "rp_n_rejected_measurements": [4],
        "rp_standard_deviation": [1.7],
        "rp_chi_squared": [14.0],
        "rp_n_relevant_bases": [18],
        "rp_relative_shrinking": [0.2],
    }).write(path)

    params = read_xp_continuous_params(path)

    assert params["source_id"] == "123"
    assert len(params["bp_coefficients"]) == 55
    assert len(params["rp_coefficient_errors"]) == 55
    assert params["quality"]["bp_basis_function_id"] == 56
    assert params["quality"]["rp_n_relevant_bases"] == 18


def test_jsonl_record_uses_twa_aliases_and_xp_links() -> None:
    gaia_id = "5412403269717562240"
    lookup = {
        "gaia_id": gaia_id,
        "astrometry": {"RAICRS": 10.0, "DEICRS": -20.0},
        "multimodal": {},
        "papers": [],
    }
    extras = {
        "plx": 21.4,
        "e_plx": 0.03,
        "RUWE": 1.18,
        "Gmag": 14.5,
        "pmRA": -78.2,
        "pmDE": 9.3,
    }
    sampled = {"wavelength": [336.0, 338.0]}
    continuous = {
        "bp_coefficients": [1.0] * 55,
        "bp_coefficient_errors": [0.1] * 55,
        "rp_coefficients": [2.0] * 55,
        "rp_coefficient_errors": [0.2] * 55,
        "quality": {"bp_n_relevant_bases": 20},
    }

    record = to_record(
        lookup,
        extras,
        xp_sampled=sampled,
        xp_continuous=continuous,
        xp_sampled_available=True,
        xp_continuous_available=True,
    )

    assert record["parallax_mas"] == 21.4
    assert record["ruwe"] == 1.18
    assert record["g_mag"] == 14.5
    sampled_record = record["multimodal"]["gaia_xp"]["sampled"]
    continuous_record = record["multimodal"]["gaia_xp"]["continuous"]
    assert sampled_record["available"] is True
    assert sampled_record["cached"] is True
    assert sampled_record["datalink_url"].endswith(gaia_id)
    assert continuous_record["cached"] is True
    assert len(continuous_record["bp_coefficients"]) == 55

    uncached = to_record(
        lookup,
        extras,
        xp_sampled_available=False,
        xp_continuous_available=True,
    )["multimodal"]["gaia_xp"]["continuous"]
    assert uncached["available"] is True
    assert uncached["cached"] is False
    assert uncached["path"] is None
    assert uncached["datalink_url"].endswith(gaia_id)


def test_overview_embeds_spectrum() -> None:
    gaia_id = "5412403269717562240"
    catalog = pd.DataFrame({
        "Gaia": [gaia_id],
        "RAICRS": [10.0],
        "DEICRS": [-20.0],
        "Gmag": [12.3],
        "plx": [10.0],
        "e_plx": [0.1],
        "pmRA": [1.0],
        "pmDE": [2.0],
        "RUWE": [1.1],
        "Name": ["TWA test"],
    })
    edges = pd.DataFrame({
        "source_id": [gaia_id],
        "arxiv_id": ["2401.00001"],
        "bibcode": ["2024TEST"],
        "title": ["Test paper"],
        "doi": [""],
        "journal": ["Test"],
        "year": [2024],
    })
    spectrum = {
        "source_id": gaia_id,
        "format": "sampled",
        "wavelength": [336.0, 338.0],
        "flux": [1e-17, 2e-17],
        "flux_error": [1e-19, 1e-19],
    }

    payload = compute_overview(catalog, edges, spectra_by_source={gaia_id: spectrum})
    html = render_html(payload)

    assert payload["spectra"][gaia_id] == spectrum
    assert payload["sources"][0]["id"] == gaia_id
    assert payload["sources"][0]["has_xp"] is True
    assert "Gaia XP ${spectrumFormat}" in html
    assert "focus-spectrum" in html
