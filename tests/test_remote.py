"""Remote mode (`audial-mcp --http`): the tools over streamable HTTP, one account per request.

Everything runs in process: the ASGI app is driven through httpx2's ASGI transport, the API's
key check (`whoami`) is replaced by a fake verifier, file downloads by a fake requests session,
DNS by a fake resolver, and the SDK functions by fakes. Nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import audial
import httpx2
import pytest
from audial.api import auth as sdk_auth
from audial.api.exceptions import (
    AudialAuthError,
    AudialError,
    AudialLimitReached,
    SubscriptionRequiredError,
)
from audial.utils import config as sdk_config
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from pydantic import BeforeValidator
from requests.structures import CaseInsensitiveDict

import audial_mcp.server as server_mod
from audial_mcp import plain
from audial_mcp import remote as remote_mode
from audial_mcp.config import Settings
from audial_mcp.errors import REMOTE_AUTH_REJECTED, REMOTE_SUBSCRIPTION_REQUIRED
from audial_mcp.validation import ValidationError

PUBLIC = "https://mcp.example.test"
KEY_A = "aud_" + "a" * 16 + "_" + "A" * 32
KEY_B = "aud_" + "b" * 16 + "_" + "B" * 32
ACCOUNTS = {KEY_A: ("user-a", "a@example.test"), KEY_B: ("user-b", "b@example.test")}
WAV = b"RIFF" + b"\0" * 60
TOOLS = {
    "stem_split",
    "analyze",
    "segment",
    "audio_features",
    "tag_audio",
    "get_lyrics",
    "master",
    "generate_samples",
    "generate_midi",
    "generate_music",
    "sound2vital",
    "sound2serum",
    "text2vox",
    "pipeline_status",
    "service_status",
    "usage",
    "list_results",
    "account",
    "sign_in",
    "sign_out",
}


def access(key: str) -> AccessToken:
    user_id, email = ACCOUNTS[key]
    return AccessToken(
        token=key,
        client_id="audial-api-key",
        scopes=["audial"],
        subject=user_id,
        claims={"email": email},
    )


class FakeVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        return access(token) if token in ACCOUNTS else None


# --- downloads -------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.body = body
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i : i + chunk_size]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeWeb:
    """URL -> response, standing in for `requests.Session`; records every URL fetched."""

    def __init__(self):
        self.pages: dict[str, FakeResponse] = {}
        self.fetched: list[str] = []
        web = self

        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def get(self, url, **kwargs):
                assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
                web.fetched.append(url)
                return web.pages.get(url) or FakeResponse(404)

        self.Session = Session


# Hosts and the addresses the fake resolver gives them.
ADDRESSES = {
    "files.example.test": "93.184.216.34",
    "cdn.example.test": "93.184.216.35",
    "intranet.example.test": "10.0.0.5",
    "metadata.example.test": "169.254.169.254",
    "mapped.example.test": "::ffff:127.0.0.1",
}


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(remote_mode.requests, "Session", fake.Session)

    def getaddrinfo(host, *_args, **_kwargs):
        if host not in ADDRESSES:
            raise socket.gaierror("unknown host")
        address = ADDRESSES[host]
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(remote_mode.socket, "getaddrinfo", getaddrinfo)
    return fake


@pytest.fixture
def inputs(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    token = remote_mode._inputs_dir.set(folder)
    yield folder
    remote_mode._inputs_dir.reset(token)


def test_fetch_downloads_into_the_call_folder_with_a_usable_name(web, inputs):
    web.pages["https://files.example.test/a/My%20Song.wav?sig=1"] = FakeResponse(body=WAV)
    path = remote_mode.fetch(
        "https://files.example.test/a/My%20Song.wav?sig=1", frozenset({".wav"}), "audio file"
    )
    assert path.parent == inputs and path.name == "My_Song.wav" and path.read_bytes() == WAV


def test_fetch_names_a_file_object_from_its_hints_or_content_type(web, inputs):
    # A ChatGPT download link has no extension: the attached file's name says what it is.
    url = remote_mode.file_arg(
        {"download_url": "https://files.example.test/f/123", "file_id": "file-1",
         "file_name": "take 2.mp3", "mime_type": "audio/mpeg"}
    )  # fmt: skip
    web.pages["https://files.example.test/f/123"] = FakeResponse(body=WAV)
    assert remote_mode.fetch(url, frozenset({".mp3"}), "audio file").name == "take_2.mp3"
    # Without hints, the response's Content-Type decides.
    web.pages["https://files.example.test/f/456"] = FakeResponse(
        headers={"content-type": "audio/wav; charset=binary"}, body=WAV
    )
    path = remote_mode.fetch("https://files.example.test/f/456", frozenset({".wav"}), "audio file")
    assert path.suffix == ".wav"
    # The fragment carrying the hints is never requested.
    assert all("#" not in url for url in web.fetched)


@pytest.mark.parametrize(
    "value, message",
    [
        ("/Users/someone/song.wav", "https URL"),
        ("~/song.wav", "https URL"),
        ("http://files.example.test/a.wav", "https URL"),
        ("file:///etc/passwd", "https URL"),
        ("https://user:pw@files.example.test/a.wav", "user name or password"),
        ("https://intranet.example.test/a.wav", "not a public address"),
        ("https://metadata.example.test/latest/meta-data", "not a public address"),
        ("https://mapped.example.test/a.wav", "not a public address"),
        ("https://nowhere.example.test/a.wav", "could not be found"),
    ],
)
def test_fetch_refuses_local_paths_and_private_addresses(web, inputs, value, message):
    with pytest.raises(ValidationError, match=message):
        remote_mode.fetch(value, frozenset({".wav"}), "audio file")
    assert web.fetched == []


def test_fetch_checks_every_redirect_hop(web, inputs):
    web.pages["https://files.example.test/a.wav"] = FakeResponse(
        302, {"location": "https://cdn.example.test/real.wav"}
    )
    web.pages["https://cdn.example.test/real.wav"] = FakeResponse(body=WAV)
    path = remote_mode.fetch("https://files.example.test/a.wav", frozenset({".wav"}), "audio")
    assert path.name == "real.wav"

    web.pages["https://files.example.test/b.wav"] = FakeResponse(
        302, {"location": "https://intranet.example.test/b.wav"}
    )
    with pytest.raises(ValidationError, match="not a public address"):
        remote_mode.fetch("https://files.example.test/b.wav", frozenset({".wav"}), "audio")
    assert "https://intranet.example.test/b.wav" not in web.fetched


def test_fetch_stops_at_the_size_limit_and_refuses_bad_responses(web, inputs):
    kinds = frozenset({".wav"})
    web.pages["https://files.example.test/big.wav"] = FakeResponse(body=b"x" * 2048)
    with pytest.raises(ValidationError, match="over"):
        remote_mode.fetch("https://files.example.test/big.wav", kinds, "audio", max_bytes=1024)
    assert list(inputs.iterdir()) == []  # the partial download is gone

    web.pages["https://files.example.test/declared.wav"] = FakeResponse(
        headers={"content-length": str(10**9)}, body=b"x"
    )
    with pytest.raises(ValidationError, match="over"):
        remote_mode.fetch("https://files.example.test/declared.wav", kinds, "audio")

    with pytest.raises(ValidationError, match="HTTP 404"):
        remote_mode.fetch("https://files.example.test/missing.wav", kinds, "audio")

    web.pages["https://files.example.test/page"] = FakeResponse(
        headers={"content-type": "text/html"}, body=b"<html>"
    )
    with pytest.raises(ValidationError, match="does not look like a supported file"):
        remote_mode.fetch("https://files.example.test/page", kinds, "audio")

    web.pages["https://files.example.test/empty.wav"] = FakeResponse(body=b"")
    with pytest.raises(ValidationError, match="empty"):
        remote_mode.fetch("https://files.example.test/empty.wav", kinds, "audio")


def test_file_arg_turns_a_file_object_into_its_link_and_passes_strings_through():
    assert remote_mode.file_arg("/tmp/a.wav") == "/tmp/a.wav"
    assert remote_mode.file_arg(None) is None
    assert (
        remote_mode.file_arg({"download_url": "https://x.test/f", "file_id": "f1"})
        == "https://x.test/f"
    )
    url = remote_mode.file_arg(
        {"download_url": "https://x.test/f#old", "file_id": "f1", "file_name": "a b.wav"}
    )
    assert url == "https://x.test/f#name=a+b.wav"
    with pytest.raises(ValueError, match="download_url"):
        remote_mode.file_arg({"file_id": "f1"})


def test_every_file_argument_accepts_a_file_object():
    """A tool argument that names a file must carry `FileArg`, or ChatGPT's attached files
    would fail validation; and remote mode must know it is a file (`FILE_PARAMS`)."""
    for tool in server_mod.mcp._tool_manager.list_tools():
        for name, field in tool.fn_metadata.arg_model.model_fields.items():
            names_a_file = name in server_mod.FILE_PARAMS or name.endswith(("_file", "file_path"))
            has_validator = any(
                isinstance(m, BeforeValidator) and m.func is remote_mode.file_arg
                for m in field.metadata
            )
            assert names_a_file == has_validator, (tool.name, name)
            if names_a_file:
                assert name in server_mod.FILE_PARAMS, (tool.name, name)


# --- credentials -----------------------------------------------------------------------------


@pytest.fixture
def sdk_credentials():
    restore = remote_mode.install_sdk_credentials()
    yield
    restore()


def as_caller(key: str):
    return auth_context_var.set(AuthenticatedUser(access(key)))


def test_sdk_reads_the_calling_request_s_key(sdk_credentials):
    token = as_caller(KEY_A)
    try:
        headers = sdk_auth.get_auth_headers()
        assert headers["x-api-key"] == KEY_A and headers["x-user-id"] == "user-a"
        # The functions that look the user id up without the key they were given, too.
        # (by module: the package's attributes of the same names are the functions)
        for name in ("master", "midi", "stem_split"):
            module = sys.modules[f"audial.functions.{name}"]
            assert module.get_user_id() == "user-a"
            assert module.get_api_key() == KEY_A
    finally:
        auth_context_var.reset(token)


def test_sdk_never_falls_back_to_the_host_s_credentials(sdk_credentials, monkeypatch):
    monkeypatch.setenv("AUDIAL_API_KEY", "host-key")
    monkeypatch.setenv("AUDIAL_USER_ID", "host-user")
    assert sdk_config.get_api_key() is None and sdk_config.get_user_id() is None
    with pytest.raises(AudialAuthError):
        sdk_auth.get_auth_headers()


def test_each_thread_sees_its_own_caller(sdk_credentials):
    import contextvars

    seen = {}

    def job(key):
        token = as_caller(key)
        try:
            ctx = contextvars.copy_context()
        finally:
            auth_context_var.reset(token)

        def run():
            time.sleep(0.05)
            seen[key] = ctx.run(sdk_auth.get_auth_headers)["x-user-id"]

        return threading.Thread(target=run)

    threads = [job(KEY_A), job(KEY_B)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {KEY_A: "user-a", KEY_B: "user-b"}


def test_install_can_be_undone():
    original = sdk_config.get_api_key
    restore = remote_mode.install_sdk_credentials()
    assert sdk_config.get_api_key is not original
    restore()
    assert sdk_config.get_api_key is original and sdk_auth._config_get_api_key is original


@pytest.mark.anyio
async def test_verifier_checks_the_key_once_and_caches_it():
    calls = []

    def whoami(key):
        calls.append(key)
        if key == KEY_B:
            raise AudialAuthError("revoked")
        return {"user_id": "user-a", "email": "a@example.test", "key_name": "ChatGPT"}

    verifier = remote_mode.AudialKeyVerifier(whoami)
    token = await verifier.verify_token(KEY_A)
    assert token.subject == "user-a" and token.claims["email"] == "a@example.test"
    assert (await verifier.verify_token(KEY_A)).subject == "user-a"
    assert calls == [KEY_A]
    assert await verifier.verify_token(KEY_B) is None
    # Not an Audial key at all (or an older key that needs a separate user id): no API call.
    assert await verifier.verify_token("0123456789abcdef0123456789abcdef") is None
    assert await verifier.verify_token("") is None
    assert calls == [KEY_A, KEY_B]


@pytest.mark.anyio
async def test_verifier_expires_entries():
    verifier = remote_mode.AudialKeyVerifier(
        lambda key: {"user_id": "user-a"}, ttl_s=0, max_entries=1
    )
    await verifier.verify_token(KEY_A)
    await verifier.verify_token(KEY_B)
    assert len(verifier._cache) == 1


# --- settings --------------------------------------------------------------------------------


def test_remote_settings_defaults_and_overrides(tmp_path):
    defaults = remote_mode.remote_settings(
        host=None, port=None, public_url=None, authorization_server=None, env={}
    )
    assert (defaults.host, defaults.port) == ("127.0.0.1", 8000)
    assert defaults.public_url == "http://127.0.0.1:8000"
    assert defaults.authorization_server is None and defaults.wait_s == 40
    env = {
        "PORT": "9100",
        "AUDIAL_MCP_PUBLIC_URL": "https://mcp.audialmusic.ai/",
        "AUDIAL_MCP_AUTHORIZATION_SERVER": "https://api.audialmusic.ai/",
        "AUDIAL_MCP_RESULTS_TTL_S": "3600",
        "AUDIAL_MCP_WAIT_S": "20",
        "AUDIAL_MCP_WORK_DIR": str(tmp_path),
    }
    s = remote_mode.remote_settings(
        host="0.0.0.0", port=None, public_url=None, authorization_server=None, env=env
    )
    assert s.port == 9100 and s.public_url == "https://mcp.audialmusic.ai"
    assert s.authorization_server == "https://api.audialmusic.ai"
    assert (s.results_ttl_s, s.wait_s, s.work_dir) == (3600, 20, tmp_path)
    flag = remote_mode.remote_settings(
        host=None, port=9200, public_url=None, authorization_server=None, env=env
    )
    assert flag.port == 9200
    for bad in ({"PORT": "http"}, {"AUDIAL_MCP_WAIT_S": "0"}, {"AUDIAL_MCP_RESULTS_TTL_S": "x"}):
        with pytest.raises(ValueError):
            remote_mode.remote_settings(
                host=None, port=None, public_url=None, authorization_server=None, env=bad
            )


def test_arguments_select_the_transport():
    assert server_mod._parse_args([]).http is False
    args = server_mod._parse_args(["--http", "--port", "9001", "--public-url", PUBLIC])
    assert args.http and args.port == 9001 and args.public_url == PUBLIC
    # Arguments used to be ignored; an unknown one still is.
    assert server_mod._parse_args(["--something-else"]).http is False


# --- the HTTP app ----------------------------------------------------------------------------


@pytest.fixture
def stdio_settings(tmp_path, monkeypatch):
    s = Settings(
        user_id=None,
        api_key=None,
        results_dir=tmp_path / "local-results",
        api_base_url=None,
        job_timeout_s=30,
    )
    monkeypatch.setattr(server_mod, "_settings", s)
    return s


def settings_for(tmp_path, **env):
    return remote_mode.remote_settings(
        host="127.0.0.1",
        port=8000,
        public_url=PUBLIC,
        authorization_server=env.pop("authorization_server", None),
        env={"AUDIAL_MCP_WORK_DIR": str(tmp_path / "work"), "AUDIAL_MCP_WAIT_S": "2", **env},
    )


@asynccontextmanager
async def serving(settings):
    app = server_mod.create_http_app(settings, FakeVerifier())
    try:
        async with app.router.lifespan_context(app):
            yield app
    finally:
        server_mod.disable_remote()


@asynccontextmanager
async def connect(
    app, key: str | None, extra_headers: dict[str, str] | None = None, *, path: str = "/mcp"
):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    headers.update(extra_headers or {})
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=PUBLIC, headers=headers) as http:
        async with Client(streamable_http_client(f"{PUBLIC}{path}", http_client=http)) as client:
            yield client, http


@asynccontextmanager
async def raw_http(app):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=PUBLIC) as http:
        yield http


LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.mark.anyio
async def test_requests_without_a_valid_key_are_refused(tmp_path, stdio_settings):
    async with serving(settings_for(tmp_path)) as app, raw_http(app) as http:
        anonymous = await http.post("/mcp", json=LIST, headers=MCP_HEADERS)
        assert anonymous.status_code == 401
        assert "resource_metadata" not in anonymous.headers["www-authenticate"]
        wrong = await http.post(
            "/mcp", json=LIST, headers={**MCP_HEADERS, "authorization": "Bearer nope"}
        )
        assert wrong.status_code == 401
        # Without an authorization server there is no metadata to publish.
        prm = await http.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 404
        assert (await http.get("/healthz")).json()["ok"] is True
        assert (await http.get("/.well-known/openai-apps-challenge")).status_code == 404


@pytest.mark.anyio
async def test_the_domain_challenge_is_served_when_configured(tmp_path, stdio_settings):
    settings = settings_for(tmp_path, AUDIAL_MCP_DOMAIN_CHALLENGE="token-from-the-portal")
    async with serving(settings) as app, raw_http(app) as http:
        answer = await http.get("/.well-known/openai-apps-challenge")
        assert answer.status_code == 200 and answer.text == "token-from-the-portal"


@pytest.mark.anyio
async def test_an_authorization_server_is_published_for_oauth_clients(tmp_path, stdio_settings):
    settings = settings_for(tmp_path, authorization_server="https://auth.example.test")
    async with serving(settings) as app, raw_http(app) as http:
        anonymous = await http.post("/mcp", json=LIST, headers=MCP_HEADERS)
        assert anonymous.status_code == 401
        challenge = anonymous.headers["www-authenticate"]
        assert f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"' in challenge
        prm = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert prm["resource"] == f"{PUBLIC}/mcp"
        assert prm["authorization_servers"] == ["https://auth.example.test"]
        # Claude asks for the scopes listed here; the API defines one, "audial".
        assert prm["scopes_supported"] == ["audial"]
    async with serving(settings) as app, connect(app, KEY_A) as (client, _):
        tools = (await client.list_tools()).tools
        assert all(
            t.meta["securitySchemes"] == [{"type": "oauth2", "scopes": ["audial"]}] for t in tools
        )


@pytest.mark.anyio
async def test_remote_tool_list(tmp_path, stdio_settings):
    async with serving(settings_for(tmp_path)) as app, connect(app, KEY_A) as (client, _):
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == TOOLS - server_mod.LOCAL_ONLY_TOOLS | {"check_job"}
        for tool in tools.values():
            hints = tool.annotations
            for hint in (hints.read_only_hint, hints.destructive_hint, hints.open_world_hint):
                assert isinstance(hint, bool), tool.name
            assert len(tool.description) < 2048
            assert "securitySchemes" not in (tool.meta or {})
            files = sorted(set(tool.input_schema["properties"]) & server_mod.FILE_PARAMS)
            assert (tool.meta or {}).get("openai/fileParams", []) == files, tool.name
            for name in files:
                schema = tool.input_schema["properties"][name]
                assert schema["type"] == "object"
                assert schema["required"] == ["download_url", "file_id"]
                assert set(schema["properties"]) == {
                    "download_url",
                    "file_id",
                    "mime_type",
                    "file_name",
                }
                assert "local" not in schema["description"].lower(), (tool.name, name)
                assert "full path" not in schema["description"].lower(), (tool.name, name)
        assert tools["text2vox"].meta["openai/fileParams"] == [
            "melody_audio_file",
            "midi_file",
            "reference_file",
            "word_timestamps_file",
        ]
        assert "download links" in client.instructions
        assert len(client.instructions) < 2048


@pytest.mark.anyio
async def test_the_stdio_server_is_left_as_it_was(tmp_path, stdio_settings):
    async with serving(settings_for(tmp_path)):
        local = {t.name: t for t in await server_mod.mcp.list_tools()}
        assert set(local) == TOOLS
        assert local["stem_split"].input_schema["properties"]["file_path"]["type"] == "string"
        assert not (local["stem_split"].meta or {}).get("openai/fileParams")
    assert server_mod._remote is None and server_mod._settings is stdio_settings
    assert sdk_config.get_api_key.__module__ == "audial.utils.config"


def fake_job(monkeypatch, name, *, seen=None, release=None, raises=None):
    """An SDK function that records whose credentials it ran with and writes one file."""

    def fn(**kwargs):
        headers = sdk_auth.get_auth_headers()
        user = headers["x-user-id"]
        if seen is not None:
            seen.append({**kwargs, "user": user, "key": headers["x-api-key"]})
        if release is not None:
            assert release.wait(10)
        if raises is not None:
            raise raises
        Path(kwargs["results_folder"], f"{user}.wav").write_bytes(b"result-for-" + user.encode())
        return {"execution": {"exeId": "exe-1", "state": "completed"}, "userId": user}

    monkeypatch.setattr(server_mod.audial, name, fn)


def attached(url="https://files.example.test/f/1", name="song.wav"):
    return {"download_url": url, "file_id": "file-1", "file_name": name, "mime_type": "audio/wav"}


@pytest.mark.anyio
async def test_a_job_runs_on_the_caller_s_account_and_returns_links(
    tmp_path, stdio_settings, web, monkeypatch
):
    web.pages["https://files.example.test/f/1"] = FakeResponse(body=WAV)
    seen = []
    fake_job(monkeypatch, "stem_split", seen=seen)
    async with serving(settings_for(tmp_path)) as app, connect(app, KEY_A) as (client, http):
        result = await client.call_tool("stem_split", {"file_path": attached()})
        assert not result.is_error, result.content
        data = result.structured_content
        assert seen[0]["user"] == "user-a" and seen[0]["key"] == KEY_A
        # The SDK got a downloaded copy, named after the attached file.
        assert Path(seen[0]["file_path"]).name == "song.wav"
        assert not Path(seen[0]["file_path"]).exists()  # removed once the job ended
        [info] = data["files"]
        assert info["name"] == "user-a.wav" and info["path"].startswith(f"{PUBLIC}/files/")
        assert data["output_dir"].startswith(f"{PUBLIC}/files/")
        assert "download link" in data["summary"]
        dumped = json.dumps(data)
        assert str(tmp_path) not in dumped  # no server paths
        assert "user-a" not in json.dumps(data["metadata"])  # the user id is not echoed back
        assert KEY_A not in dumped

        download = await http.get(info["path"].removeprefix(PUBLIC))
        assert download.status_code == 200 and download.content == b"result-for-user-a"
        assert "attachment" in download.headers["content-disposition"]
        listing = await http.get(data["output_dir"].removeprefix(PUBLIC))
        assert listing.json() == {"files": [info["path"]]}
        folder = data["output_dir"].removeprefix(f"{PUBLIC}/files/")
        # Another job's folder, reached by climbing out of this one.
        other = tmp_path / "work" / "results" / ("y" * 32)
        other.mkdir()
        (other / "secret.wav").write_bytes(b"other user's")
        for path in (
            f"/files/{folder}/%2e%2e/{'y' * 32}/secret.wav",
            f"/files/{folder}/%2e%2e/%2e%2e/inputs",
            f"/files/{folder}/missing.wav",
            "/files/" + "x" * 32,
        ):
            assert (await http.get(path)).status_code == 404, path


@pytest.mark.anyio
async def test_concurrent_callers_never_share_credentials(
    tmp_path, stdio_settings, web, monkeypatch
):
    web.pages["https://files.example.test/f/1"] = FakeResponse(body=WAV)
    seen = []
    both_started = threading.Barrier(2, timeout=5)

    def fn(**kwargs):
        both_started.wait()  # both jobs are in flight at once
        user = sdk_auth.get_auth_headers()["x-user-id"]
        seen.append(user)
        Path(kwargs["results_folder"], f"{user}.wav").write_bytes(b"0")
        return {"execution": {"exeId": "exe", "state": "completed"}}

    monkeypatch.setattr(server_mod.audial, "analyze", fn)
    async with serving(settings_for(tmp_path)) as app:

        async def run(key):
            async with connect(app, key) as (client, _):
                result = await client.call_tool("analyze", {"file_path": attached()})
                return [f["name"] for f in result.structured_content["files"]]

        a, b = await asyncio.gather(run(KEY_A), run(KEY_B))
    assert a == ["user-a.wav"] and b == ["user-b.wav"]
    assert sorted(seen) == ["user-a", "user-b"]


@pytest.mark.anyio
async def test_a_long_job_returns_a_job_id_that_only_its_owner_can_check(
    tmp_path, stdio_settings, web, monkeypatch
):
    web.pages["https://files.example.test/f/1"] = FakeResponse(body=WAV)
    release = threading.Event()
    fake_job(monkeypatch, "generate_midi", release=release)
    settings = settings_for(tmp_path, AUDIAL_MCP_WAIT_S="1")
    try:
        async with serving(settings) as app:
            async with connect(app, KEY_A) as (client, _):
                started = await client.call_tool("generate_midi", {"file_path": attached()})
                running = started.structured_content
                assert running["metadata"]["status"] == "running"
                job_id = running["metadata"]["job_id"]
                assert "check_job" in running["summary"] and running["files"] == []

            async with connect(app, KEY_B) as (other, _):
                stolen = await other.call_tool("check_job", {"job_id": job_id})
                assert stolen.is_error and "no job with that id" in stolen.content[0].text

            release.set()
            async with connect(app, KEY_A) as (client, _):
                done = await client.call_tool("check_job", {"job_id": job_id})
                assert not done.is_error, done.content
                assert [f["name"] for f in done.structured_content["files"]] == ["user-a.wav"]
                again = await client.call_tool("check_job", {"job_id": job_id})
                assert again.structured_content == done.structured_content
    finally:
        release.set()


@pytest.mark.anyio
async def test_a_job_past_its_deadline_is_reported_as_timed_out(
    tmp_path, stdio_settings, web, monkeypatch
):
    web.pages["https://files.example.test/f/1"] = FakeResponse(body=WAV)
    monkeypatch.setattr(server_mod, "_settings", Settings(None, None, tmp_path, None, 1))
    release = threading.Event()
    fake_job(monkeypatch, "master", release=release)
    try:
        async with serving(settings_for(tmp_path, AUDIAL_MCP_WAIT_S="3")) as app:
            async with connect(app, KEY_A) as (client, _):
                result = await client.call_tool("master", {"file_path": attached()})
                assert result.is_error and "did not finish within" in result.content[0].text
    finally:
        release.set()


@pytest.mark.anyio
async def test_remote_errors_are_worded_for_remote_callers(
    tmp_path, stdio_settings, web, monkeypatch
):
    web.pages["https://files.example.test/f/1"] = FakeResponse(body=WAV)
    async with serving(settings_for(tmp_path)) as app, connect(app, KEY_A) as (client, _):
        fake_job(monkeypatch, "sound2vital", raises=SubscriptionRequiredError())
        refused = await client.call_tool("sound2vital", {"file_path": attached()})
        assert refused.is_error and REMOTE_SUBSCRIPTION_REQUIRED in refused.content[0].text
        assert "Subscribe" not in refused.content[0].text

        fake_job(monkeypatch, "sound2serum", raises=AudialError(f"bad key {KEY_A} for user-a"))
        leaked = await client.call_tool("sound2serum", {"file_path": attached()})
        assert KEY_A not in leaked.content[0].text and "user-a" not in leaked.content[0].text

        local = await client.call_tool("analyze", {"file_path": "/etc/hosts.wav"})
        assert local.is_error and "https URL" in local.content[0].text

        listing = await client.call_tool("list_results", {})
        assert listing.is_error


@pytest.mark.anyio
async def test_account_and_status_tools_use_the_caller(tmp_path, stdio_settings, monkeypatch):
    seen = []

    def status(function=None):
        seen.append(sdk_auth.get_auth_headers()["x-user-id"])
        return {"items": [{"function": "stem-split", "warm": True}]}

    monkeypatch.setattr(audial, "status", status)
    async with serving(settings_for(tmp_path)) as app, connect(app, KEY_B) as (client, _):
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "local" not in tools["account"].description
        who = (await client.call_tool("account", {})).structured_content
        assert who["signed_in"] and who["email"] == "b@example.test"
        status = await client.call_tool("service_status", {})
        assert not status.is_error and seen == ["user-b"]


def test_result_store_expires_folders(tmp_path):
    store = remote_mode.ResultStore(tmp_path / "results", PUBLIC, ttl_s=60)
    fresh, stale = store.new_job_dir(), store.new_job_dir()
    (stale / "a.wav").write_bytes(b"0")
    old = time.time() - 120
    os.utime(stale, (old, old))
    store.sweep()
    assert fresh.exists() and not stale.exists()
    assert store.url_for(fresh / "my song.wav") == f"{PUBLIC}/files/{fresh.name}/my%20song.wav"


# --- ChatGPT: no plans, upgrades or checkout -------------------------------------------------

# What ChatGPT puts in every tool call's `_meta` (plugins/reference).
CHATGPT_META = {"openai/subject": "v1/anon", "openai/locale": "en-US", "openai/session": "s1"}
SALES_WORDS = ("subscri", "upgrad", "pricing", "checkout", "top-up", "topup", "trial", "purchase")


def error_text(result) -> str:
    """A failed call's message, without the SDK's "Error executing tool <name>: " prefix."""
    assert result.is_error
    text = result.content[0].text
    return text.split(": ", 1)[1] if text.startswith("Error executing tool ") else text


@pytest.mark.anyio
async def test_chatgpt_is_told_plainly_that_the_account_has_no_runs_left(
    tmp_path, stdio_settings, web, monkeypatch
):
    web.pages["https://files.example.test/f/1"] = FakeResponse(body=WAV)
    fake_job(monkeypatch, "stem_split", raises=SubscriptionRequiredError())
    resets = AudialLimitReached(
        limit=100, used=100, resets_at=None, upgrade_url="https://audialmusic.ai/product/pricing"
    )
    fake_job(monkeypatch, "master", raises=resets)
    args = {"file_path": attached()}
    async with serving(settings_for(tmp_path)) as app:
        async with connect(app, KEY_A) as (client, _):
            chatgpt = await client.call_tool("stem_split", args, meta=CHATGPT_META)
            assert error_text(chatgpt) == plain.NO_RUNS_LEFT
            text = error_text(await client.call_tool("master", args, meta=CHATGPT_META))
            assert "this month's runs" in text and "http" not in text
            assert not any(word in text.lower() for word in SALES_WORDS), text
            # The same calls from a client that is not ChatGPT keep the remote wording.
            claude = await client.call_tool("stem_split", args)
            assert error_text(claude) == REMOTE_SUBSCRIPTION_REQUIRED
            other = await client.call_tool("master", args)
            assert error_text(other).startswith("Monthly API limit reached")
        # ChatGPT's user agent alone is enough, in case a call comes without `_meta`.
        ua = {"User-Agent": "openai-mcp/1.0.0"}
        async with connect(app, KEY_A, ua) as (client, _):
            by_agent = await client.call_tool("stem_split", args)
            assert error_text(by_agent) == plain.NO_RUNS_LEFT


@pytest.mark.anyio
async def test_chatgpt_results_carry_no_sales_wording(tmp_path, stdio_settings, monkeypatch):
    block = {
        "plan": "creator",
        "monthlyLimit": 100,
        "used": 98,
        "remaining": 2,
        "topupRemaining": 5,
        "resetsAt": 1798000000000,
    }
    monkeypatch.setattr(audial, "usage", lambda: dict(block))
    refused = {
        "state": "failed",
        "error": {"message": "Subscribe at audialmusic.ai and try again.", "step": "stems"},
    }
    monkeypatch.setattr(audial.pipelines, "get", lambda _id: dict(refused))
    async with serving(settings_for(tmp_path)) as app, connect(app, KEY_A) as (client, _):
        usage = (await client.call_tool("usage", {}, meta=CHATGPT_META)).structured_content
        assert usage == {"monthlyLimit": 100, "used": 98, "remaining": 7, "resetsAt": 1798000000000}
        status = await client.call_tool("pipeline_status", {"pipeline_id": "p1"}, meta=CHATGPT_META)
        assert status.structured_content["error"]["message"] == plain.NO_RUNS_LEFT
        # Other clients see the account's block as the API returns it.
        assert (await client.call_tool("usage", {})).structured_content == block


@pytest.mark.anyio
async def test_no_remote_description_or_instruction_talks_about_buying(tmp_path, stdio_settings):
    async with serving(settings_for(tmp_path)) as app, connect(app, KEY_A) as (client, _):
        texts = [client.instructions]
        for tool in (await client.list_tools()).tools:
            texts += [tool.title, tool.description, json.dumps(tool.input_schema)]
    for text in texts:
        lowered = text.lower()
        assert not [w for w in SALES_WORDS if w in lowered], text
        assert " plan " not in lowered and "plan," not in lowered, text


@pytest.mark.anyio
async def test_tool_call_streams_are_not_buffered(tmp_path, stdio_settings):
    """App Platform and other proxies hold a response back unless it says not to."""
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "check_job", "arguments": {"job_id": "nope"}}}  # fmt: skip
    headers = {**MCP_HEADERS, "authorization": f"Bearer {KEY_A}",
               "mcp-protocol-version": "2025-06-18"}  # fmt: skip
    async with serving(settings_for(tmp_path)) as app, raw_http(app) as http:
        answer = await http.post("/mcp", json=call, headers=headers)
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/event-stream")
    assert answer.headers.get("x-accel-buffering") == "no"
    assert "no-transform" in answer.headers.get("cache-control", "")


# --- what the token says about the caller ----------------------------------------------------


def endpoint(whoami, path: str = "/mcp"):
    """The verifier one endpoint of https://mcp.example.test uses: the shared key check, then
    that endpoint's audience."""
    return remote_mode.EndpointVerifier(
        remote_mode.AudialKeyVerifier(whoami),
        resource=f"https://mcp.example.test{path}",
        server="https://mcp.example.test",
    )


@pytest.mark.anyio
async def test_verifier_keeps_the_oauth_client_and_checks_the_audience():
    resource = "https://mcp.example.test/mcp"
    answers = {
        KEY_A: {"user_id": "user-a", "token_type": "oauth", "scope": "audial",
                "client_id": "https://chatgpt.com/oauth/client.json", "client_name": "ChatGPT",
                "resource": resource},
        KEY_B: {"user_id": "user-b", "token_type": "oauth",
                "resource": "https://elsewhere.example.test/mcp"},
    }  # fmt: skip
    verifier = endpoint(answers.__getitem__)
    a = await verifier.verify_token(KEY_A)
    assert a.client_id == "https://chatgpt.com/oauth/client.json"
    assert a.claims["client_name"] == "ChatGPT" and a.resource == resource
    # Issued for another server: not a token for this one.
    assert await verifier.verify_token(KEY_B) is None
    # A plain API key carries no audience and no client, and is still accepted.
    plain_key = endpoint(lambda _k: {"user_id": "u"})
    token = await plain_key.verify_token(KEY_A)
    assert token.subject == "u" and token.client_id == "audial-api-key" and token.resource is None
    # The origin alone, or a trailing slash, names the same server.
    for alias in ("https://mcp.example.test", "https://mcp.example.test/mcp/"):
        same = endpoint(
            lambda _k, alias=alias: {"user_id": "u", "token_type": "oauth", "resource": alias}
        )
        assert (await same.verify_token(KEY_A)) is not None, alias
    # A user's own API key isn't an OAuth token: the audience check doesn't apply to it.
    own = endpoint(lambda _k: {"user_id": "u", "resource": "https://elsewhere.example.test/mcp"})
    assert (await own.verify_token(KEY_A)) is not None


@pytest.mark.anyio
async def test_each_endpoint_holds_a_token_to_its_own_audience():
    """A token for /mcp is not one for /claude/mcp, nor the other way round. One issued for the
    whole server (what the API issues when a client names no resource) is good on both."""

    def oauth(resource):
        return lambda _k: {"user_id": "u", "token_type": "oauth", "resource": resource}

    server = "https://mcp.example.test"
    for issued, mcp_ok, claude_ok in (
        (f"{server}/mcp", True, False),
        (f"{server}/claude/mcp", False, True),
        (f"{server}/claude/mcp/", False, True),
        (server, True, True),
        (f"{server}/", True, True),
        (f"{server}/claude", False, False),
        ("https://elsewhere.example.test/claude/mcp", False, False),
    ):
        on_mcp = await endpoint(oauth(issued)).verify_token(KEY_A)
        on_claude = await endpoint(oauth(issued), "/claude/mcp").verify_token(KEY_A)
        assert (on_mcp is not None, on_claude is not None) == (mcp_ok, claude_ok), issued


def test_result_store_stays_under_its_size_cap(tmp_path):
    """App Platform replaces a container whose 4 GiB disk fills up, losing every job and link,
    so the oldest finished results go first once the store is over its cap."""
    store = remote_mode.ResultStore(tmp_path / "results", PUBLIC, ttl_s=3600, max_bytes=2500)
    now = time.time()
    folders = []
    for age, size in ((300, 1000), (200, 1000), (100, 1000)):
        folder = store.new_job_dir()
        (folder / "a.wav").write_bytes(b"x" * size)
        os.utime(folder, (now - age, now - age))
        folders.append(folder)
    running = store.new_job_dir()  # a job that has written nothing yet
    os.utime(running, (now - 400, now - 400))
    store.sweep()
    oldest, middle, newest = folders
    assert not oldest.exists() and middle.exists() and newest.exists()
    assert running.exists()


def test_the_results_cap_comes_from_the_environment(tmp_path):
    def cap(**env):
        return remote_mode.remote_settings(
            host=None, port=None, public_url=None, authorization_server=None, env=env
        ).max_results_bytes

    assert cap() is None
    assert cap(AUDIAL_MCP_MAX_RESULTS_MB="2500") == 2500 * 1024 * 1024
    with pytest.raises(ValueError):
        cap(AUDIAL_MCP_MAX_RESULTS_MB="lots")
    work = tmp_path / "work"
    settings = remote_mode.remote_settings(
        host=None,
        port=None,
        public_url=PUBLIC,
        authorization_server=None,
        env={"AUDIAL_MCP_WORK_DIR": str(work), "AUDIAL_MCP_MAX_RESULTS_MB": "1"},
    )
    assert remote_mode.Remote.create(settings).results.max_bytes == 1024 * 1024


def test_the_access_log_is_off_so_download_links_stay_out_of_logs(monkeypatch, tmp_path):
    """uvicorn's access log would write every /files/<secret folder>/... path, and the
    caller's address, to the platform's logs."""
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    monkeypatch.setattr(server_mod, "create_http_app", lambda settings: object())
    monkeypatch.setattr(server_mod, "BOOT_ENV", {"AUDIAL_MCP_WORK_DIR": str(tmp_path)})
    server_mod._serve_http(server_mod._parse_args(["--http", "--port", "9010"]))
    assert seen["port"] == 9010 and seen["access_log"] is False


# --- OAuth tokens expire (W1: 1 h, with rotating refresh tokens) -----------------------------


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def oauth_whoami(calls: list, expires_at: float, **extra):
    def whoami(key):
        calls.append(key)
        return {"user_id": "user-a", "token_type": "oauth", "scope": "audial",
                "expires_at": expires_at, **extra}  # fmt: skip

    return whoami


@pytest.mark.anyio
async def test_an_oauth_token_is_cached_no_longer_than_it_lives():
    clock, calls = Clock(), []
    expires = clock.now + 1000
    verifier = remote_mode.AudialKeyVerifier(
        oauth_whoami(calls, expires), clock=clock, min_lifetime_s=0
    )
    token = await verifier.verify_token(KEY_A)
    assert token.expires_at == int(expires) and token.scopes == ["audial"]
    clock.now += 299
    assert await verifier.verify_token(KEY_A) is not None and len(calls) == 1
    clock.now += 2  # past the 5 minutes: asked again
    assert await verifier.verify_token(KEY_A) is not None and len(calls) == 2
    # A token about to expire is cached only until it expires, not for 5 more minutes.
    short = remote_mode.AudialKeyVerifier(
        oauth_whoami(calls := [], clock.now + 60), clock=clock, min_lifetime_s=0
    )
    await short.verify_token(KEY_A)
    clock.now += 61
    # Expired: refused (401, so the client refreshes) without being trusted from the cache.
    assert await short.verify_token(KEY_A) is None


@pytest.mark.anyio
async def test_a_token_too_close_to_expiry_for_a_job_is_refused_so_the_client_refreshes():
    """A job runs on the token it started with for up to 15 minutes; a token that would expire
    mid-job is refused up front (401), and ChatGPT or Claude refresh it."""
    clock, calls = Clock(), []
    verifier = remote_mode.AudialKeyVerifier(oauth_whoami(calls, clock.now + 3600), clock=clock)
    assert await verifier.verify_token(KEY_A) is not None
    clock.now += 3600 - remote_mode.OAUTH_MIN_LIFETIME_S + 1
    assert await verifier.verify_token(KEY_A) is None
    # The margin never makes a freshly issued 1 h token unusable.
    assert remote_mode.OAUTH_MIN_LIFETIME_S <= 15 * 60


@pytest.mark.anyio
async def test_forget_drops_a_cached_token_so_the_api_is_asked_again():
    calls = []
    verifier = remote_mode.AudialKeyVerifier(lambda key: calls.append(key) or {"user_id": "user-a"})
    await verifier.verify_token(KEY_A)
    await verifier.verify_token(KEY_A)
    verifier.forget(KEY_A)
    await verifier.verify_token(KEY_A)
    assert calls == [KEY_A, KEY_A]
    verifier.forget("never seen")  # nothing to drop is fine


@pytest.mark.anyio
async def test_an_api_refusal_turns_the_next_request_into_a_401(
    tmp_path, stdio_settings, web, monkeypatch
):
    """The call in flight has already answered 200 (its event stream started), so it says
    what happened; the token leaves the cache, and the next request gets the 401 with
    WWW-Authenticate that makes ChatGPT and Claude refresh the token."""
    web.pages["https://files.example.test/f/1"] = FakeResponse(body=WAV)
    refused = {"now": False}

    def whoami(key):
        if refused["now"]:
            raise AudialAuthError("Authentication failed: 403")
        return {"user_id": "user-a", "email": "a@example.test"}

    def stem_split(**_kwargs):
        refused["now"] = True  # the API stopped accepting the token mid-call
        raise AudialAuthError("Authentication failed: 403")

    monkeypatch.setattr(server_mod.audial, "stem_split", stem_split)
    settings = settings_for(tmp_path, authorization_server="https://auth.example.test")
    app = server_mod.create_http_app(settings, remote_mode.AudialKeyVerifier(whoami))
    try:
        async with app.router.lifespan_context(app):
            async with connect(app, KEY_A) as (client, _):
                failed = await client.call_tool("stem_split", {"file_path": attached()})
                assert error_text(failed) == REMOTE_AUTH_REJECTED
            async with raw_http(app) as http:
                headers = {**MCP_HEADERS, "authorization": f"Bearer {KEY_A}"}
                again = await http.post("/mcp", json=LIST, headers=headers)
            assert again.status_code == 401
            assert "resource_metadata=" in again.headers["www-authenticate"]
    finally:
        server_mod.disable_remote()


# --- the Claude endpoint: every tool but music and voice generation --------------------------
#
# Zach, 2026-11-28: Claude's directory policy 4B excludes music and voice generation, so the
# Claude listing gets its own endpoint, /claude/mcp, on the same server and process. It serves
# every remote tool but generate_music and text2vox; /mcp keeps all of them for ChatGPT.

CLAUDE = "/claude/mcp"
EVERY_REMOTE_TOOL = (TOOLS - server_mod.LOCAL_ONLY_TOOLS) | {"check_job"}
CLAUDE_TOOLS = EVERY_REMOTE_TOOL - {"generate_music", "text2vox"}


def sse_json(text: str):
    """The JSON-RPC message in a streamable-HTTP answer (an event stream, or plain JSON)."""
    datas = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    return json.loads(datas[-1] if datas else text)


@pytest.mark.anyio
async def test_the_claude_endpoint_lists_every_tool_but_music_and_voice_generation(
    tmp_path, stdio_settings
):
    assert len(EVERY_REMOTE_TOOL) == 18 and len(CLAUDE_TOOLS) == 16
    async with serving(settings_for(tmp_path)) as app:
        async with connect(app, KEY_A, path=CLAUDE) as (client, _):
            claude = {t.name: t for t in (await client.list_tools()).tools}
            instructions = client.instructions
        async with connect(app, KEY_A) as (client, _):
            everything = {t.name: t for t in (await client.list_tools()).tools}
    assert set(claude) == CLAUDE_TOOLS
    assert set(everything) == EVERY_REMOTE_TOOL
    # Nothing on the Claude endpoint points at the tools it leaves out.
    for text in [instructions, *(json.dumps(t.model_dump(mode="json")) for t in claude.values())]:
        assert "generate_music" not in text and "text2vox" not in text, text
    assert "download links" in instructions and len(instructions) < 2048
    # Every tool it keeps is the one /mcp serves.
    for name, tool in claude.items():
        same = everything[name]
        assert tool.title == same.title and tool.annotations == same.annotations, name
        assert tool.meta == same.meta, name
        if name != "service_status":  # whose `tool` argument lists only the tools served
            assert (tool.description, tool.input_schema) == (same.description, same.input_schema)


@pytest.mark.anyio
async def test_the_claude_endpoint_refuses_music_and_voice_generation_like_an_unknown_tool(
    tmp_path, stdio_settings, web, monkeypatch
):
    web.pages["https://files.example.test/f/1"] = FakeResponse(body=WAV)
    seen = []
    fake_job(monkeypatch, "generate_music", seen=seen)
    fake_job(monkeypatch, "text2vox", seen=seen)
    headers = {**MCP_HEADERS, "authorization": f"Bearer {KEY_A}",
               "mcp-protocol-version": "2025-06-18"}  # fmt: skip
    attempts = {
        "generate_music": {"prompt": "a waltz"},
        "text2vox": {
            "reference_file": attached(),
            "lyrics": "la la",
            "midi_file": attached(name="melody.mid"),
        },
    }

    async with serving(settings_for(tmp_path)) as app, raw_http(app) as http:

        async def call(path, name, arguments):
            body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}}  # fmt: skip
            answer = await http.post(path, json=body, headers=headers)
            assert answer.status_code == 200, answer.text
            return sse_json(answer.text)

        unknown = await call(CLAUDE, "no_such_tool", {})
        assert unknown["result"]["isError"] is True
        for name, arguments in attempts.items():
            refused = await call(CLAUDE, name, arguments)
            assert json.dumps(refused).replace(name, "no_such_tool") == json.dumps(unknown), name
        assert seen == []  # nothing ran
        # /mcp still runs it.
        made = await call("/mcp", "generate_music", attempts["generate_music"])
        assert made["result"]["isError"] is False
    assert [s["user"] for s in seen] == ["user-a"]


@pytest.mark.anyio
async def test_the_claude_endpoint_publishes_its_own_protected_resource_metadata(
    tmp_path, stdio_settings
):
    settings = settings_for(tmp_path, authorization_server="https://auth.example.test")
    async with serving(settings) as app, raw_http(app) as http:
        anonymous = await http.post(CLAUDE, json=LIST, headers=MCP_HEADERS)
        assert anonymous.status_code == 401
        challenge = anonymous.headers["www-authenticate"]
        assert (
            f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/claude/mcp"'
            in challenge
        )
        claude = (await http.get("/.well-known/oauth-protected-resource/claude/mcp")).json()
        everything = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
        # /mcp's own answer is unchanged.
        anonymous = await http.post("/mcp", json=LIST, headers=MCP_HEADERS)
        metadata = f"{PUBLIC}/.well-known/oauth-protected-resource/mcp"
        assert f'resource_metadata="{metadata}"' in anonymous.headers["www-authenticate"]
    assert claude["resource"] == f"{PUBLIC}/claude/mcp"
    assert everything["resource"] == f"{PUBLIC}/mcp"
    assert claude["authorization_servers"] == everything["authorization_servers"]
    assert claude["authorization_servers"] == ["https://auth.example.test"]
    assert claude["scopes_supported"] == everything["scopes_supported"] == ["audial"]
    # Without an authorization server there is no metadata to point at, on either endpoint.
    async with serving(settings_for(tmp_path)) as app, raw_http(app) as http:
        anonymous = await http.post(CLAUDE, json=LIST, headers=MCP_HEADERS)
        assert anonymous.status_code == 401
        assert "resource_metadata" not in anonymous.headers["www-authenticate"]
        prm = await http.get("/.well-known/oauth-protected-resource/claude/mcp")
        assert prm.status_code == 404


def key(c: str) -> str:
    return f"aud_{c * 16}_{c.upper() * 32}"


@pytest.mark.anyio
async def test_each_endpoint_accepts_oauth_tokens_issued_for_it(tmp_path, stdio_settings):
    issued = {
        key("1"): f"{PUBLIC}/mcp",  # ChatGPT, or a custom connector at /mcp
        key("2"): f"{PUBLIC}/claude/mcp",  # the Claude directory connector
        key("3"): PUBLIC,  # a client that named no resource: the API's default
        key("4"): "https://elsewhere.example.test/mcp",
    }
    own = key("5")  # a user's own API key, not an OAuth token
    calls = []

    def whoami(token):
        calls.append(token)
        if token == own:
            return {"user_id": "user-own"}
        return {"user_id": "user-a", "token_type": "oauth", "scope": "audial",
                "expires_at": time.time() + 3600, "resource": issued[token]}  # fmt: skip

    expected = {
        "/mcp": {key("1"): 200, key("2"): 401, key("3"): 200, key("4"): 401, own: 200},
        CLAUDE: {key("1"): 401, key("2"): 200, key("3"): 200, key("4"): 401, own: 200},
    }
    settings = settings_for(tmp_path, authorization_server="https://auth.example.test")
    app = server_mod.create_http_app(settings, remote_mode.AudialKeyVerifier(whoami))
    try:
        async with app.router.lifespan_context(app), raw_http(app) as http:
            for path, answers in expected.items():
                for token, status in answers.items():
                    headers = {**MCP_HEADERS, "authorization": f"Bearer {token}",
                               "mcp-protocol-version": "2025-06-18"}  # fmt: skip
                    answer = await http.post(path, json=LIST, headers=headers)
                    assert answer.status_code == status, (path, issued.get(token, "own key"))
                    if status == 401:
                        metadata = f"{PUBLIC}/.well-known/oauth-protected-resource{path}"
                        challenge = answer.headers["www-authenticate"]
                        assert f'resource_metadata="{metadata}"' in challenge
    finally:
        server_mod.disable_remote()
    # One lookup per key whichever endpoint asked, and a token trusted on /mcp is still held
    # to /claude/mcp's audience.
    assert sorted(calls) == sorted(set(calls)) and len(calls) == 5


@pytest.mark.anyio
async def test_an_api_refusal_on_one_endpoint_is_a_401_on_both(
    tmp_path, stdio_settings, web, monkeypatch
):
    web.pages["https://files.example.test/f/1"] = FakeResponse(body=WAV)
    refused = {"now": False}

    def whoami(_key):
        if refused["now"]:
            raise AudialAuthError("Authentication failed: 403")
        return {"user_id": "user-a", "email": "a@example.test"}

    def stem_split(**_kwargs):
        refused["now"] = True
        raise AudialAuthError("Authentication failed: 403")

    monkeypatch.setattr(server_mod.audial, "stem_split", stem_split)
    settings = settings_for(tmp_path, authorization_server="https://auth.example.test")
    app = server_mod.create_http_app(settings, remote_mode.AudialKeyVerifier(whoami))
    try:
        async with app.router.lifespan_context(app):
            async with connect(app, KEY_A) as (client, _):
                assert (await client.list_tools()).tools
            async with connect(app, KEY_A, path=CLAUDE) as (client, _):
                failed = await client.call_tool("stem_split", {"file_path": attached()})
                assert error_text(failed) == REMOTE_AUTH_REJECTED
            async with raw_http(app) as http:
                headers = {**MCP_HEADERS, "authorization": f"Bearer {KEY_A}"}
                for path in ("/mcp", CLAUDE):
                    again = await http.post(path, json=LIST, headers=headers)
                    assert again.status_code == 401, path
    finally:
        server_mod.disable_remote()


@pytest.mark.anyio
@pytest.mark.parametrize("started_on, checked_on", [("/mcp", CLAUDE), (CLAUDE, "/mcp")])
async def test_a_job_started_on_one_endpoint_can_be_checked_on_the_other(
    tmp_path, stdio_settings, web, monkeypatch, started_on, checked_on
):
    web.pages["https://files.example.test/f/1"] = FakeResponse(body=WAV)
    release = threading.Event()
    fake_job(monkeypatch, "stem_split", release=release)
    try:
        async with serving(settings_for(tmp_path, AUDIAL_MCP_WAIT_S="1")) as app:
            async with connect(app, KEY_A, path=started_on) as (client, _):
                started = await client.call_tool("stem_split", {"file_path": attached()})
                assert started.structured_content["metadata"]["status"] == "running"
                job_id = started.structured_content["metadata"]["job_id"]
            async with connect(app, KEY_B, path=checked_on) as (other, _):
                stolen = await other.call_tool("check_job", {"job_id": job_id})
                assert stolen.is_error and "no job with that id" in stolen.content[0].text
            release.set()
            async with connect(app, KEY_A, path=checked_on) as (client, http):
                done = await client.call_tool("check_job", {"job_id": job_id})
                assert not done.is_error, done.content
                [info] = done.structured_content["files"]
                assert info["name"] == "user-a.wav"
                download = await http.get(info["path"].removeprefix(PUBLIC))
                assert download.status_code == 200 and download.content == b"result-for-user-a"
    finally:
        release.set()


@pytest.mark.anyio
async def test_service_status_on_the_claude_endpoint_reports_only_the_tools_it_serves(
    tmp_path, stdio_settings, monkeypatch
):
    asked = []

    def status(function=None):
        asked.append(function)
        return {
            "items": [
                {"function": f, "warm": False, "expectedColdStartMs": 1000}
                for f in ("stem-splitter", "music-generator", "text2vox")
            ]
        }

    monkeypatch.setattr(audial, "status", status)
    async with serving(settings_for(tmp_path)) as app:
        async with connect(app, KEY_A, path=CLAUDE) as (client, _):
            doc = (await client.call_tool("service_status", {})).structured_content
            assert [item["function"] for item in doc["items"]] == ["stem-splitter"]
            assert doc["cold"] == ["stem-splitter"]
            for name in ("generate_music", "music-generator", "text2vox"):
                refused = await client.call_tool("service_status", {"tool": name})
                assert "is not an Audial tool" in error_text(refused), name
                # The tools it suggests instead are the ones this endpoint serves.
                suggested = error_text(refused).replace(f"'{name}'", "")
                assert "generate_music" not in suggested and "text2vox" not in suggested
        async with connect(app, KEY_A) as (client, _):
            doc = (await client.call_tool("service_status", {})).structured_content
            assert len(doc["items"]) == 3 and len(doc["cold"]) == 3
    assert asked == [None, None]
