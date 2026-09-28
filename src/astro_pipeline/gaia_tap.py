"""Bounded, anonymous Gaia TAP queries with explicit UWS job lifetimes."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import logging
import math
from urllib.parse import unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

import httpx
import pandas as pd
from astropy.io.votable import parse_single_table

from astro_pipeline.config import GaiaConfig, NetworkConfig
from astro_pipeline.retry import is_retryable_network_error

logger = logging.getLogger(__name__)

SYNC_MAXREC = 2000
POLL_INTERVAL_SECONDS = 2.0
CLEANUP_TIMEOUT_SECONDS = 15.0


class GaiaTapOverflowError(RuntimeError):
    """The Archive returned an incomplete query result."""


def _parse_result(payload: bytes) -> pd.DataFrame:
    try:
        for _, element in ET.iterparse(io.BytesIO(payload), events=("end",)):
            if (
                element.tag.rsplit("}", 1)[-1] == "INFO"
                and element.get("name", "").upper() == "QUERY_STATUS"
            ):
                status = element.get("value", "").upper()
                detail = (element.text or "").strip()
                if status == "OVERFLOW":
                    raise GaiaTapOverflowError(
                        "Gaia TAP returned OVERFLOW; refusing truncated results. "
                        "Use a smaller query or an asynchronous query."
                    )
                if status == "ERROR":
                    raise RuntimeError(f"Gaia TAP query failed: {detail[:500]}")
            element.clear()
    except ET.ParseError as exc:
        raise ValueError("Gaia TAP did not return a valid VOTable") from exc
    table = parse_single_table(io.BytesIO(payload)).to_table(use_names_over_ids=True)
    return table.to_pandas()


class GaiaTapClient:
    def __init__(self, config: GaiaConfig, network: NetworkConfig) -> None:
        self.config = config
        self.network = network
        base = config.tap_url.rstrip("/")
        self.base_url = base if base.endswith("/tap") else f"{base}/tap"
        parts = urlsplit(self.base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("Gaia TAP URL must be an HTTP(S) service URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Gaia TAP URL must not contain credentials, query or fragment")
        if network.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")

    def query(
        self,
        adql: str,
        *,
        asynchronous: bool = False,
        timeout: float | None = None,
    ) -> pd.DataFrame:
        budget = self.config.default_query_timeout if timeout is None else timeout
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("Gaia TAP query timeout must be positive and finite")

        def run() -> bytes:
            return asyncio.run(self._execute(adql, asynchronous, float(budget)))

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            payload = run()
        else:
            # Synchronous callers may live inside a notebook or agent event loop.
            with ThreadPoolExecutor(max_workers=1) as executor:
                payload = executor.submit(run).result()
        return _parse_result(payload)

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        retry: bool = True,
        connect_only: bool = False,
        **kwargs,
    ) -> httpx.Response:
        attempts = max(1, self.network.retry_max_attempts) if retry else 1
        request_timeout = float(self.network.request_timeout_seconds)
        for attempt in range(attempts):
            try:
                try:
                    async with asyncio.timeout(request_timeout):
                        response = await client.request(method, url, **kwargs)
                except TimeoutError as exc:
                    raise httpx.ReadTimeout(
                        f"Gaia TAP {method} request exceeded {request_timeout:g}s"
                    ) from exc
                if response.is_error:
                    response.raise_for_status()
                return response
            except Exception as exc:
                retryable = (
                    isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                    if connect_only else is_retryable_network_error(exc)
                )
                if attempt + 1 == attempts or not retryable:
                    raise
                delay = min(
                    self.network.retry_base_delay_seconds * 2**attempt,
                    self.network.retry_max_delay_seconds,
                )
                logger.warning(
                    "Retrying Gaia TAP %s after %s (attempt %d/%d)",
                    method, type(exc).__name__, attempt + 1, attempts,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _execute(self, adql: str, asynchronous: bool, budget: float) -> bytes:
        request_timeout = float(self.network.request_timeout_seconds)
        job: dict[str, str] = {}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(request_timeout, connect=min(10.0, request_timeout)),
            follow_redirects=True,
        ) as client:
            try:
                async with asyncio.timeout(budget):
                    data = {
                        "REQUEST": "doQuery", "LANG": "ADQL",
                        "FORMAT": "votable", "QUERY": adql,
                    }
                    if asynchronous:
                        return await self._async_query(client, data, job)
                    response = await self._request(
                        client, "POST", f"{self.base_url}/sync",
                        data={**data, "MAXREC": str(SYNC_MAXREC)},
                    )
                    return response.content
            except TimeoutError as exc:
                location = f"; job: {job['url']}" if job else ""
                raise TimeoutError(
                    f"Gaia TAP query exceeded {budget:g}s total deadline{location}"
                ) from exc
            finally:
                if job:
                    await self._cleanup(client, job["url"])

    def _job_url(self, location: str) -> str:
        url = urljoin(f"{self.base_url}/async", location)
        base = urlsplit(self.base_url)
        parts = urlsplit(url)
        if (
            base.scheme == "https" and (base.port or 443) == 443
            and parts.scheme == "http" and (parts.port or 80) == 80
            and parts.hostname == base.hostname
            and parts.username is None and parts.password is None
        ):
            parts = parts._replace(scheme="https", netloc=base.netloc)
        origin = (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        expected = (base.scheme, base.hostname, base.port or (443 if base.scheme == "https" else 80))
        prefix = base.path + "/async/"
        job_id = unquote(parts.path.removeprefix(prefix).rstrip("/"))
        if (
            origin != expected or parts.username is not None or parts.password is not None
            or not parts.path.startswith(prefix) or job_id in {"", ".", ".."}
            or "/" in job_id or "\\" in job_id or parts.query or parts.fragment
        ):
            raise RuntimeError("Gaia TAP returned an unexpected async job location")
        return parts.geturl().rstrip("/")

    async def _async_query(
        self, client: httpx.AsyncClient, data: dict, job: dict[str, str]
    ) -> bytes:
        # A timed-out submission may already have created a job on the Archive.
        # Retrying it blindly can enqueue duplicate jobs with unknown IDs.
        response = await self._request(
            client, "POST", f"{self.base_url}/async",
            data={**data, "PHASE": "RUN"}, follow_redirects=False, connect_only=True,
        )
        if response.status_code != 303 or not response.headers.get("location"):
            raise RuntimeError("Gaia TAP did not return a 303 async job location")
        job["url"] = self._job_url(response.headers["location"])
        url = job["url"]
        started = False
        while True:
            response = await self._request(client, "GET", f"{url}/phase")
            phase = response.text.strip().upper()
            if phase == "COMPLETED":
                result = await self._request(client, "GET", f"{url}/results/result")
                return result.content
            if phase in {"ERROR", "ABORTED"}:
                raise RuntimeError(f"Gaia TAP job ended in {phase}; job: {url}")
            if phase not in {"PENDING", "QUEUED", "EXECUTING", "HELD", "SUSPENDED"}:
                raise RuntimeError(f"Unexpected Gaia TAP job phase {phase!r}; job: {url}")
            if phase == "PENDING" and not started:
                await self._request(
                    client, "POST", f"{url}/phase", data={"PHASE": "RUN"},
                    follow_redirects=False, retry=False,
                )
                started = True
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _cleanup(self, client: httpx.AsyncClient, url: str) -> None:
        # Cleanup has its own small budget and must never discard a valid result
        # or hide the query error when the Archive is slow or unavailable.
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                response = await client.delete(url, follow_redirects=False)
                if response.status_code not in {200, 202, 204, 303, 404}:
                    response.raise_for_status()
                    raise RuntimeError(f"unexpected HTTP {response.status_code}")
        except Exception as exc:
            logger.warning(
                "Could not confirm Gaia TAP job cleanup (%s: %s); job: %s",
                type(exc).__name__, exc, url,
            )
