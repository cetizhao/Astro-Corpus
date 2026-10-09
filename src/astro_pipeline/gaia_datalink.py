"""Finite-timeout transport for astroquery Gaia DataLink requests.

Astroquery's TAP+ DataLink client uses :mod:`http.client` without passing a
socket timeout.  This module supplies a per-client connection factory that
keeps astroquery's login-cookie behavior while bounding each direct HTTP(S)
connection.  Routing intentionally remains the same direct Gaia Archive
routing used by astroquery; environment proxy support is not guaranteed here.
"""
from __future__ import annotations

import http.client
import importlib
import math
import threading
from types import SimpleNamespace
from typing import Any, Callable

_GAIA_IMPORT_LOCK = threading.Lock()

GAIA_ARCHIVE_HOST = "gea.esac.esa.int"
GAIA_ARCHIVE_ROOT = f"https://{GAIA_ARCHIVE_HOST}/"


class FiniteTimeoutConnectionHandler:
    """Create direct astroquery-compatible connections with a finite timeout."""

    def __init__(
        self,
        host: str,
        port: int,
        ssl_port: int,
        *,
        timeout_seconds: float,
    ) -> None:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("DataLink timeout_seconds must be finite and positive")
        self._host = str(host)
        self._port = int(port)
        self._ssl_port = int(ssl_port)
        self._timeout_seconds = timeout

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def get_connection(
        self,
        *,
        ishttps: bool = False,
        cookie: str | None = None,
        verbose: bool = False,
    ) -> http.client.HTTPConnection:
        del verbose
        if ishttps or cookie is not None:
            return self.get_connection_secure()
        return http.client.HTTPConnection(
            self._host,
            self._port,
            timeout=self._timeout_seconds,
        )

    def get_connection_secure(
        self,
        verbose: bool = False,
    ) -> http.client.HTTPSConnection:
        del verbose
        return http.client.HTTPSConnection(
            self._host,
            self._ssl_port,
            timeout=self._timeout_seconds,
        )


def import_gaia_client() -> Any:
    """Import astroquery without its singleton's unsolicited status request."""
    from astroquery.utils.tap.conn.tapconn import TapConn

    with _GAIA_IMPORT_LOCK:
        original = TapConn.execute_tapget
        owner = threading.get_ident()

        def without_notifications(connection, subcontext, *args, **kwargs):
            if threading.get_ident() == owner and subcontext == "notification?action=GetNotifications":
                return SimpleNamespace(status=204)
            return original(connection, subcontext, *args, **kwargs)

        # Astroquery constructs its public singleton at import, before callers
        # can supply show_server_messages=False or a finite connection handler.
        TapConn.execute_tapget = without_notifications
        try:
            return importlib.import_module("astroquery.gaia")
        finally:
            TapConn.execute_tapget = original


def _build_gaia_client(timeout_seconds: float) -> Any:
    """Build one Gaia client whose login and DataLink transports are bounded."""
    GaiaClass = import_gaia_client().GaiaClass
    from astroquery.utils.tap.conn.tapconn import TapConn
    from astroquery.utils.tap.core import TapPlus

    tap_connection = TapConn(
        ishttps=True,
        host=GAIA_ARCHIVE_HOST,
        server_context="tap-server",
        tap_context="tap",
        upload_context="Upload",
        table_edit_context="TableTool",
        data_context="data",
        datalink_context="datalink",
        port=443,
        sslport=443,
        connhandler=FiniteTimeoutConnectionHandler(
            GAIA_ARCHIVE_HOST,
            443,
            443,
            timeout_seconds=timeout_seconds,
        ),
    )
    data_connection = TapConn(
        ishttps=True,
        host=GAIA_ARCHIVE_HOST,
        server_context="data-server",
        tap_context="tap",
        upload_context="Upload",
        table_edit_context="TableTool",
        data_context="data",
        datalink_context="datalink",
        port=443,
        sslport=443,
        connhandler=FiniteTimeoutConnectionHandler(
            GAIA_ARCHIVE_HOST,
            443,
            443,
            timeout_seconds=timeout_seconds,
        ),
    )
    data_client = TapPlus(
        url=GAIA_ARCHIVE_ROOT,
        server_context="data-server",
        tap_context="tap",
        upload_context="Upload",
        table_edit_context="TableTool",
        data_context="data",
        datalink_context="datalink",
        connhandler=data_connection,
        use_names_over_ids=True,
    )
    return GaiaClass(
        gaia_tap_server=GAIA_ARCHIVE_ROOT,
        gaia_data_server=GAIA_ARCHIVE_ROOT,
        tap_server_context="tap-server",
        data_server_context="data-server",
        tap_plus_conn_handler=tap_connection,
        datalink_handler=data_client,
        show_server_messages=False,
    )


class GaiaDataLinkClient:
    """Lazy per-owner Gaia client sharing one authenticated DataLink session."""

    def __init__(
        self,
        timeout_seconds: float,
        *,
        client_factory: Callable[[float], Any] = _build_gaia_client,
    ) -> None:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("DataLink timeout_seconds must be finite and positive")
        self.timeout_seconds = timeout
        self._client_factory = client_factory
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(self.timeout_seconds)
        return self._client

    def login(self, *, user: str, password: str) -> None:
        self._get_client().login(user=user, password=password)

    def load_data(self, **kwargs: Any) -> Any:
        return self._get_client().load_data(**kwargs)


__all__ = [
    "FiniteTimeoutConnectionHandler",
    "GaiaDataLinkClient",
]
