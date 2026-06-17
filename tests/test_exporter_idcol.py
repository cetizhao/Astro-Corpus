"""Tests for the exporter multimodal ID-column fix (v0.2).

The previous implementation guessed the id column from
[source_id, gaia_source_id, obsid, specobjid] and silently broke membership
flags: LAMOST's ``obsid`` (an 8-digit int) was compared against the catalog's
19-digit Gaia id, so every flag was False. These tests pin the correct behavior.
"""
from __future__ import annotations

import pandas as pd

from astro_pipeline.io.exporter import build_knowledge_table, find_gaia_id_col


def _catalog():
    return pd.DataFrame({
        "Gaia": ["111", "222", "333"],
        "RAICRS": [10.0, 20.0, 30.0],
        "DEICRS": [-10.0, -20.0, -30.0],
    })


def test_find_gaia_id_col_prefers_gaia_over_obsid():
    """find_gaia_id_col must NOT pick obsid when a real Gaia column exists."""
    lamost = pd.DataFrame({
        "obsid": [80000001, 80000002],
        "gaiadr3": ["111", "333"],  # the actual Gaia ids
        "snrg": [20.0, 15.0],
    })
    assert find_gaia_id_col(lamost) == "gaiadr3"


def test_find_gaia_id_col_returns_none_when_no_match():
    df = pd.DataFrame({"foo": [1], "bar": [2]})
    assert find_gaia_id_col(df) is None


def test_find_gaia_id_col_explicit_wins():
    df = pd.DataFrame({"source_id": ["111"], "Gaia": ["222"]})
    assert find_gaia_id_col(df, explicit="Gaia") == "Gaia"


def test_has_lamost_flag_truthful():
    """The core regression: LAMOST availability must reflect the Gaia-keyed match."""
    lamost = pd.DataFrame({
        "obsid": [80000001, 80000002],
        "gaiadr3": ["111", "333"],
    })
    result = build_knowledge_table(
        _catalog(),
        lamost_spectra_info=lamost,
        id_col="Gaia",
        lamost_id_col="gaiadr3",
    )
    # Sources 111 and 333 have LAMOST; 222 does not.
    assert result.loc[result["Gaia"] == "111", "has_lamost_spec"].iloc[0] is True or bool(
        result.loc[result["Gaia"] == "111", "has_lamost_spec"].iloc[0]
    )
    assert not bool(result.loc[result["Gaia"] == "222", "has_lamost_spec"].iloc[0])
    assert bool(result.loc[result["Gaia"] == "333", "has_lamost_spec"].iloc[0])


def test_has_gaia_xp_flag_truthful():
    xp = pd.DataFrame({"source_id": ["222"], "has_xp_sampled": ["true"]})
    result = build_knowledge_table(
        _catalog(),
        gaia_spectra_info=xp,
        id_col="Gaia",
        gaia_id_col="source_id",
    )
    assert bool(result.loc[result["Gaia"] == "222", "has_gaia_xp"].iloc[0])
    assert not bool(result.loc[result["Gaia"] == "111", "has_gaia_xp"].iloc[0])


def test_no_survey_info_yields_all_false():
    result = build_knowledge_table(_catalog(), id_col="Gaia")
    assert not result["has_gaia_xp"].any()
    assert not result["has_lamost_spec"].any()
    assert not result["has_sdss_spec"].any()


def test_empty_survey_info_does_not_crash():
    result = build_knowledge_table(
        _catalog(),
        lamost_spectra_info=pd.DataFrame(columns=["obsid", "gaiadr3"]),
        id_col="Gaia",
    )
    assert not result["has_lamost_spec"].any()
