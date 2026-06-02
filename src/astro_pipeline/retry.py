from __future__ import annotations

import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
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
)


def make_retry_decorator(network_config) -> retry:
    return retry(
        stop=stop_after_attempt(network_config.retry_max_attempts),
        wait=wait_exponential(
            multiplier=network_config.retry_base_delay_seconds,
            max=network_config.retry_max_delay_seconds,
        ),
        retry=retry_if_exception_type(NETWORK_ERRORS),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


retry_network = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, max=120),
    retry=retry_if_exception_type(NETWORK_ERRORS),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
