"""Offline tests for optional XP products and corpus manifests."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from astro_pipeline import corpus_products
from astro_pipeline.corpus_builder import build_corpus_from_members
from astro_pipeline.manifest import CorpusManifest, make_download_plan, read_manifest
from astro_pipeline.models.results import CatalogResult, SpectrumResult


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
            calls.append((retrieval_type, list(ids)))
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

    assert calls == [("XP_SAMPLED", ["100"]), ("XP_CONTINUOUS", ["200"])]
    assert result["sampled"]["cached"] == 1
    assert result["continuous"]["cached"] == 1
    assert result["sampled"]["status"] == "completed"
    assert (tmp_path / "catalogs" / "gaia_xp_availability.parquet").exists()
    assert any(stage == "gaia_xp_sampled" for stage, _ in stages)


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
    assert manifest["stages"]["members"]["completed"] == 2
    assert manifest["stages"]["simbad_papers"]["status"] == "skipped"
    assert manifest["stages"]["gaia_xp_sampled"]["status"] == "not_requested"


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
