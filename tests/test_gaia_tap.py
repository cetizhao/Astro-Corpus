"""Offline transport tests; no Gaia service or credentials are required."""
from __future__ import annotations

import asyncio
import io
import time
from urllib.parse import parse_qs

from astropy.table import Table
import httpx
import pytest

from astro_pipeline.config import GaiaConfig, NetworkConfig
from astro_pipeline.gaia_tap import GaiaTapClient, GaiaTapOverflowError, SYNC_MAXREC

BASE = "https://gea.esac.esa.int/tap-server/tap"
JOB = BASE + "/async/test-job"
ADQL = "SELECT source_id FROM gaiadr3.gaia_source WHERE source_id = 5401795662560500352"
AsyncClient = httpx.AsyncClient


def votable(status=None, *, before=False):
    buffer = io.BytesIO()
    Table({"source_id": [5401795662560500352]}).write(buffer, format="votable")
    payload = buffer.getvalue().replace(b'ID="source_id"', b'ID="archive_column_0"')
    if status:
        info = f'<INFO name="QUERY_STATUS" value="{status}">test detail</INFO>'.encode()
        if before:
            payload = payload.replace(b"<TABLE", info + b"<TABLE", 1)
        else:
            payload = payload.replace(b"</RESOURCE>", info + b"</RESOURCE>")
    return payload


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr("astro_pipeline.gaia_tap.POLL_INTERVAL_SECONDS", 0.001)

    def install(handler, *, budget=2, attempts=1, delay=0, request_timeout=1, tap_url=BASE):
        requests = []
        options = []

        async def dispatch(request):
            requests.append(request)
            result = handler(request)
            return await result if asyncio.iscoroutine(result) else result

        def client(**kwargs):
            options.append(kwargs)
            return AsyncClient(transport=httpx.MockTransport(dispatch), **kwargs)

        monkeypatch.setattr("astro_pipeline.gaia_tap.httpx.AsyncClient", client)
        tap = GaiaTapClient(
            GaiaConfig(tap_url=tap_url, default_query_timeout=budget),
            NetworkConfig(
                request_timeout_seconds=request_timeout,
                retry_max_attempts=attempts,
                retry_base_delay_seconds=delay,
            ),
        )
        return tap, requests, options

    return install


def async_response(request, *, phase="COMPLETED", payload=None):
    if request.method == "DELETE":
        return httpx.Response(204)
    if request.url.path.endswith("/async"):
        return httpx.Response(303, headers={"Location": JOB})
    if request.url.path.endswith("/phase"):
        return httpx.Response(200, text=phase)
    assert str(request.url) == JOB + "/results/result"
    return httpx.Response(200, content=payload if payload is not None else votable())


@pytest.mark.parametrize("tap_url", [BASE, BASE.removesuffix("/tap")])
def test_small_query_is_sync_uses_named_columns_and_environment_proxy(transport, tap_url):
    tap, requests, options = transport(
        lambda request: httpx.Response(200, content=votable()), tap_url=tap_url,
    )
    result = tap.query(ADQL)
    assert result["source_id"].tolist() == [5401795662560500352]
    assert len(requests) == 1
    assert str(requests[0].url) == BASE + "/sync"
    data = parse_qs(requests[0].content.decode())
    assert data["QUERY"] == [ADQL]
    assert data["MAXREC"] == [str(SYNC_MAXREC)]
    assert data["LANG"] == ["ADQL"]
    assert options[0].get("trust_env", True) is True
    assert options[0]["timeout"].read == 1
    assert options[0]["timeout"].connect == 1


def test_async_lifecycle_never_calls_sync(transport):
    phases = iter(["QUEUED", "EXECUTING", "COMPLETED"])

    def handler(request):
        if request.url.path.endswith("/phase"):
            return httpx.Response(200, text=next(phases))
        return async_response(request)

    tap, requests, _ = transport(handler)
    result = tap.query(ADQL, asynchronous=True)
    assert len(result) == 1
    assert requests[0].method == "POST"
    assert parse_qs(requests[0].content.decode())["PHASE"] == ["RUN"]
    assert "MAXREC" not in parse_qs(requests[0].content.decode())
    assert all(not r.url.path.endswith("/sync") for r in requests)
    assert requests[-1].method == "DELETE"


def test_pending_job_is_started_once(transport):
    phases = iter(["PENDING", "PENDING", "COMPLETED"])

    def handler(request):
        if request.url.path.endswith("/phase"):
            if request.method == "POST":
                return httpx.Response(303, headers={"Location": JOB})
            return httpx.Response(200, text=next(phases))
        if str(request.url) == JOB and request.method == "GET":
            return httpx.Response(200, text="job summary")
        return async_response(request)

    tap, requests, _ = transport(handler)
    assert len(tap.query(ADQL, asynchronous=True)) == 1
    starts = [r for r in requests if r.method == "POST" and r.url.path.endswith("/phase")]
    assert len(starts) == 1


@pytest.mark.parametrize("phase", ["ERROR", "ABORTED", "UNKNOWN"])
def test_terminal_or_invalid_job_state_is_not_empty_data(transport, phase):
    tap, requests, _ = transport(lambda r: async_response(r, phase=phase))
    with pytest.raises(RuntimeError, match=phase):
        tap.query(ADQL, asynchronous=True)
    assert requests[-1].method == "DELETE"
    assert all(not r.url.path.endswith("/sync") for r in requests)


@pytest.mark.parametrize("status", ["ERROR", "OVERFLOW"])
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_votable_error_or_overflow_before_or_after_table_is_rejected(
    transport, status, before, asynchronous,
):
    payload = votable(status, before=before)

    def handler(request):
        if request.url.path.endswith("/sync"):
            return httpx.Response(200, content=payload)
        return async_response(request, payload=payload)

    tap, requests, _ = transport(handler, attempts=3)
    error = GaiaTapOverflowError if status == "OVERFLOW" else RuntimeError
    with pytest.raises(error, match="OVERFLOW|test detail"):
        tap.query(ADQL, asynchronous=asynchronous)
    assert sum(r.method == "POST" for r in requests) == 1


def test_html_response_is_not_an_empty_catalogue(transport):
    tap, _, _ = transport(lambda r: httpx.Response(200, text="<html>Unavailable"))
    with pytest.raises(ValueError, match="VOTable"):
        tap.query(ADQL)


def test_job_deadline_bounds_successful_but_endless_phase_polling(transport):
    tap, requests, _ = transport(
        lambda r: async_response(r, phase="QUEUED"), budget=0.03,
    )
    start = time.monotonic()
    with pytest.raises(TimeoutError, match="total deadline.*test-job"):
        tap.query(ADQL, asynchronous=True)
    assert time.monotonic() - start < 1
    assert sum(r.method == "POST" for r in requests) == 1
    assert requests[-1].method == "DELETE"


@pytest.mark.parametrize("stage", ["/phase", "/results/result"])
def test_job_deadline_cancels_stalled_phase_or_result_request(transport, stage):
    cancelled = []

    async def handler(request):
        if request.url.path.endswith(stage):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return async_response(request)

    tap, requests, _ = transport(handler, budget=0.03)
    with pytest.raises(TimeoutError, match="total deadline"):
        tap.query(ADQL, asynchronous=True)
    assert cancelled == [True]
    assert requests[-1].method == "DELETE"


def test_stalled_submission_is_not_resubmitted(transport):
    async def handler(request):
        await asyncio.sleep(60)
        return async_response(request)

    tap, requests, _ = transport(handler, budget=0.03, attempts=5)
    with pytest.raises(TimeoutError, match="total deadline"):
        tap.query(ADQL, asynchronous=True)
    assert len(requests) == 1


def test_per_request_budget_is_separate_from_query_deadline(transport):
    async def handler(request):
        await asyncio.sleep(60)
        return httpx.Response(200)

    tap, requests, _ = transport(handler, budget=5, request_timeout=1)
    start = time.monotonic()
    with pytest.raises(httpx.ReadTimeout, match="request exceeded 1s"):
        tap.query(ADQL)
    assert time.monotonic() - start < 3
    assert len(requests) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_retryable_http_failure_recovers_within_budget(transport, status):
    responses = iter([httpx.Response(status), httpx.Response(200, content=votable())])
    tap, requests, _ = transport(lambda r: next(responses), attempts=3)
    assert len(tap.query(ADQL)) == 1
    assert len(requests) == 2


def test_retry_delay_does_not_reset_total_deadline(transport):
    tap, requests, _ = transport(
        lambda r: httpx.Response(503), budget=0.03, attempts=5, delay=2,
    )
    with pytest.raises(TimeoutError, match="total deadline"):
        tap.query(ADQL)
    assert len(requests) == 1


def test_permanent_http_error_is_not_retried(transport):
    tap, requests, _ = transport(lambda r: httpx.Response(400), attempts=3)
    with pytest.raises(httpx.HTTPStatusError):
        tap.query(ADQL)
    assert len(requests) == 1


def test_transient_phase_failure_reuses_existing_job(transport):
    phases = 0

    def handler(request):
        nonlocal phases
        if request.url.path.endswith("/phase"):
            phases += 1
            if phases == 1:
                raise httpx.ReadTimeout("phase unavailable")
        return async_response(request)

    tap, requests, _ = transport(handler, attempts=3)
    assert len(tap.query(ADQL, asynchronous=True)) == 1
    assert phases == 2
    assert sum(r.method == "POST" for r in requests) == 1


@pytest.mark.parametrize("failure", ["timeout", "http_error"])
def test_cleanup_failure_is_bounded_and_does_not_discard_result(
    transport, monkeypatch, caplog, failure,
):
    monkeypatch.setattr("astro_pipeline.gaia_tap.CLEANUP_TIMEOUT_SECONDS", 0.02)

    async def handler(request):
        if request.method == "DELETE":
            if failure == "timeout":
                await asyncio.sleep(60)
            return httpx.Response(500)
        return async_response(request)

    tap, _, _ = transport(handler)
    assert len(tap.query(ADQL, asynchronous=True)) == 1
    assert "Could not confirm Gaia TAP job cleanup" in caplog.text


def test_query_can_be_called_from_an_existing_event_loop(transport):
    loops = []

    def handler(request):
        loops.append(asyncio.get_running_loop())
        return httpx.Response(200, content=votable())

    tap, _, _ = transport(handler)

    async def caller():
        current_loop = asyncio.get_running_loop()
        result = tap.query(ADQL)
        assert loops[0] is not current_loop
        return result

    assert len(asyncio.run(caller())) == 1


@pytest.mark.parametrize("location", [
    "https://other.example/tap/async/stolen",
    BASE + "/sync",
    BASE + "/async/",
    JOB + "?query=unexpected",
    BASE + "/async/%2e%2e",
    BASE + "/async/a%2fb",
    JOB.replace("https://", "https://user:secret@"),
])
def test_unexpected_job_location_is_not_followed(transport, location):
    tap, requests, _ = transport(
        lambda r: httpx.Response(303, headers={"Location": location}),
    )
    with pytest.raises(RuntimeError, match="job location"):
        tap.query(ADQL, asynchronous=True)
    assert len(requests) == 1


def test_archive_http_location_is_upgraded_to_https(transport):
    tap, _, _ = transport(lambda r: httpx.Response(200))
    assert tap._job_url(JOB.replace("https:", "http:")) == JOB


@pytest.mark.parametrize("field", ["default_query_timeout", "metadata_timeout_seconds"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_query_timeouts_must_be_positive_and_finite(field, value):
    with pytest.raises(ValueError):
        GaiaConfig(**{field: value})


def test_response_stream_cannot_extend_query_deadline_by_trickling_bytes(transport):
    closed = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b"x"

        async def aclose(self):
            closed.append(True)

    tap, _, _ = transport(
        lambda r: httpx.Response(200, stream=SlowStream()), budget=0.04,
    )
    with pytest.raises(TimeoutError, match="total deadline"):
        tap.query(ADQL)
    assert closed


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
def test_async_submission_retries_connection_failure_before_request_is_sent(transport, error):
    submissions = 0

    def handler(request):
        nonlocal submissions
        if request.url.path.endswith("/async"):
            submissions += 1
            if submissions == 1:
                raise error("connection not established")
        return async_response(request)

    tap, _, _ = transport(handler, attempts=3)
    assert len(tap.query(ADQL, asynchronous=True)) == 1
    assert submissions == 2


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_async_submission_does_not_retry_ambiguous_failure(transport, error):
    def handler(request):
        raise error("response lost after submission")

    tap, requests, _ = transport(handler, attempts=3)
    with pytest.raises(error):
        tap.query(ADQL, asynchronous=True)
    assert len(requests) == 1


@pytest.mark.parametrize("base,location", [
    (BASE.replace(".int/", ".int:443/"), JOB),
    (BASE, JOB.replace(".int/", ".int:443/")),
    (BASE, JOB.replace("https:", "http:").replace(".int/", ".int:80/")),
])
def test_job_location_accepts_equivalent_default_ports(transport, base, location):
    tap, _, _ = transport(lambda r: httpx.Response(200), tap_url=base)
    assert tap._job_url(location).startswith("https://gea.esac.esa.int")
    assert tap._job_url(location).endswith("/async/test-job")


def test_cleanup_error_does_not_mask_original_job_deadline(transport):
    def handler(request):
        if request.method == "DELETE":
            return httpx.Response(500)
        return async_response(request, phase="QUEUED")

    tap, _, _ = transport(handler, budget=0.03)
    with pytest.raises(TimeoutError, match="total deadline"):
        tap.query(ADQL, asynchronous=True)


def test_metadata_override_has_an_independent_shorter_budget(transport):
    async def handler(request):
        await asyncio.sleep(60)
        return httpx.Response(200)

    tap, requests, _ = transport(handler, budget=5)
    with pytest.raises(TimeoutError, match="0.02s total deadline"):
        tap.query("SELECT schema_name FROM TAP_SCHEMA.schemas", timeout=0.02)
    assert requests[0].url.path.endswith("/sync")


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_query_budget_override_is_rejected_before_network(transport, timeout):
    tap, requests, _ = transport(lambda r: pytest.fail("unexpected request"))
    with pytest.raises(ValueError, match="positive and finite"):
        tap.query(ADQL, timeout=timeout)
    assert requests == []


def test_tap_startup_does_not_import_networked_astroquery_gaia():
    import subprocess
    import sys

    code = """
import socket
import sys

def forbidden(*args, **kwargs):
    raise AssertionError('TAP startup attempted network access')

socket.socket.connect = forbidden
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
from astro_pipeline.config import GaiaConfig, NetworkConfig
from astro_pipeline.gaia_archive import probe_gaia_release, describe_gaia_table
from astro_pipeline.sources.gaia import GaiaSource
source = GaiaSource(GaiaConfig(), NetworkConfig())
assert source.qualified_table() == 'gaiadr3.gaia_source'
assert 'astroquery.gaia' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
