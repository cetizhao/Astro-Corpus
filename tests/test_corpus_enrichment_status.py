import json
from pathlib import Path

import pandas as pd
import pytest

from astro_pipeline import corpus_builder
import astro_pipeline.viz as viz


@pytest.mark.parametrize("mode", ["off", "status_only"])
@pytest.mark.parametrize("enrichment", ["completed", "partial"])
def test_enrichment_outcome_controls_terminal_corpus_status(monkeypatch, tmp_path, mode, enrichment):
    observation = {
        "schema_version": 1, "run_id": None, "status": enrichment,
        "metadata": {"status": "completed", "total": 1, "completed": 1, "failed": {}},
        "doi": {
            "status": "completed" if enrichment == "completed" else "rate_limited",
            "total": 1, "completed": 1 if enrichment == "completed" else 0,
            "failed": {}, "deferred": [] if enrichment == "completed" else ["10.1/a"],
        },
    }

    def papers(members, *, cache_path, **kwargs):
        refs = pd.DataFrame({
            "gaia_id": ["123"], "bibcode": ["2020fixture"], "title": ["A title"],
            "doi": ["10.1/a"], "arxiv_id": [""], "year": [2020],
        })
        refs.attrs["source_progress"] = {"completed": 1, "failed": 0, "remaining": 0}
        refs.attrs["enrichment_status"] = observation
        refs.to_parquet(cache_path, index=False)
        cache_path.with_suffix(".enrichment.json").write_text(json.dumps(observation))

        class Edges:
            def __len__(self):
                return 1

            def save(self, path):
                refs.rename(columns={"gaia_id": "source_id"}).to_parquet(path, index=False)

        return Edges(), refs

    def render(output_path, **kwargs):
        Path(output_path).write_text("<html></html>")
        return Path(output_path)

    monkeypatch.setattr(corpus_builder, "build_paper_edges", papers)
    monkeypatch.setattr(viz, "build_overview_html", render)
    result = corpus_builder.build_corpus_from_members(
        label="fixture", out_dir=tmp_path,
        members=pd.DataFrame({"Gaia": ["123"], "RAICRS": [10.], "DEICRS": [-20.]}),
        input_mode="catalog", input_summary="fixture", static_viz_mode=mode,
    )
    manifest = json.loads(Path(result.manifest_json).read_text())
    assert manifest["status"] == enrichment
    assert manifest["stages"]["simbad_papers"]["status"] == "completed"
    assert manifest["stages"]["paper_enrichment"]["status"] == enrichment
    assert manifest["stages"]["paper_enrichment"]["doi"]["status"] == observation["doi"]["status"]
