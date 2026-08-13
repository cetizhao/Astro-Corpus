from __future__ import annotations

import logging

import httpx
import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

logger = logging.getLogger("astro_pipeline.network")

NETWORK_ERRORS = (
    ConnectionError,
    TimeoutError,
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    requests.ConnectionError,
    requests.Timeout,
)
RETRYABLE_HTTP_STATUS = {408, 425, 429}


def is_retryable_network_error(exc: BaseException) -> bool:
    """Retry transient transport failures and retryable HTTP status codes."""
    if isinstance(exc, NETWORK_ERRORS):
        return True
    status_code = None
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
    elif isinstance(exc, requests.HTTPError) and exc.response is not None:
        status_code = exc.response.status_code
    return bool(
        status_code in RETRYABLE_HTTP_STATUS
        or (status_code is not None and 500 <= status_code <= 599)
    )


def make_retry_decorator(network_config) -> retry:
    return retry(
        stop=stop_after_attempt(network_config.retry_max_attempts),
        wait=wait_exponential(
            multiplier=network_config.retry_base_delay_seconds,
            max=network_config.retry_max_delay_seconds,
        ),
        retry=retry_if_exception(is_retryable_network_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


retry_network = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, max=120),
    retry=retry_if_exception(is_retryable_network_error),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
