import pytest
from audial.api import device_login
from audial.utils import config as sdk_config

import audial_mcp.server as server_mod


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_sign_in(tmp_path, monkeypatch):
    """No test reads the real sign-in, opens a browser or calls the real Audial API.

    The credentials file lives in a temporary folder, and the sign-in network calls fail
    loudly unless a test replaces them (see `fake_sign_in` in test_server.py).
    """
    home = tmp_path / "audial-home"
    home.mkdir()
    monkeypatch.setattr(sdk_config, "_get_config_file_path", lambda: home / ".audial_config.json")

    def no_network(*_args, **_kwargs):
        raise AssertionError("a test tried to reach the Audial API")

    monkeypatch.setattr(device_login.requests, "post", no_network)
    monkeypatch.setattr(device_login.requests, "get", no_network)
    monkeypatch.setattr(server_mod.webbrowser, "open", lambda _url: True)
    monkeypatch.setattr(server_mod, "_pending_sign_in", None)
    monkeypatch.setattr(server_mod, "_signed_in", None)
    return home
