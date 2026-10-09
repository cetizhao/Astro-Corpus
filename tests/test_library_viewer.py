from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import sys
import threading
import types

from astro_pipeline.library.store import LibraryStore
from astro_pipeline.viz.library_viewer import create_library_viewer_server


@contextmanager
def _running_server(store: LibraryStore | None):
    server = create_library_viewer_server(store, port=0, poll_seconds=2.5)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _request(
    server,
    method: str,
    path: str,
    *,
    host: str | None = None,
    origin: str | None = None,
):
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if host is not None:
        headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    headers_out = dict(response.getheaders())
    status = response.status
    connection.close()
    return status, headers_out, body


def _json(body: bytes):
    return json.loads(body.decode("utf-8"))


def _seed(store: LibraryStore, tmp_path: Path) -> None:
    corpus_id = store.register_corpus(tmp_path / "corpus", "sample")
    run_id = store.start_run(corpus_id)
    store.index_members(
        run_id,
        [
            {
                "source_id": "1",
                "gaia_release": "dr3",
                "RAICRS": 10.0,
                "DEICRS": 20.0,
            },
            {"source_id": "2", "gaia_release": "dr3"},
        ],
    )
    store.finish_run(
        run_id,
        "complete",
        metadata={"members_indexed": True, "published": True},
    )


def test_absent_http_meta_and_pages_do_not_create_database(tmp_path):
    root = tmp_path / "missing"
    with _running_server(LibraryStore(root)) as server:
        status, headers, body = _request(server, "GET", "/api/v1/meta")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert _json(body)["exists"] is False

        status, _, body = _request(server, "GET", "/api/v1/sources")
        assert status == 200
        assert _json(body)["sources"] == []

    assert not root.exists()


def test_http_sources_sky_detail_and_head(tmp_path):
    store = LibraryStore(tmp_path / "library")
    _seed(store, tmp_path)
    with _running_server(store) as server:
        status, _, body = _request(
            server,
            "GET",
            "/api/v1/sources?scope=current_datasets&limit=1",
        )
        assert status == 200
        first = _json(body)
        assert first["total"] == 2
        assert first["returned"] == 1
        assert first["next_cursor"]

        status, _, body = _request(
            server,
            "GET",
            "/api/v1/sources?scope=current_datasets&limit=1"
            f"&cursor={first['next_cursor']}&revision={first['revision']}",
        )
        assert status == 200
        assert _json(body)["returned"] == 1

        status, _, body = _request(server, "GET", "/api/v1/sky?limit=2")
        assert status == 200
        sky = _json(body)
        assert sky["total_sources"] == 2
        assert sky["total_positions"] == 1
        assert sky["missing_positions"] == 1
        assert sky["scanned_count"] == 2
        assert sky["returned"] == 1

        status, _, body = _request(server, "GET", "/api/v1/source/dr3/1")
        assert status == 200
        detail = _json(body)
        assert detail["resolved"] is True
        assert detail["dto"]["key"] == "dr3:1"

        status, headers, body = _request(server, "HEAD", "/api/v1/meta")
        assert status == 200
        assert body == b""
        assert int(headers["Content-Length"]) > 0


def test_http_rejects_invalid_parameters_routes_hosts_origins_and_methods(tmp_path):
    store = LibraryStore(tmp_path / "library")
    _seed(store, tmp_path)
    with _running_server(store) as server:
        port = server.server_address[1]
        invalid_requests = [
            "/api/v1/sources?limit=501",
            "/api/v1/sources?limit=1&limit=2",
            "/api/v1/sources?unknown=true",
            "/api/v1/sources?cursor=not-a-cursor",
            "/api/v1/source/dr3/not-digits",
        ]
        for path in invalid_requests:
            status, _, body = _request(server, "GET", path)
            assert status == 400, (path, body)
            assert _json(body)["error"]["code"] == "invalid_request"

        status, _, body = _request(
            server, "GET", "http://[invalid/api/v1/meta",
            host=f"127.0.0.1:{port}",
        )
        assert status == 400
        assert _json(body)["error"]["code"] == "invalid_request"

        traversal_paths = [
            "/assets/../library.py",
            "/assets/%2e%2e/library.py",
            "/../../etc/passwd",
            "/api/v1/not-real",
        ]
        for path in traversal_paths:
            status, _, body = _request(server, "GET", path)
            assert status == 404, (path, body)
            assert _json(body)["error"]["code"] == "not_found"

        status, _, body = _request(
            server,
            "GET",
            "http://evil.example/api/v1/meta",
        )
        assert status == 403
        assert _json(body)["error"]["code"] == "forbidden_origin"

        status, _, body = _request(
            server,
            "GET",
            "/api/v1/meta",
            host="evil.example",
        )
        assert status == 403
        assert _json(body)["error"]["code"] == "forbidden_origin"

        status, _, body = _request(
            server,
            "GET",
            "/api/v1/meta",
            origin="https://evil.example",
        )
        assert status == 403
        assert _json(body)["error"]["code"] == "forbidden_origin"

        status, headers, body = _request(server, "POST", "/api/v1/meta")
        assert status == 405
        assert headers["Allow"] == "GET, HEAD"
        assert _json(body)["error"]["code"] == "method_not_allowed"

        status, _, _ = _request(
            server,
            "GET",
            "/api/v1/meta",
            host=f"127.0.0.1:{port}",
            origin=f"http://127.0.0.1:{port}",
        )
        assert status == 200


def test_spectrum_endpoint_lazily_calls_dict_adapter_and_keeps_domain_states_http_200(
    tmp_path, monkeypatch
):
    module = types.ModuleType("astro_pipeline.spectral_adapters")
    calls = []

    def get_library_spectrum(
        store,
        release,
        source_id,
        *,
        scope="global",
        corpus_id=None,
        run_id=None,
        retrieval_type=None,
    ):
        calls.append(
            (store, release, source_id, scope, corpus_id, run_id, retrieval_type)
        )
        if source_id == "bad":
            raise ValueError("source_id must contain digits")
        return {
            "status": "not_downloaded",
            "release": release,
            "source_id": source_id,
            "product_type": retrieval_type,
            "scope": scope,
            "series": [],
        }

    module.get_library_spectrum = get_library_spectrum
    monkeypatch.setitem(sys.modules, "astro_pipeline.spectral_adapters", module)
    store = LibraryStore(tmp_path / "missing")

    with _running_server(store) as server:
        status, _, body = _request(
            server,
            "GET",
            "/api/v1/spectrum/dr3/123?scope=corpus&corpus_id=abc"
            "&run_id=run-1&retrieval_type=XP_SAMPLED",
        )
        assert status == 200
        assert _json(body)["status"] == "not_downloaded"
        assert calls == [
            (store, "dr3", "123", "corpus", "abc", "run-1", "XP_SAMPLED")
        ]

        status, _, body = _request(
            server, "GET", "/api/v1/spectrum/dr3/bad"
        )
        assert status == 400
        assert _json(body)["error"]["code"] == "invalid_request"


def test_static_allowlist_serves_vendor_only_and_has_safe_headers(tmp_path):
    with _running_server(LibraryStore(tmp_path / "missing")) as server:
        status, headers, body = _request(server, "GET", "/assets/echarts.min.js")
        assert status == 200
        assert body
        assert headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers["Cross-Origin-Resource-Policy"] == "same-origin"

        status, _, body = _request(server, "GET", "/assets/echarts.min.js?x=1")
        assert status == 400
        assert _json(body)["error"]["code"] == "invalid_request"


def test_server_refuses_non_loopback_bind(tmp_path):
    try:
        create_library_viewer_server(
            LibraryStore(tmp_path / "library"), host="0.0.0.0", port=0
        )
    except ValueError as exc:
        assert "127.0.0.1" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("non-loopback bind unexpectedly succeeded")
