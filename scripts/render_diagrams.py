#!/usr/bin/env python3
"""Render editable Mermaid diagrams locally and synchronize their Markdown sources.

``--check`` checks committed files only; it does not start a browser or contact npm.
Rendering uses a pinned documentation tool, not an application dependency.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Sequence
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
MERMAID_CLI_VERSION = "12.0.0"
DIAGRAMS = ("system-overview", "sample-selection", "paper-fulltext")


def source_block(name: str, source: str) -> str:
    return (
        f"<!-- diagram-source:{name}:start -->\n"
        "```mermaid\n" + source.rstrip() + "\n```\n"
        f"<!-- diagram-source:{name}:end -->"
    )


def source_stamp(name: str, source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"<!-- diagram:{name} source-sha256:{digest} mermaid-cli:{MERMAID_CLI_VERSION} -->"


def sync_document(document: str, name: str, source: str) -> str:
    start = f"<!-- diagram-source:{name}:start -->"
    end = f"<!-- diagram-source:{name}:end -->"
    if document.count(start) != 1 or document.count(end) != 1:
        raise ValueError(f"architecture.md needs exactly one source marker pair for {name}")
    left = document.index(start)
    right = document.index(end) + len(end)
    if right <= left:
        raise ValueError(f"invalid marker order for {name}")
    return document[:left] + source_block(name, source) + document[right:]


def check_diagrams(root: Path = ROOT) -> list[str]:
    errors = []
    document = (root / "docs/architecture.md").read_text(encoding="utf-8")
    for name in DIAGRAMS:
        source_path = root / "docs/diagrams" / f"{name}.mmd"
        svg_path = source_path.with_suffix(".svg")
        try:
            source = source_path.read_text(encoding="utf-8")
            if sync_document(document, name, source) != document:
                errors.append(f"{name}: Markdown source is out of sync")
            svg = svg_path.read_text(encoding="utf-8")
            if source_stamp(name, source) not in svg:
                errors.append(f"{name}: SVG is missing or stale; regenerate it")
            if ET.fromstring(svg).tag != "{http://www.w3.org/2000/svg}svg":
                errors.append(f"{name}: output is not an SVG document")
        except (OSError, ValueError, ET.ParseError) as exc:
            errors.append(f"{name}: {exc}")
    return errors


def browser_path(explicit: str | None) -> str:
    candidates = [explicit, os.environ.get("PUPPETEER_EXECUTABLE_PATH"),
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  shutil.which("google-chrome"), shutil.which("chromium"),
                  shutil.which("chromium-browser"), shutil.which("msedge")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise ValueError("a local Chromium-based browser is required; pass --browser or PUPPETEER_EXECUTABLE_PATH")


def render_diagrams(names: Sequence[str], *, browser: str | None = None, root: Path = ROOT) -> None:
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("rendering requires Node.js/npm; --check does not")
    executable = browser_path(browser)
    document_path = root / "docs/architecture.md"
    document = document_path.read_text(encoding="utf-8")
    env = {**os.environ, "PUPPETEER_SKIP_DOWNLOAD": "true"}
    with tempfile.TemporaryDirectory(prefix="astro-diagrams-") as temporary:
        temporary = Path(temporary)
        config = temporary / "puppeteer.json"
        config.write_text(json.dumps({"executablePath": executable}), encoding="utf-8")
        prepared = []
        for name in names:
            source_path = root / "docs/diagrams" / f"{name}.mmd"
            output_path = source_path.with_suffix(".svg")
            if output_path.exists() and f"<!-- diagram:{name} " not in output_path.read_text(encoding="utf-8"):
                raise ValueError(f"refusing to replace an unmanaged diagram: {output_path}")
            source = source_path.read_text(encoding="utf-8")
            document = sync_document(document, name, source)
            rendered = temporary / f"{name}.svg"
            subprocess.run(
                [npm, "exec", "--yes", "--prefer-offline", "--package",
                 f"@mermaid-js/mermaid-cli@{MERMAID_CLI_VERSION}", "--", "mmdc",
                 "--input", str(source_path), "--output", str(rendered),
                 "--puppeteerConfigFile", str(config), "--backgroundColor", "white",
                 "--no-font-embed", "--size", "1400", "--svgId", name],
                env=env, check=True, timeout=180,
            )
            svg = rendered.read_text(encoding="utf-8")
            ET.fromstring(svg)
            prepared.append((output_path, source_stamp(name, source) + "\n" + svg))
        for output_path, svg in prepared:
            output_path.write_text(svg, encoding="utf-8")
        document_path.write_text(document, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="offline source/SVG synchronization checks")
    parser.add_argument("--diagram", choices=DIAGRAMS, action="append", help="render only the named diagram")
    parser.add_argument("--browser", help="local Chrome/Chromium executable; never downloads a browser")
    args = parser.parse_args(argv)
    if args.check and (args.diagram or args.browser):
        parser.error("--check cannot be combined with renderer options")
    try:
        if args.check:
            errors = check_diagrams()
            for error in errors:
                print(error)
            if errors:
                return 1
            print("All diagram sources, Markdown blocks and SVG stamps are synchronized.")
        else:
            render_diagrams(args.diagram or DIAGRAMS, browser=args.browser)
            print("Rendered diagrams and synchronized docs/architecture.md.")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Diagram generation failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
