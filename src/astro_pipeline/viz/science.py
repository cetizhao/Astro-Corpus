"""Explicit public selection of versioned scientific records; no local paths or full papers."""
from __future__ import annotations

import math
from astro_pipeline.library.identity import identity_hash


def public_science_snapshot(package: dict) -> dict:
    selected = set(package["selected_revision_ids"])
    records, sources = [], {}
    dataset = {"corpus_id": "research:" + package["export_id"], "label": "科研语料示例",
               "run_id": package["export_id"], "status": "completed", "snapshot_state": "current",
               "selection_type": "explicit_research_record_selection"}
    for record in package["records"]:
        records.append({k: v for k, v in record.items() if k != "source_snapshots"})
        snapshots = {(s["release"], s["source_id"]): s for s in record["source_snapshots"]}
        for target in record["item"]["targets"]:
            key = f"{target['release']}:{target['source_id']}"
            if key in sources:
                continue
            snapshot = snapshots[target["release"], target["source_id"]]
            attrs = snapshot["attributes"]
            def number(column):
                try:
                    value = float(attrs.get(column))
                    return value if math.isfinite(value) else None
                except (ValueError, TypeError):
                    return None
            ra, dec = number("RAICRS"), number("DEICRS")
            # Do not label future-release fields/epochs as DR3 observations.
            suffix = {"dr3": "gdr3", "dr2": "gdr2"}.get(target["release"])
            astrometry = {"parallax_mas": number(f"Plx_{suffix}") if suffix else None,
                          "pmra_masyr": number(f"pmRA_{suffix}") if suffix else None,
                          "pmdec_masyr": number(f"pmDE_{suffix}") if suffix else None,
                          "radial_velocity_kms": number(f"RV_{suffix}") if suffix else None,
                          "ruwe": number(f"RUWE_{suffix}") if suffix else None,
                          "g_mag": number(f"Gmag_{suffix}") if suffix else None,
                          "bp_mag": number(f"BPmag_{suffix}") if suffix else None,
                          "rp_mag": number(f"RPmag_{suffix}") if suffix else None}
            epoch = {"dr3": 2016.0, "dr2": 2015.5}.get(target["release"])
            sources[key] = {"key": key, "release": target["release"], "source_id": target["source_id"],
                "name": target["name"], "catalog_names": [{"name": a["name"], "origin": "research_target_declaration"} for a in target["aliases"]],
                "annotation_count": 0, "annotation_paper_count": 0,
                "position": {"ra_deg": ra, "dec_deg": dec, "frame": "ICRS", "epoch": epoch,
                             "epoch_status": "release_reference_epoch" if epoch else "unknown",
                             "valid": ra is not None and dec is not None and 0 <= ra < 360 and -90 <= dec <= 90},
                "astrometry": astrometry, "datasets": [{**dataset, "n_member_rows": 1}],
                "working_datasets": [], "spectrum_status": "not_downloaded"}
    items = [r for r in records if r["revision_id"] in selected]
    return {"exported_at": package["created_at"], "sources": list(sources.values()),
            "datasets": [{**dataset, "n_member_rows": len(sources)}],
            "science": {"schema_version": "research-query-1", "schema_ready": True, "read_only": True,
                        "items": items, "archive": records,
                        "page": {"total": len(items), "returned": len(items), "offset": 0, "limit": len(items), "has_more": False}},
            "revision": identity_hash(records, namespace="public-research-1")}
