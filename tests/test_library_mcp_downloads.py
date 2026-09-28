"""Shared-library defaults on public MCP download entry points."""
from astropy.table import Table

from astro_pipeline import mcp_server
from astro_pipeline.config import PipelineConfig
from astro_pipeline.library import get_library
from astro_pipeline.sources.gaia import GaiaSource


def test_generic_mcp_product_downloads_reuse_the_global_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(PipelineConfig, "load", lambda *a, **k: PipelineConfig())
    calls = []

    def load(self, **kwargs):
        calls.append(list(kwargs["ids"]))
        return {"data.fits": [Table({"source_id": [int(value) for value in kwargs["ids"]], "flux": [[1.0, 2.0]]})]}

    monkeypatch.setattr(GaiaSource, "_load_data", load)
    first = mcp_server.download_gaia_product(
        "first", "dr3", "XP_SAMPLED", ["100"], data_structure="RAW",
    )
    second = mcp_server.download_gaia_product(
        "second", "dr3", "XP_SAMPLED", ["100"], data_structure="RAW",
    )
    assert "error" not in first and "error" not in second
    assert calls == [["100"]]
    assert first["library_run_id"] != second["library_run_id"]
    assert second["storage_mode"] == "shared_library"
    assert second["n_ready_identifiers"] == 1
    assert get_library().status()["n_sources"] == 1
    assert get_library().status()["n_ready_products"] == 1
    assert list((tmp_path / "data").rglob("*.fits")) == []
    result = mcp_server.lookup_library_source("100")
    assert result["resolved"] is True
    assert len(result["active_products"]) == 1


def test_generic_mcp_conflicting_label_does_not_start_a_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(PipelineConfig, "load", lambda *a, **k: PipelineConfig())
    monkeypatch.setattr(
        GaiaSource, "_load_data",
        lambda self, **kwargs: {"data.fits": [Table({"source_id": [100]})]},
    )
    first = mcp_server.download_gaia_product("same", "dr3", "XP_SAMPLED", ["100"])
    assert "error" not in first
    before = get_library().status()["n_runs"]
    changed = mcp_server.download_gaia_product("same", "dr3", "XP_CONTINUOUS", ["100"])
    assert "different request contract" in changed["error"]
    assert get_library().status()["n_runs"] == before
