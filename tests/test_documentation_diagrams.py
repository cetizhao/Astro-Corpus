"""Offline checks for editable architecture sources and their generated artifacts."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("render_diagrams_test", ROOT / "scripts/render_diagrams.py")
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


def test_committed_diagrams_are_synchronized_and_use_native_svg_text():
    assert renderer.check_diagrams() == []
    for name in renderer.DIAGRAMS:
        svg = (ROOT / "docs/diagrams" / f"{name}.svg").read_text(encoding="utf-8")
        assert "foreignObject" not in svg
        assert "<text" in svg


def test_source_sync_preserves_surrounding_document():
    name = "system-overview"
    before = "prefix\n" + renderer.source_block(name, "flowchart TB\n A --> B\n") + "\nsuffix\n"
    after = renderer.sync_document(before, name, "flowchart TB\n A --> C\n")
    assert after.startswith("prefix\n") and after.endswith("\nsuffix\n")
    assert "A --> C" in after and "A --> B" not in after
    assert renderer.sync_document(after, name, "flowchart TB\n A --> C\n") == after


def test_missing_or_duplicate_source_marker_is_rejected():
    with pytest.raises(ValueError, match="exactly one"):
        renderer.sync_document("no marker", "system-overview", "flowchart TB")
    twice = renderer.source_block("system-overview", "flowchart TB") * 2
    with pytest.raises(ValueError, match="exactly one"):
        renderer.sync_document(twice, "system-overview", "flowchart TB")


def test_check_is_offline_and_reports_stale_sources(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    diagrams = docs / "diagrams"
    diagrams.mkdir(parents=True)
    blocks = []
    for name in renderer.DIAGRAMS:
        source = "flowchart TB\n A --> B\n"
        blocks.append(renderer.source_block(name, source))
        (diagrams / f"{name}.mmd").write_text(source)
        (diagrams / f"{name}.svg").write_text(
            renderer.source_stamp(name, source) + '\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        )
    (docs / "architecture.md").write_text("\n".join(blocks))
    monkeypatch.setattr(renderer.subprocess, "run", lambda *a, **k: pytest.fail("offline check invoked a renderer"))
    assert renderer.check_diagrams(tmp_path) == []
    source_path = diagrams / "system-overview.mmd"
    source_path.write_text(source_path.read_text().replace("B", "C"))
    errors = renderer.check_diagrams(tmp_path)
    assert any("Markdown source is out of sync" in error for error in errors)
    assert any("SVG is missing or stale" in error for error in errors)


def test_readme_is_an_entry_point_and_uses_the_canonical_overview():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert 150 <= len(readme.splitlines()) <= 250
    assert "docs/diagrams/system-overview.svg" in readme
    assert "docs/diagrams/system-overview.mmd" in readme
    assert "README §13" not in (ROOT / "src/astro_pipeline/mcp_server.py").read_text(encoding="utf-8")
