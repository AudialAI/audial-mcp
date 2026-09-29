import json
from pathlib import Path

import pytest
from mcp import Client

import audial_mcp.results as results_mod
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
        print("SDK progress line that must not corrupt the protocol")
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
    assert calls[0][1]["prompt"] == "upbeat country shuffle" and calls[0][1]["max_wait"] == 5


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


def _fields(typed_dict):
    return {
        name: getattr(hint, "__forward_arg__", str(hint))
        for name, hint in typed_dict.__annotations__.items()
    }


def test_output_schema_mirrors_the_results_typeddicts():
    """server.py re-declares these so pydantic accepts them below Python 3.12."""
    assert _fields(server_mod.JobResult) == _fields(results_mod.JobResult)
    assert _fields(server_mod.FileInfo) == _fields(results_mod.FileInfo)


@pytest.mark.anyio
async def test_every_tool_documents_its_parameters(client):
    for tool in (await client.list_tools()).tools:
        assert tool.description, f"{tool.name} has no description"
        for name, prop in tool.input_schema.get("properties", {}).items():
            assert prop.get("description"), f"{tool.name}.{name} has no description"
