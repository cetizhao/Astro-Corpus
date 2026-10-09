from __future__ import annotations

import logging

import pytest

from astro_pipeline import gaia_datalink
from astro_pipeline.config import GaiaConfig, NetworkConfig
from astro_pipeline.gaia_datalink import (
    FiniteTimeoutConnectionHandler,
    GaiaDataLinkClient,
)
from astro_pipeline.sources.gaia import GaiaSource


def test_connection_handler_forwards_finite_timeout_to_http_and_https() -> None:
    handler = FiniteTimeoutConnectionHandler(
        "gea.esac.esa.int",
        80,
        443,
        timeout_seconds=17.5,
    )

    plain = handler.get_connection()
    secure = handler.get_connection(ishttps=True)
    authenticated = handler.get_connection(cookie="session-cookie")

    try:
        assert plain.host == secure.host == authenticated.host == "gea.esac.esa.int"
        assert plain.port == 80
        assert secure.port == authenticated.port == 443
        assert plain.timeout == secure.timeout == authenticated.timeout == 17.5
    finally:
        plain.close()
        secure.close()
        authenticated.close()

    with pytest.raises(ValueError):
        FiniteTimeoutConnectionHandler(
            "gea.esac.esa.int",
            80,
            443,
            timeout_seconds=float("inf"),
        )


def test_datalink_login_and_load_use_one_lazy_archive_client() -> None:
    events: list[tuple] = []

    class FakeArchiveClient:
        def login(self, *, user, password):
            events.append(("login", id(self), user, password))

        def load_data(self, **kwargs):
            events.append(("load", id(self), kwargs))
            return {"result": []}

    archive_client = FakeArchiveClient()

    def factory(timeout_seconds: float):
        events.append(("build", timeout_seconds))
        return archive_client

    client = GaiaDataLinkClient(23, client_factory=factory)

    client.login(user="user", password="password")
    result = client.load_data(ids=["123"], retrieval_type="XP_SAMPLED")

    assert result == {"result": []}
    assert events == [
        ("build", 23.0),
        ("login", id(archive_client), "user", "password"),
        (
            "load",
            id(archive_client),
            {"ids": ["123"], "retrieval_type": "XP_SAMPLED"},
        ),
    ]


def test_gaia_source_forwards_timeout_and_reuses_login_client_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    instances = []
    events: list[tuple] = []

    class FakeDataLinkClient:
        def __init__(self, timeout_seconds):
            self.timeout_seconds = timeout_seconds
            instances.append(self)

        def login(self, *, user, password):
            events.append(("login", id(self), user, password))

        def load_data(self, **kwargs):
            events.append(("load", id(self), kwargs))
            return {"sample.fits": []}

    monkeypatch.setattr(gaia_datalink, "GaiaDataLinkClient", FakeDataLinkClient)
    username = "private-user-needle"
    password = "private-password-needle"
    source = GaiaSource(
        GaiaConfig(username=username, password=password),
        NetworkConfig(request_timeout_seconds=41),
    )

    with caplog.at_level(logging.INFO, logger="GaiaSource"):
        source._ensure_login()
        source._ensure_login()
        result = source._load_data(ids=["123"])

    assert result == {"sample.fits": []}
    assert len(instances) == 1
    assert instances[0].timeout_seconds == 41
    assert events == [
        ("login", id(instances[0]), username, password),
        ("load", id(instances[0]), {"ids": ["123"]}),
    ]
    assert username not in caplog.text
    assert password not in caplog.text


@pytest.mark.parametrize("retrieval_type,batch_size,expected", [
    ("XP_SAMPLED", None, 50), ("XP_CONTINUOUS", None, 5),
    ("XP_SAMPLED", 17, 17),
])
def test_xp_wrapper_uses_small_default_batches(monkeypatch, tmp_path, retrieval_type, batch_size, expected):
    source = GaiaSource(GaiaConfig(), NetworkConfig())
    seen = []

    def products(request, output_dir, **kwargs):
        seen.append(request.batch_size)
        return []

    monkeypatch.setattr(source, "download_products", products)
    source.download_spectra(["123"], tmp_path, retrieval_type=retrieval_type, batch_size=batch_size)
    assert seen == [expected]


def test_archive_client_initialization_is_offline_in_fresh_process():
    import subprocess
    import sys

    code = """
import http.client
from astroquery.utils.tap.conn.tapconn import TapConn
from astro_pipeline.gaia_datalink import _build_gaia_client

def no_connection(*args, **kwargs):
    raise AssertionError('Client initialization must not open network connections')

http.client.HTTPConnection.connect = no_connection
http.client.HTTPSConnection.connect = no_connection
original = TapConn.execute_tapget
client = _build_gaia_client(11)
assert client.VALID_DATALINK_RETRIEVAL_TYPES
assert TapConn.execute_tapget is original
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_notification_suppression_restores_transport_and_does_not_mask_data(monkeypatch):
    from astroquery.utils.tap.conn.tapconn import TapConn

    calls = []

    def original(connection, subcontext, **kwargs):
        calls.append(subcontext)
        return "data-result"

    def failed_import(name):
        assert name == "astroquery.gaia"
        assert TapConn.execute_tapget(None, "notification?action=GetNotifications").status == 204
        assert TapConn.execute_tapget(None, "science-data") == "data-result"
        responses = []
        thread = gaia_datalink.threading.Thread(target=lambda: responses.append(
            TapConn.execute_tapget(None, "notification?action=GetNotifications")
        ))
        thread.start()
        thread.join(timeout=2)
        assert responses == ["data-result"]
        raise ImportError("fixture import failed")

    monkeypatch.setattr(TapConn, "execute_tapget", original)
    monkeypatch.setattr(gaia_datalink.importlib, "import_module", failed_import)
    with pytest.raises(ImportError, match="fixture import failed"):
        gaia_datalink.import_gaia_client()
    assert TapConn.execute_tapget is original
    assert calls == ["science-data", "notification?action=GetNotifications"]
