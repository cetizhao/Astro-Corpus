"""Loopback-only standard-library HTTP server for the global library viewer."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from astro_pipeline.library.overview import (
    DEFAULT_LIMIT,
    InvalidCursor,
    LibraryOverview,
    SnapshotChanged,
    SourceFilters,
)
from astro_pipeline.library.store import LibraryStore

_LOOPBACK_HOST = "127.0.0.1"
_SOURCE_PATH = re.compile(r"\A/api/v1/source/([^/]+)/([^/]+)\Z")
_SPECTRUM_PATH = re.compile(r"\A/api/v1/spectrum/([^/]+)/([^/]+)\Z")
_EVIDENCE_PATH = re.compile(r"\A/api/v1/source/([^/]+)/([^/]+)/evidence\Z")
_CONTEXT_PATH = re.compile(r"\A/api/v1/paper/([^/]+)/context\Z")

_ASSET_PATHS = {
    "/": (Path(__file__).with_name("assets") / "library.html", "text/html; charset=utf-8"),
    "/assets/library.css": (
        Path(__file__).with_name("assets") / "library.css",
        "text/css; charset=utf-8",
    ),
    "/assets/library.js": (
        Path(__file__).with_name("assets") / "library.js",
        "text/javascript; charset=utf-8",
    ),
    "/assets/echarts.min.js": (
        Path(__file__).with_name("vendor") / "echarts.min.js",
        "text/javascript; charset=utf-8",
    ),
}
for _asset in ("evidence.js", "evidence.css", "research.js", "research.css"):
    _ASSET_PATHS[f"/assets/{_asset}"] = (
        Path(__file__).with_name("assets") / _asset,
        "text/javascript; charset=utf-8" if _asset.endswith(".js") else "text/css; charset=utf-8",
    )


class LibraryViewerServer(ThreadingHTTPServer):
    """A loopback-only HTTP server carrying one revision monitor."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: LibraryStore | None,
        *,
        poll_seconds: float = 5.0,
        research_dir: Path | None = None,
    ) -> None:
        host, port = server_address
        if host != _LOOPBACK_HOST:
            raise ValueError("library viewer host must be 127.0.0.1")
        poll_seconds = float(poll_seconds)
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be a finite value greater than zero")
        if int(port) < 0 or int(port) > 65535:
            raise ValueError("port must be between 0 and 65535")
        self.store = store
        self.overview = LibraryOverview(store)
        self.poll_seconds = poll_seconds
        self.research_dir = Path(research_dir) if research_dir is not None else (
            store.root.parent / "research_tasks" if store is not None else None
        )
        super().__init__((host, int(port)), LibraryViewerHandler)

    def server_close(self) -> None:
        try:
            self.overview.close()
        finally:
            super().server_close()


class LibraryViewerHandler(BaseHTTPRequestHandler):
    """Exact-route handler; there is deliberately no general file serving."""

    server: LibraryViewerServer
    protocol_version = "HTTP/1.1"
    server_version = "AstroLibraryViewer"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        # Keep CLI output useful without exposing request headers or filesystem data.
        super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch(send_body=False)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def _safe_headers(self, *, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )

    def _send_bytes(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        *,
        send_body: bool,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(int(status))
        self._safe_headers(content_type=content_type, content_length=len(payload))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if send_body:
            self.wfile.write(payload)

    def _send_json(
        self,
        status: int,
        value: Any,
        *,
        send_body: bool,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self._send_bytes(
            status,
            payload,
            "application/json; charset=utf-8",
            send_body=send_body,
            extra_headers=extra_headers,
        )

    def _error(
        self,
        status: int,
        code: str,
        message: str,
        *,
        send_body: bool,
        extra: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        error.update(dict(extra or {}))
        self._send_json(
            status,
            {"error": error},
            send_body=send_body,
            extra_headers=headers,
        )

    def _request_origin(self) -> tuple[str, int] | None:
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1:
            return None
        raw = hosts[0].strip()
        if not raw or any(character in raw for character in "\r\n\\"):
            return None
        try:
            parsed = urlsplit("//" + raw)
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.hostname != _LOOPBACK_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        actual_port = int(self.server.server_address[1])
        if port is not None and port != actual_port:
            return None
        return _LOOPBACK_HOST, actual_port

    def _trusted_request(self) -> bool:
        expected = self._request_origin()
        if expected is None:
            return False
        origins = self.headers.get_all("Origin") or []
        if not origins:
            return True
        if len(origins) != 1:
            return False
        try:
            origin = urlsplit(origins[0].strip())
            origin_port = origin.port
        except ValueError:
            return False
        if (
            origin.scheme != "http"
            or origin.hostname != expected[0]
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            return False
        if origin_port is None:
            origin_port = 80
        return origin_port == expected[1]

    def _method_not_allowed(self) -> None:
        send_body = self.command != "HEAD"
        if not self._trusted_request():
            self._error(
                HTTPStatus.FORBIDDEN,
                "forbidden_origin",
                "Host and Origin must identify this loopback server",
                send_body=send_body,
            )
            return
        self._error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "only GET and HEAD are supported",
            send_body=send_body,
            headers={"Allow": "GET, HEAD"},
        )

    @staticmethod
    def _query_values(query: str, allowed: set[str]) -> dict[str, str]:
        if len(query) > 4096:
            raise ValueError("query string is too long")
        parsed = parse_qs(query, keep_blank_values=True, strict_parsing=False)
        unknown = sorted(set(parsed).difference(allowed))
        if unknown:
            raise ValueError("unsupported query parameter(s): " + ", ".join(unknown))
        duplicate = sorted(key for key, values in parsed.items() if len(values) != 1)
        if duplicate:
            raise ValueError("query parameter(s) must occur once: " + ", ".join(duplicate))
        return {key: values[0] for key, values in parsed.items()}

    @staticmethod
    def _optional(values: Mapping[str, str], key: str) -> str | None:
        value = values.get(key)
        return None if value is None or value == "" else value

    @staticmethod
    def _limit(values: Mapping[str, str]) -> int:
        raw = values.get("limit")
        if raw is None or raw == "":
            return DEFAULT_LIMIT
        if not re.fullmatch(r"[0-9]+", raw):
            raise ValueError("limit must be an integer")
        return int(raw)

    @classmethod
    def _filters(cls, values: Mapping[str, str]) -> SourceFilters:
        return SourceFilters.create(
            q=cls._optional(values, "q"),
            release=cls._optional(values, "release"),
            corpus_id=cls._optional(values, "corpus_id"),
            scope=values.get("scope") or "all_indexed",
            spectrum_state=values.get("spectrum_state") or "all",
            evidence_state=values.get("evidence_state") or "all",
        )

    def _dispatch(self, *, send_body: bool) -> None:
        if not self._trusted_request():
            self._error(
                HTTPStatus.FORBIDDEN,
                "forbidden_origin",
                "Host and Origin must identify this loopback server",
                send_body=send_body,
            )
            return
        try:
            target = urlsplit(self.path)
            if target.scheme or target.netloc or target.fragment:
                raise ValueError("request target must be an origin-form path")
            path = target.path
            if path in _ASSET_PATHS:
                if target.query:
                    raise ValueError("static assets do not accept query parameters")
                self._serve_asset(path, send_body=send_body)
                return
            if path == "/api/v1/meta":
                values = self._query_values(target.query, set())
                del values
                payload = self.server.overview.meta()
                payload["poll_seconds"] = self.server.poll_seconds
                self._send_json(HTTPStatus.OK, payload, send_body=send_body)
                return
            if path == "/api/v1/research-tasks":
                from astro_pipeline.viz.research import research_snapshot

                self._query_values(target.query, set())
                self._send_json(HTTPStatus.OK, research_snapshot(self.server.research_dir), send_body=send_body)
                return
            if path in {"/api/v1/sources", "/api/v1/sky"}:
                values = self._query_values(
                    target.query,
                    {
                        "q",
                        "release",
                        "corpus_id",
                        "scope",
                        "spectrum_state",
                        "evidence_state",
                        "limit",
                        "cursor",
                        "revision",
                    },
                )
                kwargs = {
                    "filters": self._filters(values),
                    "limit": self._limit(values),
                    "cursor": self._optional(values, "cursor"),
                    "revision": self._optional(values, "revision"),
                }
                payload = (
                    self.server.overview.sources(**kwargs)
                    if path.endswith("sources")
                    else self.server.overview.sky(**kwargs)
                )
                self._send_json(HTTPStatus.OK, payload, send_body=send_body)
                return
            evidence_match = _EVIDENCE_PATH.fullmatch(path)
            context_match = _CONTEXT_PATH.fullmatch(path)
            if evidence_match or context_match:
                from astro_pipeline.library.evidence import get_paper_evidence_context, query_source_evidence

                allowed = {"revision", "limit", "offset"}
                allowed |= {"role", "assessment", "membership_status"} if evidence_match else {"block_id"}
                values = self._query_values(target.query, allowed)
                offset = values.get("offset") or "0"
                if not re.fullmatch(r"[0-9]+", offset):
                    raise ValueError("offset must be a non-negative integer")
                revision, _ = self.server.overview.monitor.require(self._optional(values, "revision"))
                paging = {"limit": self._limit(values), "offset": int(offset)}
                if evidence_match:
                    payload = query_source_evidence(
                        self.server.store, unquote(evidence_match.group(2)),
                        release=unquote(evidence_match.group(1)),
                        role=self._optional(values, "role"), assessment=self._optional(values, "assessment"),
                        membership_status=self._optional(values, "membership_status"), **paging,
                    )
                else:
                    block_id = self._optional(values, "block_id")
                    payload = get_paper_evidence_context(
                        self.server.store, unquote(context_match.group(1)),
                        block_ids=[block_id] if block_id else None, **paging,
                    )
                self.server.overview.monitor.ensure_unchanged(revision)
                payload["revision"] = revision
                self._send_json(HTTPStatus.OK, payload, send_body=send_body)
                return
            source_match = _SOURCE_PATH.fullmatch(path)
            if source_match is not None:
                values = self._query_values(target.query, {"revision"})
                release = unquote(source_match.group(1))
                source_id = unquote(source_match.group(2))
                payload = self.server.overview.source(
                    release,
                    source_id,
                    revision=self._optional(values, "revision"),
                )
                self._send_json(HTTPStatus.OK, payload, send_body=send_body)
                return
            spectrum_match = _SPECTRUM_PATH.fullmatch(path)
            if spectrum_match is not None:
                values = self._query_values(
                    target.query,
                    {"scope", "corpus_id", "run_id", "retrieval_type"},
                )
                from astro_pipeline.spectral_adapters import get_library_spectrum

                payload = get_library_spectrum(
                    self.server.store,
                    unquote(spectrum_match.group(1)),
                    unquote(spectrum_match.group(2)),
                    scope=values.get("scope") or "global",
                    corpus_id=self._optional(values, "corpus_id"),
                    run_id=self._optional(values, "run_id"),
                    retrieval_type=self._optional(values, "retrieval_type"),
                )
                if not isinstance(payload, dict):
                    raise RuntimeError("spectrum adapter returned a non-object payload")
                self._send_json(HTTPStatus.OK, payload, send_body=send_body)
                return
            self._error(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "unknown viewer route",
                send_body=send_body,
            )
        except SnapshotChanged as exc:
            self._error(
                HTTPStatus.CONFLICT,
                "snapshot_changed",
                str(exc),
                send_body=send_body,
                extra={"revision": exc.current_revision},
            )
        except (InvalidCursor, ValueError) as exc:
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                str(exc),
                send_body=send_body,
            )
        except ModuleNotFoundError as exc:
            if exc.name == "astro_pipeline.spectral_adapters":
                self._error(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "spectrum_unsupported",
                    "the read-only spectrum adapter is unavailable",
                    send_body=send_body,
                )
            else:
                self.log_error("viewer dependency is unavailable: %s", exc)
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "the viewer could not complete the read-only request",
                    send_body=send_body,
                )
        except (OSError, RuntimeError, sqlite3.Error, TypeError) as exc:
            self.log_error("viewer request failed: %s", exc)
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "the viewer could not complete the read-only request",
                send_body=send_body,
            )
        except Exception as exc:  # pragma: no cover - final HTTP safety boundary
            self.log_error("unexpected viewer request failure: %s", exc)
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "the viewer could not complete the read-only request",
                send_body=send_body,
            )

    def _serve_asset(self, path: str, *, send_body: bool) -> None:
        asset_path, content_type = _ASSET_PATHS[path]
        try:
            payload = asset_path.read_bytes()
        except FileNotFoundError:
            self._error(
                HTTPStatus.NOT_FOUND,
                "asset_not_found",
                "viewer asset is not installed",
                send_body=send_body,
            )
            return
        self._send_bytes(
            HTTPStatus.OK,
            payload,
            content_type,
            send_body=send_body,
        )


def create_library_viewer_server(
    store: LibraryStore | None,
    *,
    host: str = _LOOPBACK_HOST,
    port: int = 8765,
    poll_seconds: float = 5.0,
    research_dir: Path | None = None,
) -> LibraryViewerServer:
    """Create, but do not start, the loopback viewer server."""

    return LibraryViewerServer(
        (host, int(port)),
        store,
        poll_seconds=poll_seconds,
        research_dir=research_dir,
    )


def serve_library_viewer(
    store: LibraryStore | None,
    *,
    host: str = _LOOPBACK_HOST,
    port: int = 8765,
    poll_seconds: float = 5.0,
    research_dir: Path | None = None,
) -> None:
    """Serve until interrupted; constructing or serving never initializes SQLite."""

    server = create_library_viewer_server(
        store,
        host=host,
        port=port,
        poll_seconds=poll_seconds,
        research_dir=research_dir,
    )
    try:
        server.serve_forever(poll_interval=min(0.5, poll_seconds))
    finally:
        server.server_close()


__all__ = [
    "LibraryViewerHandler",
    "LibraryViewerServer",
    "create_library_viewer_server",
    "serve_library_viewer",
]
