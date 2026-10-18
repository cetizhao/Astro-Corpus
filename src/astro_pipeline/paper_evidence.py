"""Conservative, versioned source mentions in supplied scientific HTML.

This is evidence extraction, not physical identity resolution or a claim that
the supplied HTML represents the entire published article. No remote I/O occurs.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

PARSER_VERSION = "html-evidence-1"
NAMESPACE = "paper-evidence-v1"
KINDS = ("title", "abstract", "body", "figure_caption", "table", "vizier")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold()
    return re.sub(r"\s+", " ", text.translate(str.maketrans("‐‑–−", "----"))).strip()


def _aliases(values: list[str]) -> list[str]:
    # Numeric/local labels need contextual identity resolution, not global search.
    result = sorted({v.strip() for v in values if v.strip()})
    if not result or any(len(v) < 3 or not any(c.isalpha() for c in v) for v in result):
        raise ValueError("Provide nonnumeric, unambiguous SIMBAD identifiers (at least 3 characters)")
    return result


def _matches(text: str, aliases: list[str]) -> list[str]:
    normalized = _normalize(text)
    return [alias for alias in aliases if re.search(
        r"(?<!\w)" + r"\s*".join(re.escape(part) for part in _normalize(alias).split()) + r"(?!\w)", normalized
    )]


def analyze_paper(
    *, source_id: str, release: str, aliases: list[str], bibcode: str,
    html: str, document_url: str, paper_version: str = "unspecified",
    alias_provenance: str = "caller-supplied; identity not independently verified",
    vizier_tables: list[dict[str, Any]] | None = None,
) -> dict:
    """Extract all mentions, retaining supplied HTML and explicit coverage.

    VizieR inputs require bibcode, table_id, association_provenance, and rows
    (a list of column-name/value dictionaries). This function validates the
    declared bibcode but does not independently verify the publication link.
    """
    if release.lower() not in {"dr3", "dr4"} or not source_id.isdecimal():
        raise ValueError("Expected release dr3/dr4 and a numeric source_id")
    if not bibcode.strip() or not html.strip():
        raise ValueError("bibcode and nonempty article HTML are required")
    aliases = _aliases(aliases)
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, nav, header, footer"):
        node.decompose()
    root = soup.select_one("article, .ltx_document") or soup.body or soup
    digest = hashlib.sha256(html.encode()).hexdigest()
    evidence: list[dict] = []
    counts = dict.fromkeys(KINDS, 0)

    def add(kind, text, locator, **extra):
        counts[kind] += 1
        matched = _matches(text, aliases)
        if matched:
            evidence.append({"location_type": kind, "text": text,
                             "matched_aliases": matched, "locator": locator,
                             "match_method": "normalized_alias_boundary",
                             "identity_status": "alias_match_requires_identity_review",
                             **extra})

    def location(node, ordinal):
        anchor = node.get("id")
        return {"document_url": document_url, "element_id": anchor,
                "ordinal": ordinal, "document_sha256": digest,
                "url": document_url.split("#")[0] + "#" + quote(anchor) if anchor else None}

    title_node = root.select_one("h1, .ltx_title_document") or soup.title
    title = title_node.get_text(" ", strip=True) if title_node else ""
    if title_node:
        add("title", title, location(title_node, 1))
    abstract_nodes = root.select(".ltx_abstract, .abstract, section#abstract, div#abstract")
    abstract_nodes = [n for n in abstract_nodes if not any(p in abstract_nodes for p in n.parents)]
    abstract = "\n\n".join(n.get_text(" ", strip=True) for n in abstract_nodes)
    for i, node in enumerate(abstract_nodes, 1):
        add("abstract", node.get_text(" ", strip=True), location(node, i))

    def excluded(node):
        return any(p in abstract_nodes or p.name in {"table", "figure", "figcaption"}
                   or "ltx_bibliography" in p.get("class", [])
                   or p.get("id", "").lower() in {"references", "bibliography"}
                   for p in node.parents)

    for i, node in enumerate(root.find_all("p"), 1):
        if excluded(node):
            continue
        heading = node.find_previous(["h2", "h3", "h4", "h5", "h6"])
        add("body", node.get_text(" ", strip=True), location(node, i),
            section=heading.get_text(" ", strip=True) if heading else None,
            paragraph_id=f"{digest[:12]}:p{i}")
    for i, node in enumerate(root.find_all("figcaption"), 1):
        parent = node.find_parent("figure")
        kind = "table" if parent and (parent.find("table") or "ltx_table" in parent.get("class", [])) else "figure_caption"
        add(kind, node.get_text(" ", strip=True), location(node, i),
            container_id=parent.get("id") if parent else None, component="caption")
    for i, table in enumerate(root.find_all("table"), 1):
        table_id = table.get("id") or f"table-ordinal-{i}"
        caption = table.find("caption")
        if caption:
            add("table", caption.get_text(" ", strip=True), location(caption, i),
                table_id=table_id, component="caption")
        headers = [n.get_text(" ", strip=True) for n in table.find_all("th")]
        for row_index, row in enumerate(table.find_all("tr"), 1):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"], recursive=False)]
            if not cells:
                continue
            add("table", " | ".join(cells), location(row, row_index), table_id=table_id,
                row_index=row_index, cells=cells, headers=headers, component="row",
                structured_columns_complete=not bool(table.select("[rowspan], [colspan]")))

    for table in vizier_tables or []:
        if table.get("bibcode") != bibcode or not table.get("association_provenance") or not table.get("table_id"):
            raise ValueError("VizieR table requires matching bibcode, table_id and association_provenance")
        rows = table.get("rows")
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ValueError("VizieR rows must be a list of dictionaries")
        for i, row in enumerate(rows, 1):
            add("vizier", " | ".join(str(v) for v in row.values()),
                {"url": "https://vizier.cds.unistra.fr/viz-bin/VizieR?-source=" + quote(table["table_id"], safe=""),
                 "row_index": i}, table_id=table["table_id"], row=row,
                association_provenance=table["association_provenance"],
                association_status="caller_asserted")

    coverage = {kind: {"status": "checked_supplied_elements" if counts[kind] else "not_available",
                       "elements_checked": counts[kind]} for kind in KINDS}
    coverage["figure_text"] = {"status": "not_checked"}
    coverage["pdf"] = {"status": "not_checked"}
    groups = sorted({e["location_type"] for e in evidence})
    result = {"schema_version": 1, "parser_version": PARSER_VERSION,
              "source": {"release": release.lower(), "source_id": source_id},
              "aliases": aliases, "alias_provenance": alias_provenance,
              "paper": {"bibcode": bibcode, "version": paper_version, "title": title, "abstract": abstract},
              "document": {"url": document_url, "sha256": digest, "original_html": html},
              "vizier_inputs": vizier_tables or [], "evidence": evidence, "groups": groups,
              "coverage": coverage, "coverage_scope": "supplied materials only; article completeness unverified",
              "status": "mentions_found" if evidence else "no_match_in_supplied_materials"}
    result["analysis_id"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def save_analysis(store, result: dict) -> str:
    """Save a content-keyed analysis in the existing fenced SQLite cache."""
    key = result["analysis_id"]
    with store.hold_lease(NAMESPACE, key, ttl=store.lease_seconds,
                          wait_timeout=store.wait_timeout_seconds) as lease:
        store.put_cache(NAMESPACE, key, result, lease=lease,
                        ttl_seconds=store.positive_ttl_seconds)
    return key


def load_analysis(store, analysis_id: str) -> dict:
    record = store.get_cache(NAMESPACE, analysis_id, include_stale=True) if store else None
    if not record:
        return {"status": "not_found", "analysis_id": analysis_id}
    return {"status": "found", "stale": record["stale"], "analysis": record["payload"]}
