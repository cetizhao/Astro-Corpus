"""Publication-linked VizieR table-text fallback; never interpret truncation as absence."""
from urllib.parse import quote

from astro_pipeline.paper_evidence import _matches


def find_vizier_evidence(bibcode, aliases):
    from astroquery.vizier import Vizier
    client = Vizier(columns=["*"], row_limit=5001)
    client.TIMEOUT = 45
    report = {"status": "pending", "evidence": [], "tables": [], "errors": []}
    try:
        candidates = client.find_catalogs(bibcode)
        catalogs = {key: resource for key, resource in candidates.items() if key and any(
            info.name == "cites" and info.value == "bibcode:" + bibcode
            for info in resource.infos)}
        report["catalogs"] = list(catalogs)
        if not catalogs:
            report["status"] = "no_linked_catalog_found"
            return report
        limited = len(catalogs) > 5
        for catalog in list(catalogs)[:5]:
            try:
                tables = client.get_catalogs(catalog)
                for table_id in tables.keys():
                    table = tables[table_id]
                    truncated = len(table) >= 5001
                    limited |= truncated
                    report["tables"].append({"id": table_id, "rows_checked": min(len(table), 5000), "possibly_truncated": truncated})
                    for index, row in enumerate(table[:5000], 1):
                        cells = {name: str(row[name]) for name in table.colnames}
                        text = " | ".join(cells.values())
                        matched = _matches(text, aliases)
                        # VizieR often stores the numeric Gaia ID without the catalogue prefix.
                        for alias in aliases:
                            if alias.startswith("Gaia DR3 "):
                                sid = alias.removeprefix("Gaia DR3 ")
                                if any(value.strip() == sid for name, value in cells.items()
                                       if "gaia" in name.lower() or name.lower() == "source_id"):
                                    matched.append(alias)
                        if matched:
                            report["evidence"].append({"location_type": "vizier", "table_id": table_id,
                                "row_index": index, "text": text, "row": cells, "matched_aliases": matched,
                                "association_provenance": "VizieR bibcode=" + bibcode,
                                "locator": {"url": "https://vizier.cds.unistra.fr/viz-bin/VizieR?-source=" + quote(table_id, safe="")}})
            except Exception as exc:
                report["errors"].append(str(exc))
        report["status"] = "mentions_found" if report["evidence"] else "partial" if limited or report["errors"] else "no_match_in_linked_tables"
        report["limited"] = limited
    except Exception as exc:
        report.update(status="lookup_failed", errors=[str(exc)])
    return report
