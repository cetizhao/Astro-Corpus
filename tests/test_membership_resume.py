"""Offline, run-pinned membership selection resume regression tests."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from astro_pipeline import membership
from astro_pipeline.library.files import sha256_file
from astro_pipeline.library.store import LibraryStore

CATALOG = "J/AJ/160/44"
TABLE = f"{CATALOG}/table5"


def _published_selection(monkeypatch, tmp_path, *, legacy_epoch=True):
    import astro_pipeline.library as library

    store = LibraryStore(tmp_path / "library")
    corpus = tmp_path / "uppersco_full"
    original = pd.DataFrame({
        "membership_catalog_row": list(range(4)),
        "RAJ2000": [242.0, 242.1, 242.2, 242.3],
        "DEJ2000": [-22.0, -22.1, -22.2, -22.3],
        "Name": ["001", "002", "003", "004"],
    })
    full = pd.concat([original, pd.DataFrame({
        "membership_catalog_row": [4], "RAJ2000": [242.4],
        "DEJ2000": [-22.4], "Name": ["005"],
    })], ignore_index=True)
    schema = [
        {"name": "RAJ2000", "dtype": "float64", "unit": "deg", "description": "Right Ascension (J2000)"},
        {"name": "DEJ2000", "dtype": "float64", "unit": "deg", "description": "Declination (J2000)"},
        {"name": "Name", "dtype": "object", "unit": None, "description": ""},
    ]
    descriptor = {
        "ra_column": "RAJ2000", "dec_column": "DEJ2000", "ra_unit": "deg",
        "dec_unit": "deg", "ra_dtype": "float64", "dec_dtype": "float64",
        "match_unit": "deg", "frame": "equatorial", "reference_epoch": "J2000" if legacy_epoch else None,
        "epoch_source": "column_names" if legacy_epoch else "not_declared", "selection_reason": "numeric columns with explicit angular units",
    }
    members = pd.DataFrame([
        {
            "Gaia": source_id, "membership_catalog_row": index,
            "RAICRS": 242.0 + index / 10,
            "membership_match_sep_arcsec": 0.2 + index / 10,
            "membership_match_candidate_count": 2 if index == 1 else 1,
            **{f"member_{column}": value for column, value in original.iloc[index].items() if column != "membership_catalog_row"},
            "membership_match_method": "gaia_dr3_position",
            "membership_coordinate_epoch": descriptor["reference_epoch"],
            "membership_coordinate_epoch_source": descriptor["epoch_source"],
        }
        for index, source_id in ((0, "123"), (1, "123"), (3, "456"))
    ])
    unmatched = original.iloc[[2]].copy()
    unmatched["membership_match_status"] = "no_gaia_match"
    unmatched["membership_coordinate_epoch"] = descriptor["reference_epoch"]
    unmatched["membership_coordinate_epoch_source"] = descriptor["epoch_source"]
    resolution = membership.MembershipResolution(
        catalog_id=CATALOG, table_id=TABLE, table_description="Upper Sco members",
        catalog_title="Literature list", original=original, all_rows=full,
        members=members, unmatched=unmatched, match_method="gaia_dr3_position",
        match_radius_arcsec=3.0, table_schema=schema,
        coordinate_descriptor=descriptor,
    )
    monkeypatch.setattr(library, "get_library", lambda: store)
    monkeypatch.setattr(membership, "resolve_membership_catalog", lambda *a, **k: resolution)
    source = membership.build_membership_corpus(
        label="uppersco_full", out_dir=corpus, catalog_id=CATALOG,
        table_id=TABLE, limit=4, skip_simbad=True, static_viz_mode="off",
    )
    assert source["n_gaia_matched"] == 3
    assert source["n_unmatched"] == 1
    return store, corpus, source["run_id"]


def _resume(corpus, source_run_id, **options):
    kwargs = {
        "label": "uppersco_full", "out_dir": corpus, "catalog_id": CATALOG,
        "table_id": TABLE, "limit": 4, "match_radius_arcsec": 3.0,
        "skip_simbad": True, "static_viz_mode": "off",
        "resume_selection_run_id": source_run_id,
    }
    kwargs.update(options)
    return membership.build_membership_corpus(**kwargs)


def test_resume_selection_has_no_selection_network_and_preserves_provenance(monkeypatch, tmp_path):
    store, corpus, source_run_id = _published_selection(monkeypatch, tmp_path)
    source_dir = corpus / "runs" / source_run_id
    before = {p.relative_to(source_dir): sha256_file(p) for p in source_dir.rglob("*") if p.is_file()}

    def network_forbidden(*args, **kwargs):
        raise AssertionError("selection must not contact VizieR or Gaia")

    monkeypatch.setattr(membership, "resolve_membership_catalog", network_forbidden)
    monkeypatch.setattr(membership, "GaiaSource", network_forbidden)
    monkeypatch.setattr(membership.Vizier, "get_catalogs", network_forbidden)
    output = _resume(corpus, source_run_id)
    new_dir = corpus / "runs" / output["run_id"]
    assert source_run_id != output["run_id"]
    assert (output["n_catalog_rows"], output["n_catalog_total_rows"], output["n_gaia_matched"], output["n_unmatched"]) == (4, 5, 3, 1)
    assert {p.relative_to(source_dir): sha256_file(p) for p in source_dir.rglob("*") if p.is_file()} == before
    assert sha256_file(new_dir / "membership_catalog_full.parquet") == before[Path("membership_catalog_full.parquet")]
    assert sha256_file(new_dir / "membership_catalog.parquet") == before[Path("membership_catalog.parquet")]
    new = pd.read_parquet(new_dir / "members.parquet")
    assert new["Gaia"].tolist() == ["123", "123", "456"]
    assert new["input_row_id"].tolist() == [0, 1, 3]
    assert new["member_Name"].tolist() == ["001", "002", "004"]
    assert new["membership_coordinate_epoch"].isna().all()
    assert new["membership_coordinate_epoch_source"].tolist() == ["not_declared"] * 3
    unmatched = pd.read_csv(new_dir / "unmatched_members.csv", dtype=str)
    assert unmatched["membership_catalog_row"].tolist() == ["2"]
    assert unmatched["Name"].tolist() == ["003"]
    assert unmatched["membership_coordinate_epoch_source"].tolist() == ["not_declared"]
    assert pd.read_parquet(new_dir / "input_rows.parquet")["status"].tolist() == [
        "matched", "matched", "unmatched", "matched", "not_selected_by_limit",
    ]
    assert pd.read_parquet(new_dir / "input_matches.parquet")["source_id"].tolist()[0:2] == ["123", "123"]
    manifest = json.loads((new_dir / "manifest.json").read_text())
    resolved = manifest["resolved_input"]
    assert resolved["selection_resume"]["source_run_id"] == source_run_id
    assert resolved["coordinate_descriptor"]["reference_epoch"] is None
    assert resolved["selection_resume"]["inherited_resolved_input"]["coordinate_descriptor"]["reference_epoch"] == "J2000"
    assert resolved["table_content_hash"] == json.loads((source_dir / "manifest.json").read_text())["resolved_input"]["table_content_hash"]
    indexed = store.read("SELECT input_row_id, source_id FROM sample_members WHERE run_id=? ORDER BY member_ordinal", (output["run_id"],))
    assert indexed == [
        {"input_row_id": "0", "source_id": "123"},
        {"input_row_id": "1", "source_id": "123"},
        {"input_row_id": "3", "source_id": "456"},
    ]
    assert len(store.read("SELECT * FROM input_rows WHERE run_id=?", (output["run_id"],))) == 5


def test_truthful_coordinate_descriptor_is_preserved(monkeypatch, tmp_path):
    _, corpus, source = _published_selection(monkeypatch, tmp_path, legacy_epoch=False)
    output = _resume(corpus, source)
    run = corpus / "runs" / output["run_id"]
    source_dir = corpus / "runs" / source
    assert sha256_file(run / "unmatched_members.csv") == sha256_file(source_dir / "unmatched_members.csv")
    metadata = json.loads((run / "manifest.json").read_text())["resolved_input"]
    assert metadata["coordinate_descriptor"]["epoch_source"] == "not_declared"
    assert metadata["selection_resume"]["coordinate_interpretation"] == (
        "verified against original table schema; unchanged"
    )


@pytest.mark.parametrize("changes", [
    {"catalog_id": "J/AJ/163/64"},
    {"table_id": "J/AJ/160/44/table6"},
    {"limit": 3},
    {"match_radius_arcsec": 2.0},
])
def test_resume_rejects_mismatched_selection_before_new_run(monkeypatch, tmp_path, changes):
    store, corpus, source = _published_selection(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="Cannot resume membership selection"):
        _resume(corpus, source, **changes)
    assert len(store.read("SELECT * FROM runs")) == 1


def test_resume_rejects_tampering_unpublished_and_ambiguous_refresh(monkeypatch, tmp_path):
    store, corpus, source = _published_selection(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="refresh and resume"):
        _resume(corpus, source, refresh=True)
    store.update_run(source, {"published": False})
    with pytest.raises(ValueError, match="not a finished, published"):
        _resume(corpus, source)
    store.update_run(source, {"published": True})
    artifact = corpus / "runs" / source / "membership_catalog_full.parquet"
    artifact.write_bytes(b"altered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        _resume(corpus, source)
    assert len(store.read("SELECT * FROM runs")) == 1


def test_resume_requires_shared_library_and_same_corpus(monkeypatch, tmp_path):
    import astro_pipeline.library as library

    store, corpus, source = _published_selection(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="another corpus"):
        _resume(tmp_path / "different", source)
    monkeypatch.setattr(library, "get_library", lambda: None)
    with pytest.raises(ValueError, match="requires the shared library"):
        _resume(corpus, source)


def test_resume_rejects_mismatched_original_table_hash(monkeypatch, tmp_path):
    store, corpus, source = _published_selection(monkeypatch, tmp_path)
    run_dir = corpus / "runs" / source
    path = run_dir / "membership_catalog_full.parquet"
    changed = pd.read_parquet(path)
    changed.loc[0, "Name"] = "different"
    changed.to_parquet(path, index=False)
    # A recomputed file checksum still cannot authorize a different VizieR table.
    with store.transaction() as connection:
        connection.execute("UPDATE run_artifacts SET sha256=? WHERE run_id=? AND path=?",
                           (sha256_file(path), source, str(path)))
        manifest_path = run_dir / "manifest.json"
        snapshot = json.loads(manifest_path.read_text())
        snapshot["artifacts"]["membership_catalog_full_parquet"]["sha256"] = sha256_file(path)
        manifest_path.write_text(json.dumps(snapshot))
        connection.execute("UPDATE run_artifacts SET sha256=? WHERE run_id=? AND path=?",
                           (sha256_file(manifest_path), source, str(manifest_path)))
    with pytest.raises(ValueError, match="original table content hash mismatch"):
        _resume(corpus, source)
    assert len(store.read("SELECT * FROM runs")) == 1
