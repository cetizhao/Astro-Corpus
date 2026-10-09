"""Offline tests for optional XP products and corpus manifests."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from astro_pipeline import corpus_products
from astro_pipeline import corpus_builder
from astro_pipeline.corpus_builder import _describe_input, build_corpus_from_members
from astro_pipeline.manifest import CorpusManifest, make_download_plan, read_manifest
from astro_pipeline.models.paper import PaperEdgeTable
from astro_pipeline.models.results import CatalogResult, SpectrumResult
from astro_pipeline.config import PipelineConfig


def _members() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Gaia": ["100", "200"],
            "RAICRS": [10.0, 11.0],
            "DEICRS": [20.0, 21.0],
        }
    )


def test_download_gaia_xp_only_requests_available_ids(monkeypatch, tmp_path):
    calls = []

    class FakeGaia:
        def __init__(self, *args, **kwargs):
            pass

        def query_xp_availability(self, source_ids, output_path):
            frame = pd.DataFrame(
                {
                    "source_id": ["100", "200"],
                    "has_xp_sampled": [True, False],
                    "has_xp_continuous": [False, True],
                    "gaia_source_found": [True, True],
                }
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(output_path, index=False)
            return CatalogResult("Gaia", frame, len(frame), output_path=output_path)

        def download_spectra(
            self, ids, output_dir, *, retrieval_type, progress_callback, **kwargs
        ):
            calls.append((retrieval_type, list(ids), kwargs["batch_size"]))
            output_dir.mkdir(parents=True, exist_ok=True)
            results = []
            for source_id in ids:
                path = output_dir / f"{source_id}.fits"
                path.write_bytes(b"fits")
                results.append(SpectrumResult(source_id, path, True))
            progress_callback(
                {
                    "processed": len(ids),
                    "completed_this_run": len(ids),
                    "failed_this_run": 0,
                    "done": True,
                }
            )
            return results

    monkeypatch.setattr(corpus_products, "GaiaSource", FakeGaia)
    stages = []
    result = corpus_products.download_gaia_xp_products(
        _members(),
        tmp_path,
        sampled=True,
        continuous=True,
        progress_callback=lambda stage, data: stages.append((stage, data)),
    )

    assert calls == [("XP_SAMPLED", ["100"], 50), ("XP_CONTINUOUS", ["200"], 5)]
    assert result["sampled"]["cached"] == 1
    assert result["continuous"]["cached"] == 1
    assert result["sampled"]["status"] == "completed"
    assert (tmp_path / "catalogs" / "gaia_xp_availability.parquet").exists()
    assert any(stage == "gaia_xp_sampled" for stage, _ in stages)


def test_xp_availability_failure_blocks_each_requested_product(monkeypatch, tmp_path):
    class FailingGaia:
        def __init__(self, *args, **kwargs):
            pass

        def query_xp_availability(self, *args, **kwargs):
            raise RuntimeError("archive unavailable")

    monkeypatch.setattr(corpus_products, "GaiaSource", FailingGaia)
    stages = []
    result = corpus_products.download_gaia_xp_products(
        _members(),
        tmp_path,
        sampled=True,
        continuous=True,
        progress_callback=lambda stage, data: stages.append((stage, data)),
    )

    assert result["sampled"]["status"] == "blocked"
    assert result["continuous"]["status"] == "blocked"
    terminal = {stage: update["status"] for stage, update in stages}
    assert terminal["gaia_xp_sampled"] == "blocked"
    assert terminal["gaia_xp_continuous"] == "blocked"


def test_manifest_is_atomic_and_rejects_label_reuse_for_other_input(tmp_path):
    path = tmp_path / "manifest.json"
    manifest = CorpusManifest.open_or_create(
        path,
        label="demo",
        input_mode="cone",
        input_summary="cone(1,2,3)",
        requested_products={"members": True},
    )
    manifest.update_stage("members", status="completed", total=2, completed=2)
    manifest.finish("completed")

    data = read_manifest(path)
    assert data["status"] == "completed"
    assert data["stages"]["members"]["completed"] == 2
    assert not path.with_suffix(".json.tmp").exists()

    try:
        CorpusManifest.open_or_create(
            path,
            label="demo",
            input_mode="name",
            input_summary="name='other'",
            requested_products={"members": True},
        )
    except ValueError as exc:
        assert "different input" in str(exc)
    else:
        raise AssertionError("manifest accepted reuse for a different input")


def test_manifest_keeps_previously_requested_optional_products(tmp_path):
    path = tmp_path / "manifest.json"
    first = CorpusManifest.open_or_create(
        path,
        label="demo",
        input_mode="catalog",
        input_summary="same input",
        requested_products={"members": True, "gaia_xp_sampled": True},
    )
    first.update_stage("gaia_xp_sampled", status="partial", completed=1, total=2)
    first.finish("partial")

    second = CorpusManifest.open_or_create(
        path,
        label="demo",
        input_mode="catalog",
        input_summary="same input",
        requested_products={"members": True, "gaia_xp_sampled": False},
    )
    assert second.data["requested_products"]["gaia_xp_sampled"] is True
    assert second.data["stages"]["gaia_xp_sampled"]["status"] == "partial"


def test_manifest_release_is_part_of_corpus_identity(tmp_path):
    path = tmp_path / "manifest.json"
    manifest = CorpusManifest.open_or_create(
        path,
        label="release_identity",
        input_mode="catalog",
        input_summary="same ids",
        requested_products={"members": True},
        gaia_release="dr3",
    )
    manifest.finish("completed")
    with pytest.raises(ValueError, match="different input"):
        CorpusManifest.open_or_create(
            path,
            label="release_identity",
            input_mode="catalog",
            input_summary="same ids",
            requested_products={"members": True},
            gaia_release="dr4",
        )


def test_build_from_members_writes_completed_manifest_when_skipping_network(tmp_path):
    result = build_corpus_from_members(
        label="offline",
        out_dir=tmp_path,
        members=_members(),
        input_mode="catalog",
        input_summary="offline fixture",
        skip_simbad=True,
    )

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert result.error is None
    assert result.manifest_json == str((tmp_path / "manifest.json").resolve())
    assert manifest["status"] == "completed"
    assert manifest["stages"]["resolve_members"]["completed"] == 2
    assert manifest["stages"]["save_members"]["completed"] == 2
    assert manifest["stages"]["simbad_papers"]["status"] == "skipped"
    assert manifest["stages"]["gaia_xp_sampled"]["status"] == "not_requested"
    assert manifest["corpus"]["gaia_release"] == "dr3"
    members = pd.read_parquet(tmp_path / "members.parquet")
    assert members["gaia_release"].tolist() == ["dr3", "dr3"]
    assert result.gaia_release == "dr3"


def test_download_plan_is_read_only_and_explains_region_semantics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = make_download_plan(
        label="taurus_field",
        input_mode="region",
        input_summary="Taurus center, 2 deg",
        expected_sources=200,
        skip_simbad=False,
        xp_sampled=True,
        xp_continuous=False,
    )

    assert plan["writes_data"] is False
    assert plan["requested_products"]["gaia_xp_sampled"] is True
    assert any("not physical members" in caution for caution in plan["cautions"])
    assert not (tmp_path / "data").exists()


def test_dr4_corpus_plan_refuses_dr3_xp_product_names():
    with pytest.raises(ValueError, match="does not declare DR3-style XP"):
        make_download_plan(
            label="future",
            input_mode="catalog",
            input_summary="DR4 IDs",
            expected_sources=2,
            skip_simbad=True,
            xp_sampled=True,
            xp_continuous=False,
            gaia_release="dr4",
        )


def test_dr4_corpus_plan_is_read_only_and_not_executable():
    plan = make_download_plan(
        label="future",
        input_mode="catalog",
        input_summary="DR4 IDs",
        expected_sources=2,
        skip_simbad=True,
        xp_sampled=False,
        xp_continuous=False,
        gaia_release="dr4",
    )
    assert plan["writes_data"] is False
    assert plan["executable"] is False
    assert "probe_gaia_archive" in plan["next_step"]


def test_dr4_ordinary_corpus_build_fails_before_writing(monkeypatch, tmp_path):
    config = PipelineConfig()
    config.gaia = config.gaia.model_copy(update={"release": "dr4", "db_schema": None})
    monkeypatch.setattr(corpus_builder.PipelineConfig, "load", lambda: config)

    with pytest.raises(NotImplementedError, match="ordinary corpus resolution"):
        corpus_builder.build_corpus_snapshot(
            label="future",
            out_dir=tmp_path / "future",
            name="TW Hya",
        )
    assert not (tmp_path / "future").exists()


def test_corpus_label_cannot_escape_output_directory(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="label"):
        build_corpus_from_members(
            label="../outside",
            out_dir=tmp_path / "safe",
            members=_members(),
            input_mode="catalog",
            input_summary="unsafe label",
            skip_simbad=True,
        )


def test_input_identity_includes_limit_and_uploaded_catalog_content(tmp_path):
    catalog = tmp_path / "members.csv"
    catalog.write_text("Gaia\n100\n", encoding="utf-8")
    first = _describe_input(
        cone=None,
        name=None,
        region=None,
        catalog=catalog,
        limit=3,
    )
    different_limit = _describe_input(
        cone=None,
        name=None,
        region=None,
        catalog=catalog,
        limit=200,
    )
    catalog.write_text("Gaia\n200\n", encoding="utf-8")
    different_content = _describe_input(
        cone=None,
        name=None,
        region=None,
        catalog=catalog,
        limit=3,
    )

    assert "limit=3" in first
    assert first != different_limit
    assert first != different_content


def test_simbad_source_failures_make_corpus_partial(monkeypatch, tmp_path):
    def fake_edges(*args, **kwargs):
        edges = PaperEdgeTable()
        edges.df = pd.DataFrame(
            columns=[
                "source_id",
                "arxiv_id",
                "bibcode",
                "title",
                "year",
                "journal",
                "doi",
                "main_id",
            ]
        )
        refs = pd.DataFrame(
            columns=[
                "gaia_id",
                "main_id",
                "bibcode",
                "arxiv_id",
                "title",
                "year",
                "journal",
                "doi",
            ]
        )
        refs.attrs["source_progress"] = {
            "total": 2,
            "completed": 1,
            "failed": 1,
            "remaining": 1,
        }
        return edges, refs

    monkeypatch.setattr(corpus_builder, "build_paper_edges", fake_edges)
    result = build_corpus_from_members(
        label="partial_refs",
        out_dir=tmp_path,
        members=_members(),
        input_mode="catalog",
        input_summary="offline fixture",
        skip_simbad=False,
        enrich=False,
    )

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert result.error is None
    assert manifest["status"] == "partial"
    assert manifest["stages"]["simbad_papers"]["status"] == "partial"
    assert manifest["stages"]["simbad_papers"]["failed"] == 1
