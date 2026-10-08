import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import audial
import pytest
from audial.api import device_login
from audial.api.exceptions import AudialError
from audial.utils import config as sdk_config
from mcp import Client

import audial_mcp.server as server_mod
from audial_mcp.config import ConfigError, Settings

TOOLS = {
    "stem_split",
    "analyze",
    "segment",
    "master",
    "generate_samples",
    "generate_midi",
    "generate_music",
    "sound2vital",
    "text2vox",
    "list_results",
    "account",
    "sign_in",
    "sign_out",
}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = Settings(
        user_id="u1",
        api_key="k1",
        results_dir=tmp_path / "Audial",
        api_base_url=None,
        job_timeout_s=5,
    )
    monkeypatch.setattr(server_mod, "_settings", s)
    return s


@pytest.fixture
def wav(tmp_path):
    f = tmp_path / "demo.wav"
    f.write_bytes(b"RIFF")
    return f


@pytest.fixture
async def client(settings):
    async with Client(server_mod.mcp, raise_exceptions=True) as c:
        yield c


def fake_sdk(monkeypatch, name, *, produce=("vocals.wav",), returns=None, calls=None):
    calls = calls if calls is not None else []

    def fn(*args, **kwargs):
        calls.append((args, kwargs))
        folder = Path(kwargs["results_folder"])
        for fname in produce:
            (folder / fname).write_bytes(b"0")
        # The SDK prints progress to stdout. This in-memory client proves nothing about that;
        # protocol safety comes from the stdio transport, which redirects fd 1 to stderr for the
        # duration of the session (see mcp/server/stdio.py). Kept because it is harmless.
        print("SDK progress line")
        return (
            returns
            if returns is not None
            else {"execution": {"exeId": "-P2h", "state": "completed"}}
        )

    monkeypatch.setattr(server_mod.audial, name, fn)
    return calls


@pytest.mark.anyio
async def test_lists_all_tools(client):
    tools = await client.list_tools()
    assert {t.name for t in tools.tools} == TOOLS


@pytest.mark.anyio
async def test_stem_split_maps_args_and_inventories_output(client, settings, wav, monkeypatch):
    calls = fake_sdk(monkeypatch, "stem_split", produce=("vocals.wav", "drums.wav"))
    result = await client.call_tool(
        "stem_split", {"file_path": str(wav), "stems": ["vocals", "drums"], "target_bpm": 90}
    )
    assert not result.is_error
    data = result.structured_content
    assert data["tool"] == "stem_split" and data["execution_id"] == "-P2h"
    assert Path(data["output_dir"]).parent == settings.results_dir / "stem_split"
    assert [f["name"] for f in data["files"]] == ["drums.wav", "vocals.wav"]
    (args, kwargs) = calls[0]
    assert kwargs["file_path"] == str(wav.resolve()) and kwargs["stems"] == ["vocals", "drums"]
    assert kwargs["target_bpm"] == 90 and kwargs["results_folder"] == data["output_dir"]


PENDING = device_login.DeviceLogin(
    device_code="dc",
    user_code="BCDF-GHJK",
    verification_uri="https://audialmusic.ai/activate",
    verification_uri_complete="https://audialmusic.ai/activate?code=BCDF-GHJK",
    expires_in=600,
    interval=0,
)
SIGNED_IN_KEY = "aud_0123456789abcdef_" + "s" * 32


@pytest.fixture
def signed_out(settings, monkeypatch):
    """A server with no API key in its config, as installed without any setup."""
    s = Settings(None, None, settings.results_dir, None, 5)
    monkeypatch.setattr(server_mod, "_settings", s)
    return s


def fake_sign_in(monkeypatch, *, approve_after):
    """Stand in for the Audial API: the user approves on poll number `approve_after`."""
    seen = {"started": 0, "polls": 0}

    def start(client_name):
        seen["started"] += 1
        seen["client_name"] = client_name
        return PENDING

    def poll(_pending):
        seen["polls"] += 1
        if seen["polls"] < approve_after:
            return None
        return sdk_config.save_credentials(
            api_key=SIGNED_IN_KEY, user_id="user-1", email="a@b.c", key_name="Audial MCP on host"
        )

    monkeypatch.setattr(server_mod.device_login, "start_device_login", start)
    monkeypatch.setattr(server_mod.device_login, "poll_device_login", poll)
    return seen


@pytest.mark.anyio
async def test_first_call_signs_in_through_the_browser_and_then_runs(
    client, signed_out, monkeypatch, wav
):
    opened = []
    monkeypatch.setattr(server_mod.webbrowser, "open", opened.append)
    seen = fake_sign_in(monkeypatch, approve_after=2)
    used = {}

    def analyze(*_args, **kwargs):
        # What the SDK would authenticate with at this point.
        used["key"], used["user"] = sdk_config.get_api_key(), sdk_config.get_user_id()
        (Path(kwargs["results_folder"]) / "analysis.json").write_bytes(b"{}")
        return {"execution": {"exeId": "e1"}, "echo": SIGNED_IN_KEY}

    monkeypatch.setattr(audial, "analyze", analyze)
    result = await client.call_tool("analyze", {"file_path": str(wav)})

    assert not result.is_error
    assert opened == [PENDING.verification_uri_complete]
    assert seen["started"] == 1 and seen["client_name"] == "audial-mcp" and seen["polls"] == 2
    assert used == {"key": SIGNED_IN_KEY, "user": "user-1"}
    # The key the sign-in issued is treated as a secret like a configured one.
    assert SIGNED_IN_KEY not in json.dumps(result.structured_content)


@pytest.mark.anyio
async def test_unapproved_sign_in_returns_the_link_and_is_resumed_by_the_next_call(
    client, signed_out, monkeypatch, wav
):
    monkeypatch.setattr(server_mod, "SIGN_IN_WAIT_S", 0)
    seen = fake_sign_in(monkeypatch, approve_after=2)
    fake_sdk(monkeypatch, "analyze", produce=("analysis.json",))

    first = await client.call_tool("analyze", {"file_path": str(wav)})
    text = first.content[0].text
    assert first.is_error and PENDING.verification_uri_complete in text and "BCDF-GHJK" in text

    second = await client.call_tool("analyze", {"file_path": str(wav)})
    assert not second.is_error
    assert seen["started"] == 1  # the same sign-in, not a new code


@pytest.mark.anyio
async def test_a_configured_api_key_is_used_without_any_sign_in(client, settings, monkeypatch, wav):
    sdk_config.save_credentials(api_key=SIGNED_IN_KEY, user_id="user-1")
    used = {}

    def analyze(*_args, **kwargs):
        used["key"], used["user"] = (
            os.environ.get("AUDIAL_API_KEY"),
            os.environ.get("AUDIAL_USER_ID"),
        )
        (Path(kwargs["results_folder"]) / "analysis.json").write_bytes(b"{}")
        return {}

    monkeypatch.setattr(audial, "analyze", analyze)
    result = await client.call_tool("analyze", {"file_path": str(wav)})
    assert not result.is_error and used == {"key": "k1", "user": "u1"}


@pytest.mark.anyio
async def test_account_and_sign_out(client, signed_out, monkeypatch):
    before = await client.call_tool("account", {})
    assert before.structured_content["signed_in"] is False

    sdk_config.save_credentials(api_key=SIGNED_IN_KEY, user_id="user-1", email="a@b.c")
    during = await client.call_tool("account", {})
    assert during.structured_content["signed_in"] is True
    assert during.structured_content["email"] == "a@b.c"
    assert SIGNED_IN_KEY not in json.dumps(during.structured_content)

    revoked = []
    monkeypatch.setattr(
        device_login.requests, "post", lambda url, **kw: revoked.append((url, kw["headers"]))
    )
    after = await client.call_tool("sign_out", {})
    assert after.structured_content["signed_in"] is False
    assert revoked[0][0].endswith("/auth/revoke") and revoked[0][1]["x-api-key"] == SIGNED_IN_KEY
    assert sdk_config.load_credentials() is None


@pytest.mark.anyio
async def test_sign_in_tool_is_refused_when_an_api_key_is_configured(client, settings):
    result = await client.call_tool("sign_in", {})
    assert result.is_error and "AUDIAL_API_KEY" in result.content[0].text


@pytest.mark.anyio
async def test_bad_path_is_a_readable_error(client):
    result = await client.call_tool("analyze", {"file_path": "/nope/missing.wav"})
    assert result.is_error and "does not exist" in result.content[0].text


@pytest.mark.anyio
async def test_generate_music_uses_prompt_slug_and_no_file(client, settings, monkeypatch):
    calls = fake_sdk(
        monkeypatch,
        "generate_music",
        produce=("song.mp3",),
        returns={"execution": {"exeId": "-P2m", "generation_metadata": {"bpm": 96}}},
    )
    result = await client.call_tool(
        "generate_music", {"prompt": "upbeat country shuffle", "audio_duration": 30}
    )
    data = result.structured_content
    assert Path(data["output_dir"]).name.endswith("_upbeat_country_shuffle")
    assert data["metadata"]["execution"]["generation_metadata"]["bpm"] == 96
    assert calls[0][1]["prompt"] == "upbeat country shuffle"
    assert calls[0][1]["max_wait"] == 5 + server_mod.SDK_WAIT_HEADROOM_S


@pytest.mark.anyio
async def test_text2vox_validates_reference_and_midi(client, tmp_path, monkeypatch):
    ref = tmp_path / "voice.wav"
    ref.write_bytes(b"0")
    mid = tmp_path / "melody.mid"
    mid.write_bytes(b"0")
    calls = fake_sdk(monkeypatch, "text2vox", produce=("song.wav", "song.mid"))
    result = await client.call_tool(
        "text2vox", {"reference_file": str(ref), "lyrics": "la la", "midi_file": str(mid)}
    )
    assert not result.is_error and calls[0][1]["midi_file"] == str(mid.resolve())


@pytest.mark.anyio
async def test_list_results_reports_previous_jobs(client, settings, wav, monkeypatch):
    fake_sdk(monkeypatch, "analyze", produce=("analysis.json",))
    await client.call_tool("analyze", {"file_path": str(wav)})
    result = await client.call_tool("list_results", {"tool": "analyze", "limit": 5})
    jobs = result.structured_content["result"]
    assert len(jobs) == 1 and jobs[0]["files"][0]["name"] == "analysis.json"


@pytest.mark.anyio
async def test_summary_text_mentions_folder(client, wav, monkeypatch):
    fake_sdk(monkeypatch, "master", produce=("mastered.wav",))
    result = await client.call_tool("master", {"file_path": str(wav)})
    text = result.content[0].text
    assert "master" in text and "mastered.wav" in json.dumps(result.structured_content)


@pytest.mark.anyio
async def test_every_tool_documents_its_parameters(client):
    for tool in (await client.list_tools()).tools:
        assert tool.description, f"{tool.name} has no description"
        for name, prop in tool.input_schema.get("properties", {}).items():
            assert prop.get("description"), f"{tool.name}.{name} has no description"


@pytest.mark.anyio
@pytest.mark.parametrize("escape", ["..", "../x", "/etc", "stem_split/../..", "sub\\dir"])
async def test_list_results_refuses_to_leave_the_results_directory(
    client, settings, wav, monkeypatch, escape
):
    fake_sdk(monkeypatch, "analyze", produce=("analysis.json",))
    await client.call_tool("analyze", {"file_path": str(wav)})
    # A sibling of the results dir that a traversal would otherwise reach.
    outside = settings.results_dir.parent / "secrets"
    (outside / "job").mkdir(parents=True)
    (outside / "job" / "private.key").write_bytes(b"0")

    result = await client.call_tool("list_results", {"tool": escape})
    assert result.is_error
    text = result.content[0].text
    assert "tool name" in text or "results directory" in text
    assert "private.key" not in text and "secrets" not in text
    assert result.structured_content is None


@pytest.mark.anyio
async def test_metadata_never_carries_credentials(client, settings, wav, monkeypatch):
    fake_sdk(
        monkeypatch,
        "analyze",
        produce=("analysis.json",),
        returns={
            "execution": {
                "exeId": "-P2h",
                "userId": "u1",
                "api_key": "k1",
                "generation_metadata": {"bpm": 96, "userId": "u1"},
                "attempts": [{"userId": "u1", "state": "completed"}],
            }
        },
    )
    result = await client.call_tool("analyze", {"file_path": str(wav)})
    blob = json.dumps(result.structured_content)
    assert "userId" not in blob and "api_key" not in blob and "u1" not in blob and "k1" not in blob
    metadata = result.structured_content["metadata"]
    assert metadata["execution"]["generation_metadata"]["bpm"] == 96
    assert metadata["execution"]["attempts"][0]["state"] == "completed"
    assert result.structured_content["execution_id"] == "-P2h"


@pytest.mark.anyio
async def test_a_failed_job_leaves_no_empty_folder(client, settings, wav, monkeypatch):
    def boom(*args, **kwargs):
        raise AudialError("the service refused the upload")

    monkeypatch.setattr(server_mod.audial, "master", boom)
    result = await client.call_tool("master", {"file_path": str(wav)})
    assert result.is_error and "refused the upload" in result.content[0].text
    assert list((settings.results_dir / "master").glob("*")) == []

    listed = await client.call_tool("list_results", {"tool": "master"})
    assert listed.structured_content["result"] == []


@pytest.mark.anyio
async def test_a_failed_job_leaves_no_empty_tool_folder(client, settings, wav, monkeypatch):
    def boom(*args, **kwargs):
        raise AudialError("the service refused the upload")

    monkeypatch.setattr(server_mod.audial, "master", boom)
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    await client.call_tool("master", {"file_path": str(wav)})
    # Not just the job folder: the `master/` folder it was created under goes too.
    assert not (settings.results_dir / "master").exists()
    assert settings.results_dir.exists()


@pytest.mark.anyio
async def test_a_failed_job_keeps_a_tool_folder_that_still_has_results(
    client, settings, wav, monkeypatch
):
    fake_sdk(monkeypatch, "master", produce=("mastered.wav",))
    await client.call_tool("master", {"file_path": str(wav)})

    def boom(*args, **kwargs):
        raise AudialError("the service refused the upload")

    monkeypatch.setattr(server_mod.audial, "master", boom)
    await client.call_tool("master", {"file_path": str(wav)})
    kept = list((settings.results_dir / "master").iterdir())
    assert len(kept) == 1 and (kept[0] / "mastered.wav").is_file()


@pytest.mark.anyio
async def test_credentials_are_redacted_by_value_from_metadata(client, settings, wav, monkeypatch):
    """Redaction cannot rely on key names alone: the service picks its own."""
    fake_sdk(
        monkeypatch,
        "analyze",
        produce=("analysis.json",),
        returns={
            "execution": {
                "exeId": "-P2h",
                "owner": "u1",
                "notes": ["submitted by u1 with key k1"],
                "nested": {"whoever": "k1"},
            }
        },
    )
    result = await client.call_tool("analyze", {"file_path": str(wav)})
    blob = json.dumps(result.structured_content)
    assert "u1" not in blob and "k1" not in blob, blob
    metadata = result.structured_content["metadata"]
    assert metadata["execution"]["owner"] == "<redacted>"
    assert metadata["execution"]["nested"]["whoever"] == "<redacted>"
    assert "<redacted>" in metadata["execution"]["notes"][0]


@pytest.mark.anyio
async def test_credentials_are_redacted_from_tool_error_text(client, settings, wav, monkeypatch):
    def boom(*args, **kwargs):
        raise AudialError("rejected request for user u1 (key k1)")

    monkeypatch.setattr(server_mod.audial, "analyze", boom)
    result = await client.call_tool("analyze", {"file_path": str(wav)})
    text = result.content[0].text
    assert result.is_error
    assert "u1" not in text and "k1" not in text, text
    assert "rejected request for user <redacted>" in text


@pytest.mark.anyio
async def test_a_startup_config_error_is_returned_by_every_tool(client, settings, wav, monkeypatch):
    monkeypatch.setattr(
        server_mod, "_startup_error", ConfigError("AUDIAL_JOB_TIMEOUT_S must be a positive integer")
    )
    for name, args in (("analyze", {"file_path": str(wav)}), ("list_results", {})):
        result = await client.call_tool(name, args)
        assert result.is_error, name
        assert "AUDIAL_JOB_TIMEOUT_S" in result.content[0].text, name


def test_a_bad_timeout_does_not_crash_the_import(tmp_path):
    """Importing the server with a malformed setting must not raise a traceback at import.

    `_settings` is read at module scope, before `main()` has configured logging, so a raised
    ConfigError would reach the client as an opaque "server failed to start".
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import audial_mcp.server as s;"
            "print(s._startup_error);"
            "print(s._settings.job_timeout_s, s._settings.user_id)",
        ],
        env={**os.environ, "AUDIAL_JOB_TIMEOUT_S": "soon", "AUDIAL_USER_ID": "u1"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "AUDIAL_JOB_TIMEOUT_S" in proc.stdout
    assert "900 u1" in proc.stdout


SDK_TOOL_ARGS = {
    "stem_split": {"file_path": "{wav}", "stems": ["vocals"], "target_bpm": 90},
    "analyze": {"file_path": "{wav}"},
    "segment": {"file_path": "{wav}", "components": ["drums"], "genre": "house"},
    "master": {"file_path": "{wav}", "reference_file": "{wav}"},
    "generate_samples": {"file_path": "{wav}", "components": ["drums"]},
    "generate_midi": {"file_path": "{wav}", "bpm": 120},
    "generate_music": {"prompt": "upbeat country shuffle", "audio_duration": 30, "seed": 7},
    "sound2vital": {"file_path": "{wav}"},
    "text2vox": {"reference_file": "{wav}", "lyrics": "la la", "midi_file": "{mid}", "seed": 3},
}


@pytest.mark.anyio
async def test_tool_kwargs_bind_to_the_real_sdk_signatures(client, tmp_path, wav, monkeypatch):
    """Every kwarg a tool passes must be accepted by the SDK function it calls.

    The tools are exercised against a fake SDK, so a renamed or dropped SDK parameter would
    otherwise go unnoticed until a real call failed. Here the recorded kwargs are bound
    against the *real* function's signature (bound, never called).
    """
    mid = tmp_path / "melody.mid"
    mid.write_bytes(b"0")
    substitutions = {"{wav}": str(wav), "{mid}": str(mid)}

    for name, template in SDK_TOOL_ARGS.items():
        real = getattr(audial, name)
        assert callable(real), f"audial.{name} is missing"
        calls = fake_sdk(monkeypatch, name, produce=("out.wav",))
        args = {
            k: substitutions.get(v, v) if isinstance(v, str) else v for k, v in template.items()
        }
        result = await client.call_tool(name, args)
        assert not result.is_error, f"{name}: {result.content[0].text}"
        _, kwargs = calls[0]
        inspect.signature(real).bind(**kwargs)
