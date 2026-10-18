"""Read-only bridge from source bibliography to existing structured paper blobs."""
import json

from astro_pipeline.paper_evidence import _matches
from astro_pipeline.library.paper_corpus import lookup_paper_text


def source_paper_evidence(store, detail):
    source = detail.get("source", {})
    bibliography = detail.get("active_bibliography") or {}
    attrs = source.get("attributes", {})
    aliases = {f"Gaia {source.get('gaia_release', 'dr3').upper()} {source.get('source_id', '')}"}
    for name in [bibliography.get("main_id"), attrs.get("main_id"), attrs.get("simbad_main_id"), attrs.get("Name")]:
        if isinstance(name, str) and len(name.strip()) >= 3:
            aliases.add(name.strip())
    for name in attrs.get("aliases", []) if isinstance(attrs.get("aliases"), list) else []:
        if isinstance(name, str) and len(name.strip()) >= 3:
            aliases.add(name.strip())
    aliases = sorted(aliases)
    results = []
    papers = bibliography.get("papers", [])
    for paper in papers[:100]:
        item = {"paper": paper, "evidence": [], "groups": [], "status": "fulltext_not_cached"}
        results.append(item)
        query = paper.get("arxiv_id") or paper.get("bibcode")
        if not query:
            continue
        try:
            versions = lookup_paper_text(store, query, limit=1).get("versions", [])
            if not versions:
                continue
            version = versions[0]
            artifact = next(a for a in version["artifacts"] if a["kind"] == "json")
            path = store.blobs.verify(artifact)
            if path.stat().st_size > 20_000_000:
                raise ValueError("structured paper exceeds viewer size limit")
            doc = json.loads(path.read_text(encoding="utf-8"))
            item["version_id"] = version["version_id"]
            item["quality"] = doc.get("quality", {})
            item["abstract"] = "\n".join(b.get("text", "") for b in doc["blocks"] if b.get("type") == "abstract" or any(str(s).lower() == "abstract" for s in b.get("section_path", [])))
            blocks = [{"type": "title", "text": doc.get("paper", {}).get("title") or paper.get("title", "")}] + doc["blocks"]
            for block in blocks:
                kind = block.get("type")
                if kind in {"reference", "heading", "equation"}:
                    continue
                category = "body" if kind in {"paragraph", "text"} else kind
                if any(str(s).lower() == "abstract" for s in block.get("section_path", [])):
                    category = "abstract"
                candidates = [(block.get("text", ""), None)]
                if kind == "table":
                    candidates += [(" | ".join(c.get("text", "") for c in row), i) for i, row in enumerate(block.get("cells", []), 1)]
                for text, row in candidates:
                    matched = _matches(text, aliases)
                    if matched:
                        item["evidence"].append({"category": category, "text": text,
                            "matched_aliases": matched, "block_id": block.get("id"),
                            "section_path": block.get("section_path", []),
                            "locator": block.get("locator", {}), "row": row})
            item["groups"] = sorted({e["category"] for e in item["evidence"] if e["category"]})
            item["status"] = "mentions_found" if item["evidence"] else "no_match_in_cached_text"
        except (ValueError, OSError, RuntimeError, StopIteration, KeyError) as exc:
            item["status"] = "unavailable_or_invalid_artifact"
            item["error"] = str(exc)
    return {"papers": results, "aliases": aliases, "truncated": len(papers) > 100,
            "coverage": "仅检查已缓存结构化全文和本地已知别名；不代表完整SIMBAD别名。图内文字和VizieR附表未检查。"}
