"""Explicit, version-pinned paper collections backed by immutable library blobs."""
from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version as package_version
import json
import multiprocessing
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence
import uuid

from astro_pipeline.library.files import BlobIntegrityError, atomic_json
from astro_pipeline.library.identity import canonical_json, identity_hash
from astro_pipeline.library.store import Lease, LeaseLostError, LibraryStore
from astro_pipeline.manifest import validate_label

MAX_PAPERS = 50
_NAMESPACE = "paper-text"
_COLLECTION_NAMESPACE = "paper-collection"
_SUCCESS = {"parsed", "partial"}


class _NoUsableText(ValueError):
    def __init__(self, message: str, artifacts: list[dict]):
        super().__init__(message)
        self.artifacts = artifacts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parser_key() -> str:
    from astro_pipeline.paper_text import PARSER_VERSION

    versions = {name: package_version(name) for name in ("beautifulsoup4", "pylatexenc", "pypdf")}
    return f"{PARSER_VERSION}:" + identity_hash(versions, namespace="paper-parser")[:16]


def _has_schema(store: LibraryStore | None) -> bool:
    return bool(store is not None and store.exists and store.read(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'paper_inputs'"
    ))


def _root(label: str, output_dir: Path | str | None) -> Path:
    from astro_pipeline.config import PipelineConfig

    label = validate_label(label)
    base = Path(output_dir) if output_dir is not None else PipelineConfig.load().output.base_dir
    parent = (base.expanduser().resolve() / "paper_corpora").resolve()
    path = parent / label
    if path.is_symlink() or path.resolve().parent != parent:
        raise ValueError("paper corpus label must not resolve outside its parent")
    return path


def _run_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("run_id must be a UUID") from exc


def _run_dir(root: Path, run_id: str) -> Path:
    runs = root / "runs"
    path = runs / _run_id(run_id)
    if runs.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("paper run path must not escape the collection or use symlinks")
    return path


def _selection(papers: Sequence[str | Mapping[str, Any]]) -> list[dict[str, Any]]:
    from astro_pipeline.sources.paper_fulltext import normalize_arxiv_id, split_arxiv_id

    if not isinstance(papers, (list, tuple)) or not 1 <= len(papers) <= MAX_PAPERS:
        raise ValueError(f"provide an explicit list of 1–{MAX_PAPERS} papers")
    result = []
    for ordinal, item in enumerate(papers):
        original = {"arxiv_id": item} if isinstance(item, str) else dict(item)
        identifier = normalize_arxiv_id(str(original.get("arxiv_id", "")))
        work_id, _ = split_arxiv_id(identifier)
        # Round-trip rejects non-JSON values before creating a collection.
        original = json.loads(canonical_json(original))
        result.append({"ordinal": ordinal, "input": original,
                       "requested_id": identifier, "work_id": work_id})
    if len(canonical_json(result).encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("paper selection exceeds 2 MiB")
    return result


def _blob(store: LibraryStore, data: bytes, extension: str, content_type: str) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    registered = store.read("SELECT * FROM blobs WHERE sha256 = ?", (digest,))
    if registered:
        record = registered[0]
        try:
            store.blobs.verify(record)
            return record
        except BlobIntegrityError:
            extension, content_type = record["extension"], record["content_type"]
    # Close the writer before BlobStore reopens the file; Windows locks an open
    # NamedTemporaryFile against this second read.
    with tempfile.TemporaryDirectory(prefix="astro-paper-") as temporary:
        source = Path(temporary) / "artifact"
        source.write_bytes(data)
        blob = store.blobs.publish(source, extension=extension,
                                   content_type=content_type, repair=True)
    return store.register_blob(blob)


def _artifact(store: LibraryStore, row: Mapping[str, Any], *, verify: bool = False) -> dict:
    value = dict(row)
    if verify:
        store.blobs.verify(value)
    return {"kind": value["kind"], "sha256": value["sha256"],
            "size_bytes": value["size_bytes"], "content_type": value["content_type"],
            "extension": value["extension"], "relative_path": value["relative_path"],
            "path": str(store.blobs.path(value)),
            "metadata": json.loads(value["metadata_json"])}


def _version(store: LibraryStore, version_id: str, *, verify: bool = False) -> dict | None:
    rows = store.read("SELECT * FROM paper_text_versions WHERE version_id = ?", (version_id,))
    if not rows:
        return None
    row = rows[0]
    artifacts = store.read(
        "SELECT a.kind, a.metadata_json, b.* FROM paper_text_artifacts a "
        "JOIN blobs b ON b.sha256 = a.blob_sha256 WHERE a.version_id = ? ORDER BY a.kind",
        (version_id,),
    )
    if not {"json", "markdown"}.issubset({a["kind"] for a in artifacts}):
        raise BlobIntegrityError("paper text version lacks its structured artifacts")
    return {"version_id": version_id, "arxiv_id": row["work_id"],
            "arxiv_version": row["arxiv_version"],
            "versioned_id": f"{row['work_id']}v{row['arxiv_version']}",
            "parser_version": row["parser_version"], "include_pdf": bool(row["include_pdf"]),
            "metadata": json.loads(row["metadata_json"]),
            "quality": json.loads(row["quality_json"]), "created_at": row["created_at"],
            "integrity": "verified" if verify else "not_checked",
            "artifacts": [_artifact(store, a, verify=verify) for a in artifacts]}


def _cached_version(store: LibraryStore, metadata: dict, parser_key: str, include_pdf: bool,
                    not_before: float | None = None) -> dict | None:
    rows = store.read(
        "SELECT c.version_id, v.created_at FROM paper_text_current c "
        "JOIN paper_text_versions v ON v.version_id = c.version_id "
        "WHERE c.work_id = ? AND c.arxiv_version = ? AND c.parser_version = ? AND c.include_pdf >= ? "
        "ORDER BY c.include_pdf, v.created_at DESC LIMIT 1",
        (metadata["arxiv_id"], metadata["version"], parser_key, int(include_pdf)),
    )
    if (not rows or float(store.clock()) - rows[0]["created_at"] > store.positive_ttl_seconds
            or (not_before is not None and rows[0]["created_at"] < not_before)):
        return None
    try:
        return _version(store, rows[0]["version_id"], verify=True)
    except BlobIntegrityError:
        return None


def _save_download(store: LibraryStore, metadata: dict, kind: str, data: bytes,
                   details: dict, lease: Lease) -> dict:
    extensions = {"metadata": "html", "html": "html", "source": "bin", "pdf": "pdf"}
    extension = "html" if kind.startswith("metadata") else extensions[kind]
    if kind.startswith("metadata") and "xml" in details.get("content_type", ""):
        extension = "xml"
    blob = _blob(store, data, extension, details.get("content_type", "application/octet-stream"))
    download_id = str(uuid.uuid4())
    with store.transaction() as connection:
        store.assert_lease(connection, lease)
        connection.execute(
            "INSERT INTO paper_downloads(download_id, work_id, arxiv_version, kind, "
            "blob_sha256, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (download_id, metadata["arxiv_id"], metadata["version"], kind, blob["sha256"],
             canonical_json(details), float(store.clock())),
        )
    return {"kind": kind, "blob": blob, "metadata": {**details, "download_id": download_id}}


def _download(store: LibraryStore, client: Any, metadata: dict, kind: str,
              lease: Lease, run_id: str, refresh: bool, *,
              pinned: dict | None = None, not_before: float | None = None) -> tuple[dict, bytes]:
    from astro_pipeline.sources.paper_fulltext import PaperUnavailable

    versioned_id = metadata["versioned_id"]
    if pinned is not None:
        data = store.blobs.verify(pinned).read_bytes()
        return {"kind": kind, "blob": pinned, "metadata": pinned["metadata"]}, data
    if not refresh:
        rows = store.read(
            "SELECT d.download_id, d.metadata_json, d.created_at AS observed_at, b.* "
            "FROM paper_downloads d JOIN blobs b ON b.sha256 = d.blob_sha256 "
            "WHERE d.work_id = ? AND d.arxiv_version = ? AND d.kind = ? "
            "ORDER BY d.created_at DESC, d.download_id DESC LIMIT 1",
            (metadata["arxiv_id"], metadata["version"], kind),
        )
        if (rows and float(store.clock()) - rows[0]["observed_at"] <= store.positive_ttl_seconds
                and (not_before is None or rows[0]["observed_at"] >= not_before)):
            row = rows[0]
            try:
                data = store.blobs.verify(row).read_bytes()
                return {"kind": kind, "blob": row,
                        "metadata": {**json.loads(row["metadata_json"]), "download_id": row["download_id"]}}, data
            except BlobIntegrityError:
                pass
        negative = store.get_cache("paper-unavailable", f"{versioned_id}:{kind}")
        if (negative and negative["outcome"] == "unavailable"
                and (not_before is None or negative["observed_at"] >= not_before)):
            raise PaperUnavailable(f"cached unavailable: {versioned_id} {kind}")
    attempt = store.record_attempt("paper-download", f"{versioned_id}:{kind}",
                                   {"versioned_id": versioned_id, "kind": kind}, run_id)
    try:
        fetched = client.fetch(versioned_id, kind)
        details = {"url": fetched.url, "final_url": fetched.final_url,
                   "content_type": fetched.content_type, "retrieved_at": fetched.retrieved_at,
                   "versioned_id": fetched.versioned_id}
        result = _save_download(store, metadata, kind, fetched.data, details, lease)
    except PaperUnavailable as exc:
        store.put_cache("paper-unavailable", f"{versioned_id}:{kind}", {"reason": str(exc)},
                        outcome="unavailable", ttl_seconds=store.negative_ttl_seconds, lease=lease)
        store.finish_attempt(attempt, "unavailable", exc)
        raise
    except Exception as exc:
        store.finish_attempt(attempt, "failed", exc)
        raise
    store.finish_attempt(attempt, "completed")
    return result, fetched.data


def _resolve(store: LibraryStore, client: Any, identifier: str, lease: Lease,
             run_id: str, refresh: bool) -> dict:
    from astro_pipeline.sources.paper_fulltext import split_arxiv_id

    if not refresh:
        cached = store.get_cache("paper-metadata", identifier)
        if cached is not None:
            metadata = dict(cached["payload"])
            raw = metadata.get("metadata_blob")
            try:
                if raw:
                    store.blobs.verify(raw)
                    for evidence in metadata.get("metadata_evidence_artifacts", []):
                        store.blobs.verify(evidence["blob"])
                    return metadata
            except BlobIntegrityError:
                pass
    attempt = store.record_attempt("paper-resolve", identifier, {"arxiv_id": identifier}, run_id)
    try:
        metadata = dict(client.resolve(identifier))
        work, requested_version = split_arxiv_id(identifier)
        if (metadata.get("arxiv_id") != work or not isinstance(metadata.get("version"), int)
                or metadata["version"] < 1
                or (requested_version is not None and metadata["version"] != requested_version)
                or metadata.get("versioned_id") != f"{work}v{metadata['version']}"):
            raise ValueError("resolved metadata does not match the requested arXiv identity")
        raw = metadata.pop("_metadata_bytes", None)
        evidence_responses = metadata.pop("_metadata_responses", [])
        content_type = metadata.pop("_metadata_content_type", "text/html")
        if not isinstance(raw, bytes) or not raw:
            raise ValueError("arXiv metadata response must retain its original bytes")
        artifact = _save_download(store, metadata, "metadata", raw,
                                  {"url": metadata.get("metadata_url"),
                                   "retrieved_at": metadata.get("resolved_at", _now()),
                                   "content_type": content_type}, lease)
        metadata["metadata_blob"] = artifact["blob"]
        metadata["metadata_download_id"] = artifact["metadata"]["download_id"]
        metadata["metadata_evidence_artifacts"] = []
        for index, response in enumerate(evidence_responses):
            if response["data"] == raw:
                continue
            evidence = _save_download(
                store, metadata, f"metadata_evidence_{index}", response["data"],
                {"url": response["url"], "content_type": response["content_type"],
                 "retrieved_at": metadata.get("resolved_at", _now())}, lease,
            )
            metadata["metadata_evidence_artifacts"].append(evidence)
        store.put_cache("paper-metadata", identifier, metadata,
                        ttl_seconds=store.positive_ttl_seconds, lease=lease)
    except Exception as exc:
        store.finish_attempt(attempt, "failed", exc)
        raise
    store.finish_attempt(attempt, "completed")
    return metadata


def _parse_worker(connection, kind: str, data: bytes, metadata: dict) -> None:
    try:
        from astro_pipeline import paper_text
        parser = {"html": paper_text.parse_html, "source": paper_text.parse_latex,
                  "pdf": paper_text.parse_pdf}[kind]
        connection.send((True, parser(data, paper=metadata)))
    except Exception as exc:
        connection.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


def _parse_document(kind: str, data: bytes, metadata: dict, timeout: float = 90) -> dict:
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_parse_worker, args=(writer, kind, data, metadata), daemon=True)
    process.start()
    writer.close()
    try:
        if not reader.poll(timeout):
            raise ValueError(f"{kind} parsing exceeded {timeout:g} seconds")
        try:
            ok, result = reader.recv()
        except EOFError as exc:
            raise ValueError(f"{kind} parser exited without a result") from exc
        if not ok:
            raise ValueError(result)
        return result
    finally:
        reader.close()
        process.join(timeout=1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
        if process.is_alive():
            process.kill()
            process.join()


def _document_provenance(document: dict, metadata: dict, parser_key: str, source: dict) -> None:
    document["source"] = {"kind": source["kind"], "sha256": source["blob"]["sha256"],
                          **source["metadata"]}
    document["parser"]["cache_key"] = parser_key
    document["rights"] = {"license": metadata.get("license"),
                          "license_evidence": metadata.get("license_evidence"),
                          "reuse_permission": "not_assessed"}


def _restore_pinned_version(store: LibraryStore, client: Any, pinned: dict,
                            lease: Lease, run_id: str) -> dict:
    """Repair only byte-identical pinned artifacts; never substitute a newer version."""
    from astro_pipeline.paper_text import markdown

    artifacts = {a["kind"]: a for a in pinned["artifacts"]}
    damaged = set()
    for kind, artifact in artifacts.items():
        try:
            store.blobs.verify(artifact)
        except BlobIntegrityError:
            damaged.add(kind)
    if not damaged:
        return _version(store, pinned["version_id"], verify=True)
    attempt = store.record_attempt("paper-repair", pinned["version_id"],
                                   {"versioned_id": pinned["versioned_id"], "artifacts": sorted(damaged)}, run_id)

    def restore(kind: str, data: bytes) -> None:
        artifact = artifacts[kind]
        observed = hashlib.sha256(data).hexdigest()
        if observed != artifact["sha256"]:
            raise BlobIntegrityError(
                f"cannot reproduce pinned {kind} bytes for {pinned['versioned_id']}; "
                "restore from backup or explicitly refresh into a new run"
            )
        with store.transaction() as connection:
            store.assert_lease(connection, lease)
        _blob(store, data, artifact["extension"], artifact["content_type"])

    try:
        metadata_responses = None
        for kind in sorted(damaged - {"json", "markdown"}):
            if kind.startswith("metadata"):
                if metadata_responses is None:
                    observation = client.resolve(pinned["versioned_id"])
                    metadata_responses = [observation["_metadata_bytes"]]
                    metadata_responses.extend(r["data"] for r in observation.get("_metadata_responses", []))
                data = next((data for data in metadata_responses
                             if hashlib.sha256(data).hexdigest() == artifacts[kind]["sha256"]), b"")
            else:
                fetched = client.fetch(pinned["versioned_id"], kind)
                if fetched.versioned_id != pinned["versioned_id"]:
                    raise ValueError("repair response changed the pinned paper identity")
                data = fetched.data
            restore(kind, data)
        if "json" in damaged:
            source_sha = artifacts["json"]["metadata"]["source_sha256"]
            source = next(a for a in artifacts.values()
                          if a["kind"] in {"html", "source", "pdf"} and a["sha256"] == source_sha)
            document = _parse_document(source["kind"], store.blobs.verify(source).read_bytes(), pinned["metadata"])
            _document_provenance(document, pinned["metadata"], pinned["parser_version"],
                                 {"kind": source["kind"], "blob": source, "metadata": source["metadata"]})
            document["quality"] = pinned["quality"]
            restore("json", (canonical_json(document) + "\n").encode())
        if "markdown" in damaged:
            document = json.loads(store.blobs.verify(artifacts["json"]).read_text(encoding="utf-8"))
            restore("markdown", markdown(document).encode())
        result = _version(store, pinned["version_id"], verify=True)
    except Exception as exc:
        store.finish_attempt(attempt, "failed", exc)
        raise
    store.finish_attempt(attempt, "completed")
    return result


def _acquire_version(store: LibraryStore, client: Any, metadata: dict, parser_key: str,
                     include_pdf: bool, refresh: bool, lease: Lease, run_id: str, *,
                     base: dict | None = None, not_before: float | None = None) -> dict:
    from astro_pipeline.paper_text import markdown
    from astro_pipeline.sources.paper_fulltext import PaperUnavailable

    artifacts = []
    errors = []
    document = None
    parsed_from = None
    pinned = {a["kind"]: a for a in base["artifacts"]} if base else {}
    routes = ("html", "source", "pdf")
    if base:
        metadata = {**metadata, "completed_from_text_version_id": base["version_id"]}
        source_sha = pinned["json"]["metadata"]["source_sha256"]
        routes = tuple(kind for kind in routes if kind in pinned and pinned[kind]["sha256"] == source_sha)
        not_before = max(not_before or 0.0, base["created_at"])
    for kind in routes:
        try:
            artifact, data = _download(store, client, metadata, kind, lease, run_id, refresh,
                                       pinned=pinned.get(kind), not_before=not_before)
            artifacts.append(artifact)
            document = _parse_document(kind, data, metadata)
            parsed_from = artifact
            break
        except (LeaseLostError, KeyboardInterrupt):
            raise
        except Exception as exc:
            errors.append({"stage": kind, "error": str(exc),
                           "unavailable": isinstance(exc, PaperUnavailable)})
    if document is None:
        message = "; ".join(f"{e['stage']}: {e['error']}" for e in errors)
        if errors and all(e["unavailable"] for e in errors):
            raise PaperUnavailable(message)
        raw_artifacts = [
            _artifact(store, {**a["blob"], "kind": a["kind"], "metadata_json": canonical_json(a["metadata"])})
            for a in artifacts
        ]
        raise _NoUsableText(f"no usable full text: {message}", raw_artifacts)
    if include_pdf and not any(a["kind"] == "pdf" for a in artifacts):
        try:
            pdf, _ = _download(store, client, metadata, "pdf", lease, run_id, refresh,
                               pinned=pinned.get("pdf"), not_before=not_before)
            artifacts.append(pdf)
        except LeaseLostError:
            raise
        except Exception as exc:
            errors.append({"stage": "reference_pdf", "error": str(exc),
                           "unavailable": isinstance(exc, PaperUnavailable)})
    has_pdf = any(a["kind"] == "pdf" for a in artifacts)
    _document_provenance(document, metadata, parser_key, parsed_from)
    quality = document["quality"]
    if quality.get("status") not in _SUCCESS or not document.get("blocks"):
        raise ValueError("parser did not return a usable, quality-labelled document")
    quality["acquisition_errors"] = errors
    quality["acquisition_complete"] = not include_pdf or has_pdf
    quality["scientifically_verified"] = False
    blocks = document["blocks"]
    metrics = quality.setdefault("metrics", {})
    metrics.update({"block_types": dict(Counter(b["type"] for b in blocks)),
                    "body_characters": sum(len(b.get("text", "")) for b in blocks if b["type"] == "paragraph"),
                    "inline_math_representations": sum(len(b.get("inline_math", [])) for b in blocks)})
    document_blob = _blob(store, (canonical_json(document) + "\n").encode(), "json", "application/json")
    markdown_blob = _blob(store, markdown(document).encode(), "md", "text/markdown")
    artifacts += [{"kind": "json", "blob": document_blob,
                   "metadata": {"source_sha256": parsed_from["blob"]["sha256"], "parser_key": parser_key}},
                  {"kind": "markdown", "blob": markdown_blob,
                   "metadata": {"source_sha256": document_blob["sha256"], "parser_key": parser_key}},
                  {"kind": "metadata", "blob": metadata["metadata_blob"],
                   "metadata": {"download_id": metadata["metadata_download_id"]}}]
    artifacts.extend(metadata.get("metadata_evidence_artifacts", []))
    version_id = str(uuid.uuid4())
    with store.transaction() as connection:
        store.assert_lease(connection, lease)
        connection.execute(
            "INSERT INTO paper_text_versions(version_id, work_id, arxiv_version, parser_version, "
            "include_pdf, metadata_json, quality_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, metadata["arxiv_id"], metadata["version"], parser_key, int(has_pdf),
             canonical_json(metadata), canonical_json(quality), float(store.clock())),
        )
        for artifact in artifacts:
            connection.execute(
                "INSERT INTO paper_text_artifacts(version_id, kind, blob_sha256, metadata_json) "
                "VALUES (?, ?, ?, ?)",
                (version_id, artifact["kind"], artifact["blob"]["sha256"], canonical_json(artifact["metadata"])),
            )
        connection.execute(
            "INSERT INTO paper_text_current(work_id, arxiv_version, parser_version, include_pdf, version_id) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(work_id, arxiv_version, parser_version, include_pdf) "
            "DO UPDATE SET version_id = excluded.version_id",
            (metadata["arxiv_id"], metadata["version"], parser_key, int(has_pdf), version_id),
        )
    return _version(store, version_id, verify=True)


def _rows(store: LibraryStore, run_id: str) -> list[dict]:
    return store.read("SELECT * FROM paper_inputs WHERE run_id = ? ORDER BY ordinal", (run_id,))


def _set_input(store: LibraryStore, run_id: str, ordinal: int, lease: Lease, **updates) -> None:
    allowed = {"pinned_version", "metadata_json", "status", "version_id", "result_json"}
    if not updates or not set(updates).issubset(allowed):
        raise ValueError("invalid paper checkpoint fields")
    with store.transaction() as connection:
        store.assert_lease(connection, lease)
        connection.execute(
            "UPDATE paper_inputs SET " + ", ".join(f"{key} = ?" for key in updates)
            + " WHERE run_id = ? AND ordinal = ?",
            (*updates.values(), run_id, ordinal),
        )


def _snapshot(store: LibraryStore, root: Path, run_id: str, contract: dict,
              lease: Lease, *, running: bool) -> dict:
    rows = _rows(store, run_id)
    counts = Counter(row["status"] for row in rows)
    entries = []
    incomplete = False
    for row in rows:
        result = json.loads(row["result_json"])
        incomplete |= row["status"] not in _SUCCESS or not result.get("acquisition_complete", False)
        entries.append({"ordinal": row["ordinal"], "input": json.loads(row["input_json"]),
                        "requested_id": row["requested_id"], "work_id": row["work_id"],
                        "pinned_version": row["pinned_version"], "status": row["status"],
                        "version_id": row["version_id"], **result})
    status = "running" if running else "partial" if incomplete or counts.get("partial") else "completed"
    manifest = {"schema_version": "1.0", "kind": "paper_corpus", "label": root.name,
                "run_id": run_id, "active_run_id": run_id, "status": status,
                "library_root": str(store.root), "updated_at": _now(), "contract": contract,
                "n_inputs": len(rows), "n_unique_papers": len({r['work_id'] for r in rows}),
                "counts": dict(counts), "n_fulltext": sum(counts.get(s, 0) for s in _SUCCESS),
                "n_downloaded": sum(any(a["kind"] in {"html", "source", "pdf"} for a in e.get("artifacts", [])) for e in entries),
                "papers": entries}
    metadata = {"kind": "paper_corpus", "published": not running,
                "n_papers": manifest["n_unique_papers"], "n_inputs": len(rows),
                "n_fulltext": manifest["n_fulltext"]}
    with store.transaction() as connection:
        store.assert_lease(connection, lease)
        atomic_json(_run_dir(root, run_id) / "manifest.json", manifest)
        atomic_json(root / "manifest.json", manifest)
        connection.execute(
            "UPDATE runs SET status = ?, finished_at = ?, metadata_json = ? WHERE run_id = ?",
            (status, None if running else float(store.clock()), canonical_json(metadata), run_id),
        )
    return manifest


def build_paper_corpus(
    label: str, papers: Sequence[str | Mapping[str, Any]] | None = None, *,
    include_pdf: bool = True, refresh: bool = False, resume_run_id: str | None = None,
    output_dir: Path | str | None = None, store: LibraryStore | None = None,
    client: Any = None,
) -> dict:
    """Build up to 50 papers explicitly; repeated identical requests resume their pinned run."""
    from astro_pipeline.library import get_library
    from astro_pipeline.sources.paper_fulltext import ArxivClient, PaperUnavailable

    root = _root(label, output_dir)
    selected = _selection(papers) if papers is not None else None
    if refresh and resume_run_id:
        raise ValueError("refresh and resume_run_id are mutually exclusive")
    if resume_run_id:
        resume_run_id = _run_id(resume_run_id)
    store = store if store is not None else get_library(create=False)
    if store is None:
        raise RuntimeError("paper full text requires the shared library to be enabled")
    if root.exists() and not (root / "manifest.json").is_file():
        raise ValueError("refusing a nonempty or unmanaged paper corpus directory")
    parser_key = _parser_key()
    contract_options = {"kind": "paper_corpus", "include_pdf": bool(include_pdf), "parser_key": parser_key}
    label_key = identity_hash(str(root), namespace=_COLLECTION_NAMESPACE)
    with store.hold_lease(_COLLECTION_NAMESPACE, label_key,
                          ttl=store.lease_seconds, wait_timeout=store.wait_timeout_seconds) as collection_lease:
        previous = json.loads((root / "manifest.json").read_text()) if (root / "manifest.json").is_file() else None
        if previous and (previous.get("kind") != "paper_corpus"
                         or previous.get("label") != label):
            raise ValueError("refusing an unmanaged or mismatched paper manifest")
        if previous:
            _run_dir(root, previous["run_id"])
        if previous and previous.get("library_root") != str(store.root):
            raise ValueError("paper corpus belongs to a different library")
        if resume_run_id:
            path = _run_dir(root, resume_run_id) / "manifest.json"
            if not path.is_file():
                raise ValueError("unknown paper corpus run")
            if previous and previous["active_run_id"] != resume_run_id:
                raise ValueError("only the active run can be resumed; historical runs stay pinned")
            previous = json.loads(path.read_text())
        if previous:
            previous_contract = previous["contract"]
            if selected is not None and selected != previous_contract["selection"]:
                raise ValueError("label already has a different paper selection; use a new label")
            selected = previous_contract["selection"]
            if not refresh and any(previous_contract[k] != v for k, v in contract_options.items()):
                raise ValueError("parser/options changed; use refresh=True for a new immutable run")
        if selected is None:
            raise ValueError("provide papers for a new collection")
        contract = {**contract_options, "selection": selected}
        if previous and not refresh:
            run_id = previous["run_id"]
            known = store.read("SELECT run_id FROM runs WHERE run_id = ?", (run_id,))
            if not known:
                raise ValueError("paper run is missing from its shared library")
        else:
            corpus_id = store.register_corpus(root, label)
            request_id = store.register_request(contract)
            run_id = store.start_run(corpus_id, request_id,
                                     options={**contract_options, "refresh_requested": bool(refresh)})
            with store.transaction() as connection:
                store.assert_lease(connection, collection_lease)
                for item in selected:
                    connection.execute(
                        "INSERT INTO paper_inputs(run_id, ordinal, input_json, work_id, requested_id, "
                        "metadata_json, status, result_json) VALUES (?, ?, ?, ?, ?, '{}', 'pending', '{}')",
                        (run_id, item["ordinal"], canonical_json(item["input"]), item["work_id"], item["requested_id"]),
                    )
                atomic_json(_run_dir(root, run_id) / "selection.json", selected)
        run_record = store.read("SELECT options_json, started_at FROM runs WHERE run_id = ?", (run_id,))[0]
        refresh_requested = bool(json.loads(run_record["options_json"]).get("refresh_requested", False))
        not_before = run_record["started_at"] if refresh_requested else None
        _snapshot(store, root, run_id, contract, collection_lease, running=True)
        client_context = ArxivClient() if client is None else nullcontext(client)
        # A single shared-library lease also prevents independent builds from
        # multiplying the provider's per-client request rate.
        with store.hold_lease("arxiv-network", "fulltext", ttl=store.lease_seconds,
                              wait_timeout=store.wait_timeout_seconds), client_context as active_client:
            refreshed: set[tuple[str, int]] = set()
            resolved_ids: set[str] = set()
            for row in _rows(store, run_id):
                old_result = json.loads(row["result_json"])
                if row["version_id"] and old_result.get("acquisition_complete"):
                    try:
                        if _version(store, row["version_id"], verify=True):
                            continue
                    except BlobIntegrityError:
                        pass
                with store.hold_lease(_NAMESPACE, row["work_id"], ttl=store.lease_seconds,
                                      wait_timeout=store.wait_timeout_seconds) as paper_lease:
                    try:
                        base = _version(store, row["version_id"]) if row["version_id"] else None
                        if row["version_id"] and base is None:
                            raise BlobIntegrityError("the input's pinned text version is missing")
                        if base:
                            if base["arxiv_id"] != row["work_id"] or base["arxiv_version"] != row["pinned_version"]:
                                raise BlobIntegrityError("the pinned text version does not match its input")
                            base = _restore_pinned_version(store, active_client, base, paper_lease, run_id)
                            metadata = base["metadata"]
                        else:
                            metadata = json.loads(row["metadata_json"])
                            if metadata:
                                try:
                                    store.blobs.verify(metadata["metadata_blob"])
                                    for evidence in metadata.get("metadata_evidence_artifacts", []):
                                        store.blobs.verify(evidence["blob"])
                                except BlobIntegrityError:
                                    metadata = _resolve(store, active_client, metadata["versioned_id"],
                                                        paper_lease, run_id, True)
                                    _set_input(store, run_id, row["ordinal"], collection_lease,
                                               metadata_json=canonical_json(metadata))
                            else:
                                metadata = _resolve(store, active_client, row["requested_id"], paper_lease,
                                                    run_id, refresh_requested and row["requested_id"] not in resolved_ids)
                                resolved_ids.add(row["requested_id"])
                                _set_input(store, run_id, row["ordinal"], collection_lease,
                                           pinned_version=metadata["version"], metadata_json=canonical_json(metadata),
                                           status="resolved")
                        version_key = (metadata["arxiv_id"], metadata["version"])
                        refresh_this = refresh and version_key not in refreshed
                        if base:
                            value = base if not include_pdf or base["include_pdf"] else None
                        else:
                            value = None if refresh_this else _cached_version(
                                store, metadata, parser_key, include_pdf, not_before=not_before)
                        reused = value is not None
                        if value is None:
                            value = _acquire_version(store, active_client, metadata, parser_key, include_pdf,
                                                     refresh_this, paper_lease, run_id,
                                                     base=base, not_before=not_before)
                        refreshed.add(version_key)
                        result = {"arxiv_id": metadata["arxiv_id"], "versioned_id": metadata["versioned_id"],
                                  "title": metadata.get("title"), "license": metadata.get("license"),
                                  "quality": value["quality"], "artifacts": value["artifacts"],
                                  "acquisition_complete": not include_pdf or value["include_pdf"],
                                  "cache_reused": reused}
                        _set_input(store, run_id, row["ordinal"], collection_lease,
                                   status=value["quality"]["status"], version_id=value["version_id"],
                                   result_json=canonical_json(result))
                    except (LeaseLostError, KeyboardInterrupt):
                        raise
                    except Exception as exc:
                        result = {**old_result, "error": f"{type(exc).__name__}: {exc}",
                                  "acquisition_complete": False}
                        if isinstance(exc, _NoUsableText):
                            result["artifacts"] = exc.artifacts
                        _set_input(store, run_id, row["ordinal"], collection_lease,
                                   status="unavailable" if isinstance(exc, PaperUnavailable) else "failed",
                                   result_json=canonical_json(result))
                _snapshot(store, root, run_id, contract, collection_lease, running=True)
        _snapshot(store, root, run_id, contract, collection_lease, running=False)
    return paper_corpus_status(label, output_dir=output_dir, store=store)


def paper_corpus_status(label: str, *, output_dir: Path | str | None = None,
                        store: LibraryStore | None = None, run_id: str | None = None) -> dict:
    """Read collection checkpoints without opening a network connection or migrating SQLite."""
    root = _root(label, output_dir)
    path = _run_dir(root, run_id) / "manifest.json" if run_id else root / "manifest.json"
    if not path.is_file():
        return {"exists": False, "label": root.name, "path": str(root)}
    manifest = json.loads(path.read_text())
    return {"exists": True, "path": str(root), "manifest_path": str(path), **manifest}


def verify_paper_text(store: LibraryStore) -> list[dict]:
    """Check paper-specific identity and snapshot references without repairs."""
    if not _has_schema(store):
        return []
    errors = []
    invalid = store.read(
        "SELECT i.run_id, i.ordinal FROM paper_inputs i LEFT JOIN paper_text_versions v "
        "ON v.version_id = i.version_id WHERE "
        "(i.status IN ('parsed', 'partial') AND v.version_id IS NULL) OR "
        "(v.version_id IS NOT NULL AND (v.work_id != i.work_id OR v.arxiv_version != i.pinned_version))"
    )
    errors.extend({**row, "error": "paper input and pinned text version disagree"} for row in invalid)
    for row in store.read("SELECT version_id FROM paper_text_versions"):
        try:
            _version(store, row["version_id"])
        except (BlobIntegrityError, ValueError) as exc:
            errors.append({**row, "error": str(exc)})
    runs = store.read(
        "SELECT r.run_id, r.status, c.canonical_path FROM runs r JOIN corpora c "
        "ON c.corpus_id = r.corpus_id WHERE json_extract(r.options_json, '$.kind') = 'paper_corpus'"
    )
    for run in runs:
        if run["status"] == "running":
            continue
        path = Path(run["canonical_path"]) / "runs" / run["run_id"] / "manifest.json"
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            rows = _rows(store, run["run_id"])
            expected = [(r["ordinal"], r["work_id"], r["pinned_version"], r["version_id"], r["status"]) for r in rows]
            actual = [(r["ordinal"], r["work_id"], r["pinned_version"], r["version_id"], r["status"]) for r in snapshot["papers"]]
            if snapshot["run_id"] != run["run_id"] or actual != expected:
                raise ValueError("paper snapshot does not match the indexed run")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"run_id": run["run_id"], "path": str(path), "error": str(exc)})
    return errors


def lookup_paper_text(store: LibraryStore | None, query: str, *, limit: int = 50) -> dict:
    """List cached paper versions and local artifact paths, never full-text blocks."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not _has_schema(store):
        return {"available": False, "schema_ready": False, "versions": []}
    text = str(query).strip()
    if not text:
        raise ValueError("paper query must not be blank")
    from astro_pipeline.sources.paper_fulltext import split_arxiv_id
    from astro_pipeline.library.papers import normalize_doi

    doi_query = normalize_doi(text)
    try:
        work_id, arxiv_version = split_arxiv_id(text)
    except ValueError:
        work_id, arxiv_version = text, None
    rows = store.read(
        "SELECT DISTINCT v.version_id, v.created_at FROM paper_text_versions v "
        "LEFT JOIN paper_inputs i ON i.version_id = v.version_id WHERE "
        "(v.work_id = ? AND (? IS NULL OR v.arxiv_version = ?)) OR "
        "lower(json_extract(v.metadata_json, '$.doi')) = lower(?) OR "
        "lower(json_extract(i.input_json, '$.bibcode')) = lower(?) OR "
        "lower(json_extract(i.input_json, '$.doi')) = lower(?) OR "
        "EXISTS (SELECT 1 FROM json_each(i.input_json, '$.aliases') alias "
        "WHERE CASE WHEN alias.type = 'object' THEN "
        "lower(json_extract(alias.value, '$.bibcode')) END = lower(?)) OR "
        "instr(lower(json_extract(v.metadata_json, '$.title')), lower(?)) > 0 "
        "ORDER BY v.created_at DESC, v.version_id LIMIT ?",
        (work_id, arxiv_version, arxiv_version, doi_query, text, doi_query, text, text, limit + 1),
    )
    versions = [_version(store, r["version_id"]) for r in rows[:limit]]
    return {"available": bool(versions), "schema_ready": True,
            "versions": versions, "truncated": len(rows) > limit}
