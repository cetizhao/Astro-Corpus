"""Version-pinned evidence snapshots for standalone HTML, without network access."""
from __future__ import annotations

from datetime import datetime, timezone
import html
import json
from pathlib import Path

from astro_pipeline.library.evidence import _has_table, get_paper_evidence_context, query_source_evidence
from astro_pipeline.library.files import atomic_file
from astro_pipeline.library.identity import source_identity
from astro_pipeline.library.overview import RevisionMonitor
from astro_pipeline.library.store import LibraryStore


def evidence_snapshot(store: LibraryStore | None, targets: list[tuple[str, str]]) -> dict:
    """Embed all assessment history and cited blocks for exactly these identities."""
    monitor = RevisionMonitor(store)
    try:
        revision, _ = monitor.require(None)
        sources, contexts = {}, {}
        recorded = {(r["release"], r["source_id"]) for r in store.read(
            "SELECT DISTINCT release, source_id FROM paper_annotations"
        )} if _has_table(store, "paper_annotations") else set()
        for release, source_id in dict.fromkeys(source_identity(*t) for t in targets):
            if (release, source_id) not in recorded:
                sources[f"{release}:{source_id}"] = []
                continue
            records, offset = [], 0
            while True:
                page = query_source_evidence(store, source_id, release=release, limit=100, offset=offset)
                records.extend(page["assessments"])
                if not page["page"]["has_more"]:
                    break
                offset += page["page"]["returned"]
            sources[f"{release}:{source_id}"] = records
            for record in records:
                annotation = record["annotation"]
                spans = annotation["identity_evidence"] + [s for r in annotation["relations"] for s in r["evidence"]]
                spans += [s for r in annotation.get("results", []) for s in r["evidence"]]
                for span in spans:
                    version, block = record["paper"]["paper_version_id"], span["block_id"]
                    key = f"{version}:{block}"
                    if key in contexts:
                        continue
                    units, offset = [], 0
                    while True:
                        context = get_paper_evidence_context(store, version, block_ids=[block], limit=100, offset=offset)
                        if not context["available"]:
                            raise ValueError(f"cannot export unavailable evidence version {version}")
                        units.extend(context["units"])
                        if not context["page"]["has_more"]:
                            break
                        offset += context["page"]["returned"]
                    contexts[key] = {"available": True, "paper": context["paper"], "units": units}
        monitor.ensure_unchanged(revision)
        return {"exported_at": datetime.now(timezone.utc).isoformat(), "revision": revision,
                "sources": sources, "contexts": contexts,
                "batch_ids": sorted({r["batch_id"] for records in sources.values() for r in records})}
    finally:
        monitor.close()


def snapshot_assets() -> str:
    assets = Path(__file__).with_name("assets")
    return f'<style>{(assets / "evidence.css").read_text(encoding="utf-8")}</style><script>{(assets / "evidence.js").read_text(encoding="utf-8")}</script>'


def script_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def export_evidence_html(store: LibraryStore | None, targets: list[tuple[str, str]], output_path: Path | str, *, title: str = "Astro-Corpus · 文献与证据") -> Path:
    snapshot = evidence_snapshot(store, targets)
    document = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>:root {{ --ink:#18273a; --secondary:#586777; --surface:#fff; --line:#dbe2e8; --soft:#eef3f8; }}
body {{ margin:32px auto; padding:0 20px; max-width:1000px; background:#f8fafb; color:var(--ink); font:15px/1.6 system-ui; }} select {{padding:10px;max-width:100%;}} a{{color:inherit;}}</style>
{snapshot_assets()}<h1>{html.escape(title)}</h1><p>固定证据快照 · 全部待复核 · 导出时间 {html.escape(snapshot["exported_at"])}</p>
<label>天体 <select id="e-source"></select></label><main id="e-root"></main><script>
const snapshot = {script_json(snapshot)};
const selector = document.getElementById("e-source");
for (const key of Object.keys(snapshot.sources)) selector.add(new Option(key, key));
function render() {{ window.AstroEvidence.mount(document.getElementById("e-root"), {{ records: snapshot.sources[selector.value] || [], snapshotLabel: "固定导出",
loadContext: async (version, block) => snapshot.contexts[`${{version}}:${{block}}`] }}); }}
selector.addEventListener("change", render); render();</script></html>'''
    out = Path(output_path)
    atomic_file(out, lambda temp: temp.write_text(document, encoding="utf-8"))
    return out
