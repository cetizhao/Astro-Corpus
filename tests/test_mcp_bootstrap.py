"""MCP registration and fresh-clone fallback tests."""
from __future__ import annotations

import asyncio

from astro_pipeline import mcp_server


def test_mcp_registers_expected_tools():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "download_corpus",
        "download_plan",
        "search_membership_catalogs",
        "preview_membership_catalog",
        "download_membership_corpus",
        "corpus_status",
        "create_gaia_bulk_plan",
        "run_gaia_bulk_download",
        "gaia_bulk_status",
        "probe_gaia_archive",
        "describe_gaia_table",
        "list_gaia_products",
        "plan_gaia_product_download",
        "download_gaia_product",
        "gaia_product_status",
        "lookup_library_source",
        "lookup_library_paper",
        "download_paper_corpus",
        "paper_corpus_status",
        "library_status",
    } <= names
    assert len(names) == 27


def test_cached_lookup_has_structured_fresh_clone_fallback(monkeypatch):
    monkeypatch.setattr(mcp_server, "corpus", None)
    result = mcp_server.lookup_source("123")
    assert result["error"] == "default cached corpus is not installed"
    assert "download_corpus" in result["next_step"]


def test_gaia_xp_label_is_validated():
    result = mcp_server.gaia_xp_spectrum("123", label="../outside")
    assert "path separator" in result["error"]


def test_dr4_product_plan_is_available_without_live_archive():
    result = mcp_server.plan_gaia_product_download(
        release="dr4",
        retrieval_type="EPOCH_ASTROMETRY",
        n_identifiers=12,
        batch_size=5,
    )
    assert result["writes_data"] is False
    assert result["n_batches"] == 3
    assert result["profile_status"] == "provisional"


def test_dr4_bulk_mcp_refuses_unconfirmed_partition():
    result = mcp_server.create_gaia_bulk_plan(
        label="future_dr4",
        release="dr4",
        schema="gaiadr4",
        healpix_level=12,
        pixel_start=0,
        pixel_stop=1,
    )
    assert "not confirmed" in result["error"]
