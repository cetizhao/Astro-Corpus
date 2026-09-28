"""End-to-end library builds with counted, offline archive substitutes."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from astropy.table import Table

from astro_pipeline import corpus_builder, intake
from astro_pipeline.config import PipelineConfig
from astro_pipeline.corpus import Corpus
from astro_pipeline.library import get_library
from astro_pipeline.library.files import sha256_file
from astro_pipeline.library.refs import resolve_product
from astro_pipeline.models.results import CatalogResult
from astro_pipeline.sources.gaia import GaiaSource


def members(ids):
    rows = []
    for source_id in ids:
        row = {name: float(int(source_id)) for name in intake.CANONICAL_RENAME.values() if name != "Gaia"}
        row.update({column: 1 for column in intake.QC_EXTRA_COLUMNS})
        row.update({"Gaia": str(source_id), "RAICRS": 10.0, "DEICRS": 20.0, "duplicated_source": False})
        rows.append(row)
    return pd.DataFrame(rows)


@pytest.fixture
def archives(monkeypatch):
    calls = {"ids": [], "cones": [], "availability": [], "products": []}
    monkeypatch.setattr(PipelineConfig, "load", lambda *a, **k: PipelineConfig())

    def fetch(ids):
        calls["ids"].append(list(ids))
        return members(ids)

    def cone(self, query, output_path=None):
        calls["cones"].append(query)
        frame = members(["222", "333"]).rename(columns={v: k for k, v in intake.CANONICAL_RENAME.items()})
        return CatalogResult("Gaia", frame, len(frame))

    def position(self, positions, *, row_id_col, **kwargs):
        frame = members(["222"]).rename(columns={v: k for k, v in intake.CANONICAL_RENAME.items()})
        frame[row_id_col] = positions[row_id_col].tolist()
        frame["match_sep_arcsec"] = 0.1
        frame["match_candidate_count"] = 1
        return frame

    def availability(self, ids, output_path=None):
        calls["availability"].append(list(ids))
        frame = pd.DataFrame({
            "source_id": ids, "gaia_source_found": True,
            "has_xp_sampled": True, "has_xp_continuous": False,
        })
        if output_path is not None:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(output_path, index=False)
        return CatalogResult("Gaia", frame, len(frame))

    def products(self, **kwargs):
        ids = list(kwargs["ids"])
        calls["products"].append(ids)
        return {"xp.fits": [Table({
            "source_id": [int(source_id) for source_id in ids],
            "flux": [[1.0, 2.0] for _ in ids],
            "flux_error": [[0.1, 0.1] for _ in ids],
        })]}

    monkeypatch.setattr(intake, "_fetch_gaia_by_ids", fetch)
    monkeypatch.setattr(GaiaSource, "query_catalog", cone)
    monkeypatch.setattr(GaiaSource, "crossmatch_positions", position)
    monkeypatch.setattr(GaiaSource, "query_xp_availability", availability)
    monkeypatch.setattr(GaiaSource, "_load_data", products)
    return calls


def test_overlapping_input_modes_share_entities_and_products(tmp_path, archives):
    source = tmp_path / "ids.csv"
    source.write_text("Gaia,note\n111,first\n222,second\n")
    a = corpus_builder.build_corpus_snapshot(
        label="a", out_dir=tmp_path / "a", catalog=source,
        skip_simbad=True, xp_sampled=True,
    )
    b = corpus_builder.build_corpus_snapshot(
        label="b", out_dir=tmp_path / "b", cone=(10.0, 20.0, 0.1),
        limit=2, skip_simbad=True, xp_sampled=True,
    )
    positions = tmp_path / "positions.csv"
    positions.write_text("RAICRS,DEICRS,label\n10,20,coordinate input\n")
    c = corpus_builder.build_corpus_snapshot(
        label="c", out_dir=tmp_path / "c", catalog=positions,
        skip_simbad=True, xp_sampled=True,
    )
    assert all(result.error is None for result in (a, b, c))
    assert all(result.library_indexed for result in (a, b, c))
    store = get_library()
    assert store.status()["n_sources"] == 3
    assert store.status()["n_corpora"] == 3
    assert store.status()["n_ready_products"] == 3
    assert sum(len(ids) for ids in archives["products"]) == 3
    assert sum(len(ids) for ids in archives["availability"]) == 3
    assert len(archives["cones"]) == 1
    paths = [resolve_product(tmp_path / label, "222", "XP_SAMPLED")["path"] for label in ("a", "b", "c")]
    assert len(set(paths)) == 1
    for label in ("a", "b", "c"):
        assert list((tmp_path / label).rglob("*.fits")) == []
    assert b.xp_products["sampled"]["shared_cache_hits"] == 1
    assert c.xp_products["sampled"]["shared_cache_hits"] == 1


def test_repeated_rows_and_new_runs_preserve_original_snapshots(tmp_path, archives):
    source = tmp_path / "ids.csv"
    original = b"Gaia,note\n111,first\n111,second\n222,third\n"
    source.write_bytes(original)
    target = tmp_path / "sample"
    first = corpus_builder.build_corpus_snapshot(label="sample", out_dir=target, catalog=source, skip_simbad=True)
    first_members = target / "runs" / first.run_id / "members.parquet"
    first_hash = sha256_file(first_members)
    frame = pd.read_parquet(first_members)
    assert frame["Gaia"].tolist() == ["111", "111", "222"]
    assert frame["input_row_id"].tolist() == [0, 1, 2]
    stored_inputs = pd.read_parquet(target / "runs" / first.run_id / "input_rows.parquet")
    assert [json.loads(value)["note"] for value in stored_inputs["raw_json"]] == ["first", "second", "third"]
    assert next((target / "runs" / first.run_id / "inputs").glob("*.csv")).read_bytes() == original
    second = corpus_builder.build_corpus_snapshot(label="sample", out_dir=target, catalog=source, skip_simbad=True)
    assert second.run_id != first.run_id
    assert sha256_file(first_members) == first_hash
    assert get_library().status()["n_sources"] == 2
    assert len(archives["ids"]) == 1
    corpus = Corpus(data_dir=target, catalog_path=target / "members.csv", offline=True)
    assert corpus.run_id == second.run_id
    assert corpus.summary()["unique_sources"] == 2
    assert corpus.summary()["catalog_sources"] == 3


def test_failed_new_selection_keeps_published_view(tmp_path, archives, monkeypatch):
    target = tmp_path / "cone"
    first = corpus_builder.build_corpus_snapshot(label="cone", out_dir=target, cone=(10, 20, 0.1), limit=2, skip_simbad=True)
    before = (target / "members.parquet").read_bytes()

    def unavailable(**kwargs):
        raise RuntimeError("archive unavailable")

    monkeypatch.setattr(corpus_builder, "resolve_members", unavailable)
    failed = corpus_builder.build_corpus_snapshot(label="cone", out_dir=target, cone=(10, 20, 0.1), limit=2, skip_simbad=True)
    assert failed.error == "archive unavailable"
    assert (target / "members.parquet").read_bytes() == before
    assert json.loads((target / "manifest.json").read_text())["active_run_id"] == first.run_id
    corpus = Corpus(data_dir=target, catalog_path=target / "members.csv", offline=True)
    assert corpus.run_id == first.run_id
    assert len(corpus.catalog) == 2


def test_legacy_directory_is_not_adopted_or_overwritten(tmp_path, archives):
    target = tmp_path / "old"
    target.mkdir()
    old = target / "members.csv"
    old.write_text("Gaia\n999\n")
    with pytest.raises(ValueError, match="no manifest"):
        corpus_builder.build_corpus_snapshot(label="old", out_dir=target, cone=(10, 20, 0.1), skip_simbad=True)
    assert old.read_text() == "Gaia\n999\n"
    assert not (target / "manifest.json").exists()
    assert not get_library().exists
    assert archives["cones"] == []


def test_contract_conflict_is_rejected_before_new_network_or_run(tmp_path, archives):
    target = tmp_path / "field"
    corpus_builder.build_corpus_snapshot(label="field", out_dir=target, cone=(10, 20, 0.1), limit=2, skip_simbad=True)
    before = get_library().status()["n_runs"]
    with pytest.raises(ValueError, match="different input"):
        corpus_builder.build_corpus_snapshot(label="field", out_dir=target, cone=(11, 20, 0.1), limit=2, skip_simbad=True)
    assert get_library().status()["n_runs"] == before
    assert len(archives["cones"]) == 1
