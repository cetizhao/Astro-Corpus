"""Offline tests for transient-network retry classification."""
from __future__ import annotations

import httpx
import requests

from astro_pipeline.retry import is_retryable_network_error


def _requests_http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


def _httpx_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def test_requests_transport_and_throttle_errors_are_retryable():
    assert is_retryable_network_error(requests.ConnectionError("offline"))
    assert is_retryable_network_error(requests.Timeout("slow"))
    assert is_retryable_network_error(_requests_http_error(429))
    assert is_retryable_network_error(_requests_http_error(503))


def test_httpx_retryable_statuses_match_requests():
    assert is_retryable_network_error(_httpx_status_error(408))
    assert is_retryable_network_error(_httpx_status_error(500))


def test_permanent_client_errors_are_not_retried():
    assert not is_retryable_network_error(_requests_http_error(401))
    assert not is_retryable_network_error(_requests_http_error(404))
    assert not is_retryable_network_error(_httpx_status_error(403))
    assert not is_retryable_network_error(ValueError("invalid query"))
