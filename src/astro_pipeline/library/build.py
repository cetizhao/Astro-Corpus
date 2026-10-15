"""Corpus/run lifecycle and source projection for the shared library."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import logging
import os
import sys
from pathlib import Path
import tempfile
from typing import Any

import pandas as pd

from astro_pipeline.intake import CANONICAL_RENAME, QC_EXTRA_COLUMNS, InputResolution
from astro_pipeline.library.files import atomic_bytes, atomic_copy, atomic_file, atomic_json, sha256_file
from astro_pipeline.library.identity import canonical_json, identity_hash, json_value, normalize_identifier
from astro_pipeline.manifest import CorpusManifest

logger = logging.getLogger(__name__)


@dataclass
class LibraryRun:
    store: Any
    out_dir: Path
    label: str
    input_mode: str
    input_summary: str
    contract: dict
    release: str
    requested_products: dict
    options: dict = field(default_factory=dict)
    verified: bool = True

    def __post_init__(self):
        self.out_dir = Path(self.out_dir).resolve()
        self.manifest = None
        self.run_id = ""
        self.corpus_id = ""
        self.request_id = ""
        self.resolution = None
        self.counts = {}
        self.indexed = False
        self.published = False
        self.prepared = False
        self._recorded = set()
        self._guard = None

    def _validate(self):
        return CorpusManifest.validate_input(
            self.out_dir / "manifest.json", label=self.label,
            input_mode=self.input_mode, input_summary=self.input_summary,
            gaia_release=self.release, input_contract=self.contract,
        )

    def __enter__(self):
        self._validate()
        self.store.initialize()
        self._guard = self.store.hold_lease(
            "corpus", str(self.out_dir), ttl=self.store.lease_seconds,
            wait_timeout=self.store.wait_timeout_seconds,
        )
        self.lease = self._guard.__enter__()
        try:
            self._validate()
            self.corpus_id = self.store.register_corpus(self.out_dir, self.label)
            self.request_id = self.store.register_request(self.contract)
            self.run_id = self.store.start_run(self.corpus_id, self.request_id, self.options)
            self.manifest = CorpusManifest.open_or_create(
                self.out_dir / "manifest.json", label=self.label,
                input_mode=self.input_mode, input_summary=self.input_summary,
                requested_products=self.requested_products, gaia_release=self.release,
                input_contract=self.contract,
            )
            self.manifest.write_guard = self.fenced
            self.manifest.defer_finish = True
            self.manifest.data.pop("snapshot_run_id", None)
            self.manifest.data.pop("pipeline_status", None)
            self.manifest.data.setdefault("active_artifacts", dict(self.manifest.data.get("artifacts", {})))
            self.manifest.data.update({
                "schema_version": "2.0", "storage": {
                    "kind": "shared_library", "library_root": str(self.store.root),
                    "relative_library_root": os.path.relpath(self.store.root, self.out_dir),
                    "index_path": "index.sqlite3",
                },
                "corpus_id": self.corpus_id, "request_id": self.request_id,
                "current_run_id": self.run_id,
                "stages": {}, "artifacts": {}, "warnings": [], "errors": [],
            })
            self.manifest.data["runs"][-1]["run_id"] = self.run_id
            self.manifest.save()
            self.run_dir = self.out_dir / "runs" / self.run_id
            self.run_dir.mkdir(parents=True, exist_ok=False)
            return self
        except BaseException:
            if self.run_id:
                self.store.finish_run(self.run_id, "failed", error="run initialization failed")
            self._guard.__exit__(*sys.exc_info())
            raise

    @contextmanager
    def fenced(self):
        with self.store.transaction() as connection:
            self.store.assert_lease(connection, self.lease)
            yield

    def set_resolved_contract(self, contract: dict, resolved_input: dict):
        self.contract = dict(contract)
        self.request_id = self.store.register_request(self.contract)
        with self.store.transaction() as connection:
            self.store.assert_lease(connection, self.lease)
            connection.execute("UPDATE runs SET request_id = ? WHERE run_id = ?", (self.request_id, self.run_id))
        self.manifest.data["request_id"] = self.request_id
        self.manifest.data["corpus"].update({
            "input_contract": json_value(contract),
            "input_hash": identity_hash(contract, namespace="request"),
        })
        self.manifest.data["resolved_input"] = json_value(resolved_input)
        self.manifest.save()
        self.store.update_run(self.run_id, {"resolved_input": resolved_input})

    def record_file(self, kind: str, path: Path, row_count=None):
        path = Path(path)
        key = str(path.resolve())
        if row_count is None:
            row_count = self.manifest.data.get("artifacts", {}).get(kind, {}).get("rows")
        if key not in self._recorded:
            self.store.record_run_artifact(self.run_id, kind, path, sha256_file(path), row_count)
            self._recorded.add(key)
        self.manifest.add_artifact(kind, path, rows=row_count, sha256=sha256_file(path))

    def capture_input(self, catalog: Path | str | None):
        if catalog is None:
            return
        payload = Path(catalog).read_bytes()
        import hashlib
        digest = hashlib.sha256(payload).hexdigest()
        expected = self.contract.get("selector", {}).get("csv_sha256")
        if expected is not None and digest != expected:
            raise ValueError("Input CSV changed after preflight; retry with a stable file")
        path = self.run_dir / "inputs" / f"{digest}.csv"
        atomic_bytes(path, payload)
        self.record_file("original_input_csv", path)
        self.store.update_run(self.run_id, {"original_input_name": Path(catalog).name})

    def set_resolution(self, resolution: InputResolution):
        self.resolution = resolution
        if resolution.contract and canonical_json(resolution.contract) != canonical_json(self.contract):
            raise ValueError("Resolved input differs from the validated request contract")
        if resolution.raw_input is not None:
            import hashlib
            digest = hashlib.sha256(resolution.raw_input).hexdigest()
            path = self.run_dir / "inputs" / f"{digest}.csv"
            if not path.exists():
                atomic_bytes(path, resolution.raw_input)
                self.record_file("original_input_csv", path)
        for name, frame in (("input_rows", resolution.input_rows), ("input_matches", resolution.matches)):
            if len(frame.columns):
                path = self.run_dir / f"{name}.parquet"
                atomic_file(path, lambda temporary, frame=frame: frame.to_parquet(temporary, index=False))
                self.record_file(name, path, len(frame))
        if not resolution.input_rows.empty:
            unmatched = resolution.input_rows.loc[
                resolution.input_rows["status"].isin({"unmatched", "invalid", "id_not_found"})
            ]
            path = self.run_dir / "unmatched_input_rows.parquet"
            atomic_file(path, lambda temporary: unmatched.to_parquet(temporary, index=False))
            self.record_file("unmatched_input_rows", path, len(unmatched))
        self.manifest.data["resolved_input"] = json_value(resolution.resolved_input)
        self.manifest.save()
        self.store.update_run(self.run_id, {
            "resolved_input": resolution.resolved_input,
            "original_input_name": resolution.original_name,
        })

    def index_members(self, members: pd.DataFrame):
        if self.indexed:
            return
        resolution = self.resolution or InputResolution(members)
        input_rows = json_value(resolution.input_rows.to_dict(orient="records"))
        by_input = {
            str(row["input_row_id"]): row
            for row in json_value(resolution.matches.to_dict(orient="records"))
        }
        allowed = set(CANONICAL_RENAME.values()) | set(QC_EXTRA_COLUMNS) | {
            "has_xp_sampled", "has_xp_continuous",
        }
        records = []
        matches = []
        for ordinal, row in enumerate(json_value(members.to_dict(orient="records"))):
            source_id = normalize_identifier(row["Gaia"])
            input_id = row.get("input_row_id", row.get("membership_catalog_row"))
            if input_id is not None:
                input_id = str(int(input_id))
            records.append({
                "member_ordinal": ordinal, "Gaia": source_id,
                "gaia_release": self.release, "input_row_id": input_id,
                "attributes": {key: value for key, value in row.items() if key in allowed and key != "Gaia"},
            })
            matches.append({
                "member_ordinal": ordinal, "input_row_id": input_id,
                "match": by_input.get(input_id, {
                    key: value for key, value in row.items()
                    if key.startswith(("match_", "membership_"))
                }),
            })
        self.manifest.update_stage("library_index", status="running")
        self.counts = self.store.index_members(
            self.run_id, records, input_rows=input_rows, matches=matches,
            verified=self.verified, lease=self.lease,
        )
        self.indexed = True
        self.store.update_run(self.run_id, {"members_indexed": True, **self.counts})
        self.manifest.update_stage("library_index", status="completed", **self.counts)

    def _promote_file(self, source: Path, destination: Path):
        if source.name == "product_refs.json":
            from astro_pipeline.library.refs import copy_reference_file
            with self.fenced():
                copy_reference_file(source, destination)
        else:
            # Only the final rename is fenced; preparation never holds a DB lock.
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
            os.close(descriptor)
            temporary = Path(name)
            try:
                import shutil
                shutil.copyfile(source, temporary)
                with temporary.open("r+b") as stream:
                    os.fsync(stream.fileno())
                with self.fenced():
                    os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    def finish(self, result):
        from astro_pipeline.library.refs import collect_corpus_refs
        collect_corpus_refs(self.run_dir, run_id=self.run_id, release=self.release)
        members_path = self.run_dir / "members.parquet"
        terminal = self.manifest.data.get("pipeline_status", self.manifest.data["status"])
        publishable = members_path.exists() and self.indexed and terminal in {"completed", "partial"}
        artifact_names = {
            str(Path(value["path"]).resolve()): name
            for name, value in self.manifest.data.get("artifacts", {}).items()
            if value.get("path")
        }
        files = []
        for path in sorted(self.run_dir.rglob("*")):
            if not path.is_file() or path.name.startswith(".") or path.name == "manifest.json":
                continue
            if path.suffix.lower() not in {".csv", ".parquet", ".json", ".html", ".png"}:
                continue
            relative = path.relative_to(self.run_dir)
            self.record_file(artifact_names.get(str(path.resolve()), str(relative)), path)
            files.append((path, relative))
        self.manifest.data["library"] = {
            "indexed": self.indexed, "published": False, "view_pending": publishable,
            "root": str(self.store.root), "run_id": self.run_id, **self.counts,
        }
        self.manifest.save()
        snapshot_manifest = self.run_dir / "manifest.json"
        snapshot = self.manifest.snapshot()
        snapshot["snapshot_run_id"] = self.run_id
        snapshot["status"] = terminal
        snapshot.pop("active_run_id", None)
        snapshot.pop("active_artifacts", None)
        snapshot["runs"] = [entry for entry in snapshot.get("runs", []) if entry.get("run_id") == self.run_id]
        for entry in snapshot["runs"]:
            entry["status"] = terminal
        snapshot["storage"]["relative_library_root"] = os.path.relpath(self.store.root, self.run_dir)
        atomic_json(snapshot_manifest, snapshot)
        self.store.record_run_artifact(self.run_id, "manifest_snapshot", snapshot_manifest, sha256_file(snapshot_manifest))
        self.store.finish_run(
            self.run_id, terminal, error=result.error,
            metadata={
                "members_indexed": self.indexed, "published": False,
                "prepared": publishable, "view_pending": publishable,
                "snapshot_path": str(self.run_dir), **self.counts,
            },
        )
        self.prepared = publishable
        if publishable:
            for path, relative in files:
                if relative.parts[0] != "inputs":
                    self._promote_file(path, self.out_dir / relative)
            self.manifest.data["active_run_id"] = self.run_id
            self.manifest.data["active_artifacts"] = dict(self.manifest.data.get("artifacts", {}))
            self.manifest.data["library"].update(published=True, view_pending=False)
        self.manifest.defer_finish = False
        self.manifest.finish(terminal)
        if publishable:
            self.published = True
            self.store.update_run(
                self.run_id, {"published": True, "view_pending": False}, lease=self.lease,
            )
        result.run_id = self.run_id
        result.library_corpus_id = self.corpus_id
        result.library_root = str(self.store.root)
        result.library_indexed = self.indexed
        result.n_input_rows = self.counts.get("n_input_rows", 0)
        result.n_unique_sources = self.counts.get("n_unique_sources", 0)
        result.storage_mode = "shared_library"
        result.out_dir = str(self.out_dir)
        result.manifest_json = str(self.manifest.path)
        if self.published:
            for name in ("members_csv", "members_parquet", "paper_edges_parquet", "simbad_refs_parquet"):
                value = getattr(result, name)
                if value and Path(value).is_relative_to(self.run_dir):
                    setattr(result, name, str(self.out_dir / Path(value).relative_to(self.run_dir)))
            if result.viz_html:
                result.viz_html = f"file://{self.out_dir / 'source_paper_viz.html'}"
        return result

    def __exit__(self, error_type, error, traceback):
        if error is not None and self.run_id:
            try:
                if self.prepared:
                    self.store.update_run(self.run_id, {
                        "view_pending": True, "publication_error": str(error),
                    }, lease=self.lease)
                else:
                    self.store.finish_run(self.run_id, "failed", error=str(error), metadata={"published": False})
            except Exception as failure:
                logger.warning("Could not record library run failure: %s", failure)
            if self.manifest is not None:
                try:
                    self.manifest.defer_finish = False
                    self.manifest.add_error(str(error))
                    if self.prepared:
                        self.manifest.data.setdefault("library", {})["view_pending"] = True
                    self.manifest.finish("partial" if self.prepared else "failed")
                except Exception as failure:
                    logger.warning("Could not update failed build manifest: %s", failure)
        if self._guard is not None:
            return self._guard.__exit__(error_type, error, traceback)
        return False


def id_cache_fetcher(store, release: str, *, refresh: bool = False):
    from astro_pipeline.config import PipelineConfig
    from astro_pipeline.intake import _fetch_gaia_by_ids
    from astro_pipeline.library.identity import source_cache_key
    config = PipelineConfig.load()
    remote_fetcher = _fetch_gaia_by_ids
    if config.gaia.release != release:
        from astro_pipeline.gaia_releases import get_gaia_release_profile
        from astro_pipeline.intake import _canonicalize
        from astro_pipeline.models.query import SourceIdQuery
        from astro_pipeline.sources.gaia import GaiaSource
        gaia = GaiaSource(config.gaia.model_copy(update={
            "release": release, "db_schema": get_gaia_release_profile(release).tap_schema,
        }), config.network)

        def remote_fetcher(ids):
            return _canonicalize(gaia._query_by_ids(
                SourceIdQuery(source_ids=ids), extra_columns=QC_EXTRA_COLUMNS,
            ).data)

    required = {CANONICAL_RENAME.get(name, name) for name in config.gaia.default_columns + QC_EXTRA_COLUMNS} - {"Gaia"}

    def fetch(ids):
        rows = []
        missing = []
        for raw_id in ids:
            identifier = normalize_identifier(raw_id)
            cached = None if refresh else store.source(release, identifier)
            if cached is not None and required.issubset(cached["covered_fields"]):
                rows.append({"Gaia": identifier, **cached["attributes"]})
                continue
            negative = None if refresh else store.get_cache("gaia-id-status", source_cache_key(release, identifier))
            if negative is None or negative["outcome"] != "not_found":
                missing.append(identifier)
        if missing:
            queried = remote_fetcher(list(dict.fromkeys(missing)))
            found = set(queried.get("Gaia", pd.Series(dtype=str)).astype(str))
            for identifier in missing:
                present = identifier in found
                store.put_cache(
                    "gaia-id-status", source_cache_key(release, identifier),
                    {"source_id": identifier, "gaia_release": release, "found": present},
                    outcome="success" if present else "not_found",
                    ttl_seconds=store.positive_ttl_seconds if present else store.negative_ttl_seconds,
                )
            rows.extend(queried.to_dict(orient="records"))
        return pd.DataFrame(rows, columns=None if rows else ["Gaia", *sorted(required)])

    return fetch
