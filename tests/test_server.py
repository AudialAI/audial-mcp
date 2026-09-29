import json
from pathlib import Path

import pytest
from audial.api.exceptions import AudialError
from mcp import Client

import audial_mcp.server as server_mod
from audial_mcp.config import Settings

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


@pytest.mark.anyio
async def test_missing_credentials_is_a_readable_error(client, monkeypatch, wav, settings):
    monkeypatch.setattr(
        server_mod, "_settings", Settings(None, None, settings.results_dir, None, 5)
    )
    result = await client.call_tool("analyze", {"file_path": str(wav)})
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
