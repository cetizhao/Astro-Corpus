"""Read-only, global queries over the local astronomical library."""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from astro_pipeline.gaia_releases import normalize_gaia_release
from astro_pipeline.library.identity import source_cache_key, source_identity
from astro_pipeline.library.store import LibraryStore


_SCOPE = "global_library"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _json_value(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _page(limit: int, offset: int, total: int, returned: int) -> dict[str, Any]:
    return {
        "limit": limit,
        "offset": offset,
        "total": total,
        "returned": returned,
        "has_more": offset + returned < total,
    }


def _pagination(limit: int, offset: int) -> tuple[int, int]:
    limit = int(limit)
    offset = int(offset)
    if limit < 1 or limit > _MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    return limit, offset


def _run_order(run: Mapping[str, Any]) -> tuple[float, float, str]:
    started = float(run.get("started_at") or 0.0)
    finished = float(run.get("finished_at") or started)
    return finished, started, str(run.get("run_id") or "")


class Library:
    """Read-only service for global source, paper, and status queries."""

    def __init__(self, store: LibraryStore | None) -> None:
        self.store = store

    def status(self) -> dict[str, Any]:
        if self.store is None:
            return {
                "exists": False,
                "enabled": False,
                "scope": _SCOPE,
                "schema": 0,
                "n_sources": 0,
                "n_corpora": 0,
                "n_runs": 0,
                "n_papers": 0,
                "n_ready_products": 0,
                "n_blobs": 0,
                "n_current_datasets": 0,
            }
        result = {"scope": _SCOPE, "enabled": True, **self.store.status()}
        result["root"] = str(self.store.root)
        result["db_path"] = str(self.store.db_path)
        result["n_current_datasets"] = (
            len(self._current_runs()) if result["exists"] else 0
        )
        return result

    def _absent(self, **query: Any) -> dict[str, Any]:
        status = self.status()
        return {
            "exists": False,
            "scope": _SCOPE,
            "resolved": False,
            "query": query,
            "library": status,
        }

    def _current_runs(self) -> list[dict[str, Any]]:
        """Choose one latest published member snapshot per corpus."""
        if self.store is None or not self.store.exists:
            return []
        rows = self.store.read(
            "SELECT c.corpus_id, c.canonical_path, c.label, c.provenance_status, "
            "r.run_id, r.request_id, r.status, r.started_at, r.finished_at, "
            "r.metadata_json FROM corpora AS c "
            "JOIN runs AS r ON r.corpus_id = c.corpus_id"
        )
        selected: dict[str, dict[str, Any]] = {}
        for row in rows:
            metadata = _json_object(row["metadata_json"])
            if not (
                metadata.get("members_indexed") is True
                and metadata.get("published") is True
            ):
                continue
            candidate = {
                "corpus_id": row["corpus_id"],
                "path": row["canonical_path"],
                "label": row["label"],
                "provenance_status": row["provenance_status"],
                "run_id": row["run_id"],
                "request_id": row["request_id"],
                "status": row["status"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "metadata": metadata,
            }
            current = selected.get(row["corpus_id"])
            if current is None or _run_order(candidate) > _run_order(current):
                selected[row["corpus_id"]] = candidate
        return sorted(
            selected.values(),
            key=lambda row: (str(row["label"]).casefold(), row["corpus_id"]),
        )

    def _datasets_for_source(
        self,
        release: str,
        source_id: str,
        *,
        current_runs: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if self.store is None or not self.store.exists:
            return []
        current_runs = list(current_runs or self._current_runs())
        datasets: list[dict[str, Any]] = []
        for run in current_runs:
            member_rows = self.store.read(
                "SELECT member_ordinal, input_row_id, attributes_json, match_json "
                "FROM sample_members WHERE run_id = ? AND release = ? "
                "AND source_id = ? ORDER BY member_ordinal",
                (run["run_id"], release, source_id),
            )
            if not member_rows:
                continue
            datasets.append(
                {
                    **dict(run),
                    "n_member_rows": len(member_rows),
                    "members": [
                        {
                            "member_ordinal": row["member_ordinal"],
                            "input_row_id": row["input_row_id"],
                            "attributes": _json_value(row["attributes_json"], {}),
                            "match": _json_value(row["match_json"], {}),
                        }
                        for row in member_rows
                    ],
                }
            )
        return datasets

    def _availability_for_source(
        self, release: str, source_id: str
    ) -> dict[str, Any] | None:
        if self.store is None or not self.store.exists:
            return None
        return self.store.get_cache(
            "gaia-xp-availability:v1",
            source_cache_key(release, source_id),
            include_stale=True,
        )

    def _products_for_source(
        self, release: str, source_id: str
    ) -> list[dict[str, Any]]:
        if self.store is None or not self.store.exists:
            return []
        rows = self.store.read(
            "SELECT i.item_key, i.identifier, i.active_version_id, i.updated_at, "
            "i.last_error, c.contract_json FROM product_items AS i "
            "JOIN product_contracts AS c ON c.contract_hash = i.contract_hash "
            "WHERE i.identifier = ? AND i.state = 'ready' "
            "AND i.active_version_id IS NOT NULL ORDER BY i.updated_at DESC",
            (source_id,),
        )
        products: list[dict[str, Any]] = []
        for row in rows:
            contract = _json_object(row["contract_json"])
            if str(contract.get("linking_parameter", "")).upper() != "SOURCE_ID":
                continue
            try:
                contract_release = normalize_gaia_release(contract.get("release", ""))
            except ValueError:
                continue
            if contract_release != release:
                continue
            version = self.store.get_product_version(
                row["active_version_id"], verify=False
            )
            if version is None:
                continue
            products.append(
                {
                    **version,
                    "state": "ready",
                    "updated_at": row["updated_at"],
                    "last_error": row["last_error"],
                    "integrity": "unchecked",
                }
            )
        return products

    def lookup_source(
        self,
        source_id: Any,
        release: str = "dr3",
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        release, source_id = source_identity(release, source_id)
        query = {"release": release, "source_id": source_id}
        if self.store is None or not self.store.exists:
            return self._absent(**query)
        source = self.store.source(release, source_id)
        if source is None:
            return {
                "exists": True,
                "scope": _SCOPE,
                "resolved": False,
                "query": query,
                "reason": "source_not_found",
                "datasets": [],
                "xp_availability": self._availability_for_source(
                    release, source_id
                ),
                "active_products": [],
                "pagination": _page(limit, offset, 0, 0),
            }
        datasets = self._datasets_for_source(release, source_id)
        page = datasets[offset : offset + limit]
        bibliography = self.store.get_source_references(
            release, source_id, include_stale=True
        )
        return {
            "exists": True,
            "scope": _SCOPE,
            "resolved": True,
            "query": query,
            "source": source,
            "datasets": page,
            "pagination_target": "datasets",
            "pagination": _page(limit, offset, len(datasets), len(page)),
            "active_bibliography": bibliography,
            "xp_availability": self._availability_for_source(release, source_id),
            "active_products": self._products_for_source(release, source_id),
        }

    @staticmethod
    def _paper_matches(
        rows: Sequence[Mapping[str, Any]], query: str
    ) -> list[dict[str, Any]]:
        from astro_pipeline.library.papers import normalize_doi
        from astro_pipeline.sources.paper_fulltext import split_arxiv_id

        folded = query.casefold()
        doi_query = normalize_doi(query)
        try:
            arxiv_work, _ = split_arxiv_id(query)
        except ValueError:
            arxiv_work = None
        exact: list[dict[str, Any]] = []
        partial: list[dict[str, Any]] = []
        for row in rows:
            metadata = _json_object(row["metadata_json"])
            metadata.setdefault("bibcode", row["bibcode"])
            identifiers = [
                str(metadata.get("bibcode", "")),
                str(metadata.get("arxiv_id") or metadata.get("arxiv") or ""),
                str(metadata.get("doi") or ""),
            ]
            try:
                candidate_work, _ = split_arxiv_id(identifiers[1])
            except ValueError:
                candidate_work = None
            doi_match = bool(identifiers[2].strip()) and normalize_doi(identifiers[2]) == doi_query
            if (any(value and value.casefold() == folded for value in identifiers)
                    or doi_match or (arxiv_work is not None and candidate_work == arxiv_work)):
                exact.append(metadata)
                continue
            title = str(metadata.get("title", ""))
            if title and folded in title.casefold():
                partial.append(metadata)
        matches = exact if exact else partial
        return sorted(matches, key=lambda item: str(item.get("bibcode", "")))

    def _paper_sources(
        self,
        bibcode: str,
        *,
        current_runs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        rows = self.store.read(
            "SELECT s.gaia_release, s.source_id, s.attributes_json, "
            "s.covered_fields_json, s.field_provenance_json, s.updated_at, "
            "snap.snapshot_id, snap.outcome, snap.main_id, snap.observed_at, "
            "snap.expires_at FROM source_ref_current AS current "
            "JOIN source_ref_snapshots AS snap "
            "ON snap.snapshot_id = current.snapshot_id "
            "JOIN source_papers AS edge ON edge.snapshot_id = snap.snapshot_id "
            "JOIN sources AS s ON s.gaia_release = current.release "
            "AND s.source_id = current.source_id "
            "WHERE edge.bibcode = ? ORDER BY s.gaia_release, s.source_id",
            (bibcode,),
        )
        now = float(self.store.clock())
        result: list[dict[str, Any]] = []
        for row in rows:
            release = row["gaia_release"]
            source_id = row["source_id"]
            result.append(
                {
                    "source": {
                        "gaia_release": release,
                        "source_id": source_id,
                        "attributes": _json_value(row["attributes_json"], {}),
                        "covered_fields": _json_value(
                            row["covered_fields_json"], []
                        ),
                        "field_provenance": _json_value(
                            row["field_provenance_json"], {}
                        ),
                        "updated_at": row["updated_at"],
                    },
                    "reference_snapshot": {
                        "snapshot_id": row["snapshot_id"],
                        "outcome": row["outcome"],
                        "main_id": row["main_id"],
                        "observed_at": row["observed_at"],
                        "expires_at": row["expires_at"],
                        "stale": float(row["expires_at"]) <= now,
                    },
                    "datasets": self._datasets_for_source(
                        release,
                        source_id,
                        current_runs=current_runs,
                    ),
                }
            )
        return result

    def lookup_paper(
        self,
        query: str,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        query = str(query).strip()
        if not query:
            raise ValueError("paper query must not be blank")
        if self.store is None or not self.store.exists:
            return self._absent(paper=query)
        rows = self.store.read(
            "SELECT bibcode, metadata_json, updated_at FROM papers ORDER BY bibcode"
        )
        matches = self._paper_matches(rows, query)
        from astro_pipeline.library.paper_corpus import lookup_paper_text

        fulltext = lookup_paper_text(self.store, query)
        from astro_pipeline.sources.paper_fulltext import split_arxiv_id

        try:
            split_arxiv_id(query)
            is_arxiv_query = True
        except ValueError:
            is_arxiv_query = False
        if not fulltext["available"] and len(matches) == 1 and not is_arxiv_query:
            identifier = matches[0].get("arxiv_id") or matches[0].get("arxiv") or matches[0].get("doi")
            if identifier:
                fulltext = lookup_paper_text(self.store, str(identifier))
        if not matches and fulltext["available"]:
            works = {v["arxiv_id"] for v in fulltext["versions"]}
            if len(works) == 1:
                metadata = dict(fulltext["versions"][0]["metadata"])
                metadata.pop("metadata_blob", None)
                return {
                    "exists": True, "scope": _SCOPE, "resolved": True,
                    "query": query, "paper": metadata, "sources": [],
                    "fulltext": fulltext, "pagination_target": "sources",
                    "pagination": _page(limit, offset, 0, 0),
                }
        if not matches:
            return {
                "exists": True,
                "scope": _SCOPE,
                "resolved": False,
                "query": query,
                "reason": "multiple_fulltext_papers" if fulltext["available"] else "paper_not_found",
                "fulltext": fulltext,
                "matches": [],
                "pagination": _page(limit, offset, 0, 0),
            }
        if len(matches) > 1:
            page = matches[offset : offset + limit]
            return {
                "exists": True,
                "scope": _SCOPE,
                "resolved": False,
                "ambiguous": True,
                "query": query,
                "reason": "multiple_papers",
                "fulltext": fulltext,
                "matches": page,
                "pagination_target": "matches",
                "pagination": _page(limit, offset, len(matches), len(page)),
            }
        paper = matches[0]
        bibcode = str(paper["bibcode"])
        sources = self._paper_sources(bibcode, current_runs=self._current_runs())
        page = sources[offset : offset + limit]
        return {
            "exists": True,
            "scope": _SCOPE,
            "resolved": True,
            "query": query,
            "paper": paper,
            "fulltext": fulltext,
            "sources": page,
            "pagination_target": "sources",
            "pagination": _page(limit, offset, len(sources), len(page)),
        }


def library_status(store: LibraryStore | None) -> dict[str, Any]:
    return Library(store).status()


def lookup_library_source(
    store: LibraryStore | None,
    source_id: Any,
    release: str = "dr3",
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    return Library(store).lookup_source(source_id, release, limit, offset)


def lookup_library_paper(
    store: LibraryStore | None,
    query: str,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    return Library(store).lookup_paper(query, limit, offset)
