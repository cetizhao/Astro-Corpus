from pathlib import Path
import json
import os

import numpy as np
import pandas as pd
from astropy.table import Table

from astro_pipeline.library.files import BlobStore
from astro_pipeline.spectra import (
    XP_SAMPLED_WAVELENGTH_NM,
    get_xp_spectrum_png,
    read_xp_continuous_params,
    read_xp_sampled,
)
from astro_pipeline.viz.source_paper import (
    build_overview_html,
    compute_overview,
    render_html,
)
from scripts.export_jsonl import (
    _input_provenance_for_row,
    _load_input_provenance,
    to_record,
)


def _write_sampled_refs(
    corpus_dir: Path,
    library_root: Path,
    gaia_id: str,
    blob: dict,
    version_id: str,
) -> Path:
    product_dir = corpus_dir / "gaia_xp_sampled"
    product_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "2.0",
        "storage": {
            "kind": "shared_library",
            "library_root": str(library_root.resolve()),
            "relative_library_root": os.path.relpath(library_root, product_dir),
            "index_path": "index.sqlite3",
        },
        "contract": {
            "release": "dr3",
            "archive_tag": "Gaia DR3",
            "retrieval_type": "XP_SAMPLED",
            "data_structure": "INDIVIDUAL",
            "output_format": "fits",
            "linking_parameter": "SOURCE_ID",
            "valid_data": False,
        },
        "items": [
            {
                "identifier": gaia_id,
                "item_key": f"item-{gaia_id}",
                "status": "ready",
                "version_id": version_id,
                "artifacts": [
                    {
                        "role": "normalized",
                        "slot": "normalized:000",
                        "blob": blob,
                        "metadata": {"is_primary": True},
                        "verified_path": "/old/library/path.fits",
                    }
                ],
                "error": None,
            }
        ],
    }
    path = product_dir / "product_refs.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


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


def test_png_cache_invalidates_when_pinned_fits_hash_changes(
    monkeypatch, tmp_path: Path
) -> None:
    from astro_pipeline import spectra

    gaia_id = "1234567890123456789"
    corpus_dir = tmp_path / "data" / "new-label"
    library_root = tmp_path / "data" / "_library"
    first_source = tmp_path / "first.fits"
    first_source.write_bytes(b"first-fits-version")
    first_blob = BlobStore(library_root).publish(
        first_source,
        extension="fits",
        content_type="application/fits",
    ).to_dict()
    refs_path = _write_sampled_refs(
        corpus_dir,
        library_root,
        gaia_id,
        first_blob,
        "version-1",
    )
    renders: list[Path] = []

    monkeypatch.setattr(
        spectra,
        "read_xp_spectrum",
        lambda meta: {
            "format": "sampled",
            "source_id": gaia_id,
            "wavelength": [336.0, 338.0],
            "flux": [1.0, 2.0],
            "flux_error": [0.1, 0.1],
        },
    )

    def fake_render(spectrum, out_path, *, title=None):
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(f"render-{len(renders)}".encode())
        renders.append(out)
        return out

    monkeypatch.setattr(spectra, "render_xp_plot", fake_render)

    first = get_xp_spectrum_png(gaia_id, corpus_dir)
    cached = get_xp_spectrum_png(gaia_id, corpus_dir)

    assert first["cached"] is False
    assert cached["cached"] is True
    assert len(renders) == 1
    assert list(corpus_dir.rglob("*.fits")) == []

    second_source = tmp_path / "second.fits"
    second_source.write_bytes(b"second-fits-version")
    second_blob = BlobStore(library_root).publish(
        second_source,
        extension="fits",
        content_type="application/fits",
    ).to_dict()
    _write_sampled_refs(
        corpus_dir,
        library_root,
        gaia_id,
        second_blob,
        "version-2",
    )

    refreshed = get_xp_spectrum_png(gaia_id, corpus_dir)

    assert refreshed["cached"] is False
    assert len(renders) == 2
    metadata_path = corpus_dir / "gaia_xp_png" / f"{gaia_id}.png.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["source_sha256"] == second_blob["sha256"]
    assert metadata["source_version_id"] == "version-2"
    assert refs_path.is_file()


def test_duplicate_export_rows_keep_distinct_sidecar_metadata(tmp_path: Path) -> None:
    data_dir = tmp_path / "corpus"
    data_dir.mkdir()
    pd.DataFrame(
        {
            "input_row_id": ["row-0", "row-1"],
            "run_id": ["run-a", "run-a"],
            "status": ["matched", "matched"],
            "raw_json": [
                json.dumps({"Gaia": "123", "user_label": "first"}),
                json.dumps({"Gaia": "123", "user_label": "second"}),
            ],
        }
    ).to_parquet(data_dir / "input_rows.parquet", index=False)
    catalog = pd.DataFrame(
        {
            "Gaia": ["123", "123"],
            "input_row_id": ["row-0", "row-1"],
            "run_id": ["run-a", "run-a"],
            "RAICRS": [10.0, 10.0],
        }
    )
    sidecar = _load_input_provenance(data_dir)
    lookup = {
        "gaia_id": "123",
        "gaia_release": "dr3",
        "astrometry": {"RAICRS": 10.0, "DEICRS": -20.0},
        "multimodal": {},
        "papers": [],
    }

    records = []
    for _, row in catalog.iterrows():
        provenance = _input_provenance_for_row(row, sidecar)
        raw = provenance["raw_input_row"]
        records.append(
            to_record(
                lookup,
                {**raw, **row.to_dict()},
                input_provenance=provenance,
                xp_sampled={"wavelength": [336.0]},
                xp_sampled_path="/shared/objects/pinned.fits",
            )
        )

    assert [record["provenance"]["input_row_id"] for record in records] == [
        "row-0",
        "row-1",
    ]
    assert [record["provenance"]["raw_input_row"]["user_label"] for record in records] == [
        "first",
        "second",
    ]
    assert records[0]["multimodal"]["gaia_xp"]["sampled"]["path"] == (
        "/shared/objects/pinned.fits"
    )


def test_export_main_uses_corpus_selected_snapshot_rows(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import export_jsonl

    corpus_root = tmp_path / "corpus"
    snapshot_dir = corpus_root / "runs" / "active-run"
    snapshot_dir.mkdir(parents=True)
    requested_catalog = corpus_root / "members.csv"
    output_path = tmp_path / "export.jsonl"
    selected_rows = pd.DataFrame(
        {
            "Gaia": ["123", "123"],
            "gaia_release": ["dr3", "dr3"],
            "input_row_id": ["row-0", "row-1"],
            "RAICRS": [10.0, 10.0],
        }
    )
    calls: dict[str, list[Path]] = {"sidecar": [], "products": []}

    class FakeCorpus:
        def __init__(self, *, data_dir, catalog_path, offline):
            assert Path(data_dir) == corpus_root
            assert Path(catalog_path) == requested_catalog
            assert offline is True
            self.catalog = selected_rows
            self.data_dir = snapshot_dir
            self.catalog_path = snapshot_dir / "members.parquet"

        def lookup_source(self, gaia_id):
            return {
                "resolved": True,
                "gaia_id": str(gaia_id),
                "gaia_release": "dr3",
                "astrometry": {"RAICRS": 10.0, "DEICRS": -20.0},
                "multimodal": {},
                "papers": [],
            }

    def fake_sidecar(data_dir):
        calls["sidecar"].append(Path(data_dir))
        return {}

    def fake_product(data_dir, gaia_id, release, retrieval_type, reader):
        calls["products"].append(Path(data_dir))
        return None, None

    monkeypatch.setattr(export_jsonl, "Corpus", FakeCorpus)
    monkeypatch.setattr(export_jsonl, "_load_input_provenance", fake_sidecar)
    monkeypatch.setattr(export_jsonl, "_read_source_product", fake_product)
    monkeypatch.setattr(
        export_jsonl.sys,
        "argv",
        [
            "export_jsonl.py",
            "--catalog",
            str(requested_catalog),
            "--data-dir",
            str(corpus_root),
            "--output",
            str(output_path),
        ],
    )

    export_jsonl.main()

    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert [record["provenance"]["input_row_id"] for record in records] == [
        "row-0",
        "row-1",
    ]
    assert calls["sidecar"] == [snapshot_dir]
    assert calls["products"] == [snapshot_dir, snapshot_dir]


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
    assert record["gaia_release"] == "dr3"
    assert record["provenance"]["gaia_release"] == "dr3"
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


def test_overview_html_reads_shared_spectrum_without_corpus_fits_copy(
    tmp_path: Path,
) -> None:
    gaia_id = "987654321"
    data_root = tmp_path / "data"
    corpus_dir = data_root / "new-label"
    library_root = data_root / "_library"
    corpus_dir.mkdir(parents=True)
    catalog_path = corpus_dir / "members.csv"
    edges_path = corpus_dir / "paper_edges.parquet"
    output_path = corpus_dir / "source_paper_viz.html"
    pd.DataFrame(
        {
            "Gaia": [gaia_id],
            "RAICRS": [10.0],
            "DEICRS": [-20.0],
            "Gmag": [12.3],
        }
    ).to_csv(catalog_path, index=False)
    pd.DataFrame(columns=["source_id", "bibcode"]).to_parquet(
        edges_path,
        index=False,
    )
    sampled_source = tmp_path / "sampled.fits"
    flux = np.asarray([1e-17, 2e-17], dtype="float32")
    Table(
        {
            "source_id": [int(gaia_id)],
            "wavelength": [np.asarray([336.0, 338.0])],
            "flux": [flux],
            "flux_error": [np.asarray([1e-19, 1e-19])],
        }
    ).write(sampled_source)
    blob = BlobStore(library_root).publish(
        sampled_source,
        extension="fits",
        content_type="application/fits",
    ).to_dict()
    _write_sampled_refs(corpus_dir, library_root, gaia_id, blob, "viz-version")

    build_overview_html(
        output_path,
        catalog_path=catalog_path,
        edges_path=edges_path,
        spectra_dir=corpus_dir,
        corpus_name="shared-test",
    )

    html = output_path.read_text(encoding="utf-8")
    assert '"has_xp":true' in html
    assert '"format":"sampled"' in html
    assert list(corpus_dir.rglob("*.fits")) == []


def test_vendored_echarts_asset_exists():
    asset = Path(__file__).parents[1] / "src" / "astro_pipeline" / "viz" / "vendor" / "echarts.min.js"
    text = asset.read_text(encoding="utf-8")
    assert "Apache Software Foundation" in text[:2000]
    assert 'version:"5.5.0"' in text


def test_overview_html_can_be_one_self_contained_file(tmp_path: Path) -> None:
    gaia_id = "123456789"
    catalog_path = tmp_path / "members.csv"
    edges_path = tmp_path / "paper_edges.parquet"
    output_path = tmp_path / "source_paper_viz.html"
    pd.DataFrame({
        "Gaia": [gaia_id],
        "RAICRS": [10.0],
        "DEICRS": [-20.0],
        "Gmag": [12.3],
        "plx": [10.0],
        "e_plx": [0.1],
        "pmRA": [1.0],
        "pmDE": [2.0],
        "RUWE": [1.1],
    }).to_csv(catalog_path, index=False)
    pd.DataFrame({
        "source_id": [gaia_id],
        "arxiv_id": ["2401.00001"],
        "bibcode": ["2024TEST"],
        "title": ["Test paper"],
        "doi": [""],
        "journal": ["Test"],
        "year": [2024],
    }).to_parquet(edges_path, index=False)

    build_overview_html(
        output_path,
        catalog_path=catalog_path,
        edges_path=edges_path,
        inline_echarts=True,
        corpus_name="test-corpus",
    )

    html = output_path.read_text(encoding="utf-8")
    assert '<script src="echarts.min.js"></script>' not in html
    assert 'version:"5.5.0"' in html
    assert "test-corpus Source-Paper Corpus" in html
