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
    } <= names
    assert len(names) == 16


def test_cached_lookup_has_structured_fresh_clone_fallback(monkeypatch):
    monkeypatch.setattr(mcp_server, "corpus", None)
    result = mcp_server.lookup_source("123")
    assert result["error"] == "default cached corpus is not installed"
    assert "download_corpus" in result["next_step"]


def test_gaia_xp_label_is_validated():
    result = mcp_server.gaia_xp_spectrum("123", label="../outside")
    assert "path separator" in result["error"]
