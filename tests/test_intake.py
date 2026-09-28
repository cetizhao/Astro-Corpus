"""Tests for ``astro_pipeline.intake`` -- input normalization + canonical schema.

Pure-function tests (no network) cover input validation, column renaming,
and truncation behavior. They are the regression sentinels: any future
refactor that breaks the contract (canonical column names, validation
errors, truncation when limit exceeded) is caught at pytest time.

Network-marked tests verify the live SIMBAD/Gaia paths end-to-end. They
are skipped by default (``addopts = "-m 'not network'"``); run with
``pytest -m network`` when exercising the live path.
"""
from __future__ import annotations

import logging

import pandas as pd
import pytest

from astro_pipeline.intake import (
    CANONICAL_RENAME,
    QC_EXTRA_COLUMNS,
    _canonicalize,
    resolve_members,
)


# --------------------------------------------------------------------- validation
def test_resolve_members_rejects_no_input():
    with pytest.raises(ValueError, match="Exactly one of"):
        resolve_members()


@pytest.mark.parametrize("kwargs", [
    {"cone": (1.0, 2.0, 3.0), "name": "X"},
    {"cone": (1.0, 2.0, 3.0), "catalog": "x.csv", "name": "X"},
    {"name": "X", "region": ("Y", 1.0)},
    {"cone": (1.0, 2.0, 3.0), "catalog": "x.csv", "name": "X", "region": ("Y", 1.0)},
])
def test_resolve_members_rejects_multiple_inputs(kwargs):
    with pytest.raises(ValueError, match="Exactly one of"):
        resolve_members(**kwargs)


# --------------------------------------------------------------------- canonicalize
def _gaia_native_df() -> pd.DataFrame:
    """A minimal DataFrame with Gaia DR3 native column names."""
    return pd.DataFrame({
        "source_id": [5401795662560500352, 5412403269717562240],
        "ra": [165.466, 146.615],
        "dec": [-34.705, -44.961],
        "parallax": [16.63, 21.44],
        "parallax_error": [0.015, 0.031],
        "pmra": [-68.31, -78.26],
        "pmdec": [-13.90, 9.26],
        "phot_g_mean_mag": [10.45, 14.52],
        "ruwe": [1.175, 1.186],
        "phot_bp_rp_excess_factor": [1.351, 2.871],   # QC extra, not in rename map
        "astrometric_excess_noise": [0.094, 0.274],
    })


def test_canonicalize_renames_expected_columns():
    out = _canonicalize(_gaia_native_df())
    for native, canonical in CANONICAL_RENAME.items():
        if native in _gaia_native_df().columns:
            assert canonical in out.columns, f"{native} not renamed to {canonical}"
            assert native not in out.columns


def test_canonicalize_preserves_unknown_columns():
    """Columns not in CANONICAL_RENAME (e.g. QC extras) should pass through unchanged."""
    out = _canonicalize(_gaia_native_df())
    assert "phot_bp_rp_excess_factor" in out.columns
    assert "astrometric_excess_noise" in out.columns


def test_canonicalize_gaia_column_is_string():
    """Downstream code uses str comparison on Gaia ids; enforce str values."""
    out = _canonicalize(_gaia_native_df())
    assert all(isinstance(x, str) for x in out["Gaia"])


def test_canonicalize_handles_empty_df():
    empty = _gaia_native_df().iloc[0:0]
    out = _canonicalize(empty)
    assert out.empty
    assert "Gaia" in out.columns  # rename still applied to columns


def test_canonical_rename_map_covers_essentials():
    """The downstream pipeline reads at minimum these fields. Catch accidental
    removal of a canonical alias."""
    must_have = {"source_id": "Gaia", "ra": "RAICRS", "dec": "DEICRS",
                 "parallax": "Plx_gdr3", "ruwe": "RUWE_gdr3",
                 "phot_g_mean_mag": "Gmag_gdr3"}
    for native, canonical in must_have.items():
        assert CANONICAL_RENAME.get(native) == canonical, \
            f"{native} lost its canonical mapping"


def test_qc_extras_are_nonempty_and_strings():
    assert QC_EXTRA_COLUMNS
    assert all(isinstance(c, str) and c for c in QC_EXTRA_COLUMNS)


# --------------------------------------------------------------------- truncation
def test_from_cone_truncates_and_warns_when_over_limit(monkeypatch, caplog):
    """Even if Gaia returns more rows than ``limit``, we must truncate and warn."""
    from astro_pipeline import intake

    # Mock GaiaSource.query_catalog to return 100 fake rows without hitting the network.
    fake_df = pd.DataFrame({
        "source_id": list(range(100)),
        "ra": [165.0] * 100, "dec": [-34.0] * 100,
        "parallax": [10.0] * 100, "parallax_error": [0.1] * 100,
        "pmra": [0.0] * 100, "pmdec": [0.0] * 100,
        "phot_g_mean_mag": [12.0] * 100, "ruwe": [1.0] * 100,
    })

    class _FakeGaiaSource:
        def __init__(self, *a, **kw): pass
        def query_catalog(self, q, output_path=None):
            class _R: data = fake_df.copy()
            return _R()
        def _query_by_ids(self, q, extra_columns=None, output_path=None):
            class _R: data = fake_df.copy()
            return _R()

    monkeypatch.setattr(intake, "GaiaSource", _FakeGaiaSource)

    with caplog.at_level(logging.WARNING):
        out = resolve_members(cone=(165.0, -34.0, 1.0), limit=5)
    assert len(out) == 5
    assert any("truncating to 5" in rec.message for rec in caplog.records)


def test_position_catalog_uses_batched_match_and_applies_limit_first(
    monkeypatch, tmp_path
):
    from astro_pipeline import intake

    catalog = tmp_path / "positions.csv"
    pd.DataFrame(
        {"RAICRS": [10.0, 11.0, 12.0], "DEICRS": [20.0, 21.0, 22.0]}
    ).to_csv(catalog, index=False)
    captured = {}

    class _FakeGaiaSource:
        def __init__(self, *args, **kwargs):
            pass

        def crossmatch_positions(self, positions, **kwargs):
            captured["positions"] = positions.copy()
            captured["kwargs"] = kwargs
            row_id_col = kwargs["row_id_col"]
            return pd.DataFrame(
                {
                    row_id_col: [1, 0],
                    "source_id": [200, 100],
                    "ra": [11.0, 10.0],
                    "dec": [21.0, 20.0],
                    "match_sep_arcsec": [0.1, 0.1],
                    "match_candidate_count": [1, 1],
                }
            )

    monkeypatch.setattr(intake, "GaiaSource", _FakeGaiaSource)
    result = resolve_members(catalog=catalog, limit=2)

    assert len(captured["positions"]) == 2
    assert captured["kwargs"]["batch_size"] == 25
    assert result["Gaia"].tolist() == ["100", "200"]


# --------------------------------------------------------------------- live (network)
@pytest.mark.network
def test_resolve_members_name_mode_tw_hya():
    """Live: SIMBAD -> Gaia DR3 lookup for a well-known source."""
    df = resolve_members(name="TW Hya")
    assert len(df) == 1
    assert str(df["Gaia"].iloc[0]) == "5401795662560500352"
    # TW Hya is at ~60 pc; parallax ~16.6 mas is stable across Gaia DR3 reprocessings.
    assert 16.0 < float(df["Plx_gdr3"].iloc[0]) < 17.5
    # QC extras should be present
    assert "phot_bp_rp_excess_factor" in df.columns


@pytest.mark.network
def test_resolve_members_cone_mode_returns_canonical_columns():
    """Live: small cone search returns canonical column names."""
    df = resolve_members(cone=(165.466, -34.705, 0.1), limit=5)
    assert len(df) > 0
    for col in ("Gaia", "RAICRS", "DEICRS", "Plx_gdr3", "RUWE_gdr3"):
        assert col in df.columns, f"missing canonical column {col}"
