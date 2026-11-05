"""The analysis and what is added to it: segment, audio_features, tag_audio, get_lyrics, and
generate_samples on an analysis.

The Audial SDK is mocked throughout, its 1.6 analysis functions included; nothing here talks to
the service.
"""

import dataclasses
import json
import re
import time
from pathlib import Path

import pytest
from audial.api.exceptions import AudialAPIError
from mcp import Client

import audial_mcp.server as server_mod
from audial_mcp.config import Settings

ROOT = Path(__file__).resolve().parent.parent

ANALYSIS = {
    "audio_segmentation": {
        "format_version": 4,
        "bpm": 124.0,
        "grid": {
            "beats_per_bar": 4,
            "beat_count": 400,
            "bar_count": 100,
            "pieces": [{"start": 0.48, "bpm": 124.0, "bar": 1, "beat": 1, "count": 400}],
        },
        "sections": [
            {"label": "INTRO", "start": 0.48, "end": 31.45},
            {"label": "CHORUS", "start": 31.45, "end": 62.42},
        ],
    },
    "analysis_id": "-PaNew",
}
# segment() also returns the analysis under its 1.5 name, `segmentation`: the same object.
ANALYSIS["segmentation"] = ANALYSIS["audio_segmentation"]
ANALYSIS["execution"] = {"exeId": "-PaNew", "state": "completed"}
SEGMENT_FILE = "demo.wav_segmentation/analysis.json"

LYRICS = {
    "lyrics": "Hold me close tonight",
    "is_instrumental": False,
    "segments": [{"text": "Hold me close", "start": 12.3, "end": 14.1}],
    "lines": [
        {
            "start": 12.3,
            "end": 14.1,
            "words": [["Hold", 12.3, 12.6], [" me", 12.6, 12.8], ["close", 12.9, 14.1]],
        },
        {"start": 15.0, "end": 16.2, "words": [["tonight", 15.0, 16.2]]},
    ],
    "section_lyrics": ["", "Hold me close tonight"],
    "first_vocal_beat_time": 12.3,
}

FEATURES = {
    "features_version": 2,
    "song": {"key": "A minor", "loudness": -7.9, "energy": 0.71, "danceability": 0.64},
    "sections": [{"index": 0, "label": "INTRO", "energy": 0.42}],
}

TAGS = {
    "tags_version": 1,
    "genre": ["Electronic---UK Garage"],
    "song_tags": {"full": {"moods": ["dark"], "instruments": ["synth"], "artist_tags": []}},
}

# Each add-on tool, the SDK function it calls, a result, and the file the SDK saves it as.
ADDONS = [
    ("audio_features", "audio_features", FEATURES, "features.json"),
    ("tag_audio", "tags", TAGS, "tags.json"),
    ("get_lyrics", "lyrics", LYRICS, "lyrics.json"),
]
SAVED_AS = {sdk_name: saved_as for _, sdk_name, _, saved_as in ADDONS}


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


@pytest.fixture
def sdk(monkeypatch):
    """Fake SDK functions, recording each call in order as (name, kwargs).

    Like the SDK, each writes its result into `results_folder`: segment as
    `<file>_segmentation/analysis.json`, an add-on as `<analysis_id>_analysis/<name>.json`.
    """
    calls: list[tuple[str, dict]] = []

    def install(name, returns, *, writes=None):
        def fn(**kwargs):
            calls.append((name, kwargs))
            if isinstance(returns, BaseException):
                raise returns
            saved = writes or (
                f"{kwargs['analysis_id']}_analysis/{SAVED_AS[name]}" if name in SAVED_AS else None
            )
            if saved and "results_folder" in kwargs:
                path = Path(kwargs["results_folder"]) / saved
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(returns))
            return returns

        monkeypatch.setattr(server_mod.audial, name, fn, raising=False)

    def refuse(*names):
        for name in names:

            def fn(*_args, _name=name, **_kwargs):
                raise AssertionError(f"audial.{_name} must not be called")

            monkeypatch.setattr(server_mod.audial, name, fn, raising=False)

    install.calls = calls
    install.refuse = refuse
    return install


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


# ---------------------------------------------------------------------------
# segment
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_segment_is_one_analysis_of_the_mix_and_returns_its_id(client, wav, sdk):
    sdk("segment", ANALYSIS, writes=SEGMENT_FILE)
    result = await client.call_tool("segment", {"file_path": str(wav), "genre": "House"})
    assert not result.is_error, result.content[0].text

    (name, kwargs), *rest = sdk.calls
    assert name == "segment" and not rest
    assert kwargs["file_path"] == str(wav.resolve()) and kwargs["genre"] == "House"
    assert kwargs["edm"] is None and kwargs["features"] is None
    # The model reads the tempo map; the per-beat lists would only lengthen the result.
    assert kwargs["expand_beats"] is False
    assert callable(kwargs["on_progress"])
    assert kwargs["max_wait"] == 5 + server_mod.SDK_WAIT_HEADROOM_S
    assert "components" not in kwargs and "analysis_type" not in kwargs

    data = result.structured_content
    assert kwargs["results_folder"] == data["output_dir"]
    assert [f["name"] for f in data["files"]] == [SEGMENT_FILE]
    assert data["execution_id"] == "-PaNew"
    meta = data["metadata"]
    assert meta["analysis_id"] == "-PaNew"
    assert meta["audio_segmentation"]["grid"]["pieces"][0]["bpm"] == 124.0
    # The 1.5 name for the same analysis is not sent twice.
    assert "segmentation" not in meta
    assert "analysis_id: -PaNew" in data["summary"]
    for tool in ("audio_features", "tag_audio", "get_lyrics", "generate_samples"):
        assert tool in data["summary"]


@pytest.mark.anyio
async def test_segment_still_accepts_features_and_the_old_component_arguments(client, wav, sdk):
    """Callers written for 0.5 keep working: `features` is passed on, the rest is ignored."""
    sdk("segment", ANALYSIS, writes=SEGMENT_FILE)
    result = await client.call_tool(
        "segment",
        {
            "file_path": str(wav),
            "features": ["energy", "tags"],
            "edm": True,
            "components": ["drums"],
            "analysis_type": "select_features",
        },
    )
    assert not result.is_error, result.content[0].text
    _, kwargs = sdk.calls[0]
    assert kwargs["features"] == ["energy", "tags"] and kwargs["edm"] is True
    assert "components" not in kwargs and "analysis_type" not in kwargs


# ---------------------------------------------------------------------------
# The add-ons
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_lyrics_from_file(client, settings, wav, sdk):
    """A file is analysed first, then the lyrics are added to that analysis as timed lines."""
    sdk("segment", ANALYSIS, writes=SEGMENT_FILE)
    sdk("lyrics", LYRICS)
    result = await client.call_tool("get_lyrics", {"file_path": str(wav)})
    assert not result.is_error, result.content[0].text

    assert [name for name, _ in sdk.calls] == ["segment", "lyrics"]
    segment_kwargs, lyrics_kwargs = sdk.calls[0][1], sdk.calls[1][1]
    assert segment_kwargs["file_path"] == str(wav.resolve())
    assert lyrics_kwargs["analysis_id"] == "-PaNew"

    data = result.structured_content
    assert segment_kwargs["results_folder"] == lyrics_kwargs["results_folder"] == data["output_dir"]
    assert data["tool"] == "get_lyrics" and data["execution_id"] == "-PaNew"
    assert Path(data["output_dir"]).parent == settings.results_dir / "get_lyrics"
    meta = data["metadata"]
    assert meta["lines"] == [
        {"start": 12.3, "end": 14.1, "text": "Hold me close"},
        {"start": 15.0, "end": 16.2, "text": "tonight"},
    ]
    assert meta["lyrics"] == "Hold me close tonight" and meta["analysis_id"] == "-PaNew"
    assert meta["section_lyrics"] == ["", "Hold me close tonight"]
    assert "segments" not in meta
    # The analysis and the full lyrics, word timings included, are both in the job folder.
    assert sorted(f["name"] for f in data["files"]) == ["-PaNew_analysis/lyrics.json", SEGMENT_FILE]
    assert "analysis_id: -PaNew" in data["summary"]


@pytest.mark.anyio
@pytest.mark.parametrize("tool,sdk_name,returns,saved_as", ADDONS)
async def test_an_addon_on_an_analysis_id_runs_no_new_analysis(
    client, settings, sdk, tool, sdk_name, returns, saved_as
):
    sdk.refuse("segment")
    sdk(sdk_name, returns)
    result = await client.call_tool(tool, {"analysis_id": "-PaOld"})
    assert not result.is_error, result.content[0].text

    [(name, kwargs)] = sdk.calls
    data = result.structured_content
    assert name == sdk_name and kwargs["analysis_id"] == "-PaOld"
    assert kwargs["results_folder"] == data["output_dir"]
    # Warm-up news reaches the client, and the watchdog fires before the SDK gives up.
    assert callable(kwargs["on_progress"])
    assert kwargs["max_wait"] == 5 + server_mod.SDK_WAIT_HEADROOM_S
    assert data["metadata"]["analysis_id"] == "-PaOld" and data["execution_id"] == "-PaOld"
    assert Path(data["output_dir"]).name.endswith("_-PaOld")
    assert [f["name"] for f in data["files"]] == [f"-PaOld_analysis/{saved_as}"]


@pytest.mark.anyio
async def test_audio_features_and_tags_come_back_whole(client, sdk):
    sdk("audio_features", FEATURES)
    sdk("tags", TAGS)
    features = await client.call_tool("audio_features", {"analysis_id": "-PaOld"})
    tags = await client.call_tool("tag_audio", {"analysis_id": "-PaOld"})
    assert features.structured_content["metadata"]["song"]["energy"] == 0.71
    assert features.structured_content["metadata"]["sections"][0]["label"] == "INTRO"
    assert tags.structured_content["metadata"]["genre"] == ["Electronic---UK Garage"]


@pytest.mark.anyio
async def test_an_instrumental_says_so(client, sdk):
    sdk("lyrics", {"lyrics": "", "is_instrumental": True, "lines": []})
    result = await client.call_tool("get_lyrics", {"analysis_id": "-PaOld"})
    data = result.structured_content
    assert data["metadata"]["lines"] == [] and data["metadata"]["is_instrumental"] is True
    assert "No vocals in this track." in data["summary"]


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["audio_features", "tag_audio", "get_lyrics", "generate_samples"])
@pytest.mark.parametrize(
    "args,expected",
    [({}, "Give the analysis_id"), ({"analysis_id": "-Pa1", "file_path": "{wav}"}, "not both")],
)
async def test_an_addon_needs_exactly_one_source(client, settings, wav, sdk, tool, args, expected):
    sdk.refuse("segment", "audio_features", "tags", "lyrics", "generate_samples")
    args = {k: str(wav) if v == "{wav}" else v for k, v in args.items()}
    result = await client.call_tool(tool, args)
    assert result.is_error and expected in result.content[0].text
    assert not (settings.results_dir / tool).exists()


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["../../db/u1/execution", "a/b", "id with spaces"])
async def test_an_addon_refuses_an_id_that_is_not_an_id(client, sdk, bad):
    sdk.refuse("lyrics")
    result = await client.call_tool("get_lyrics", {"analysis_id": bad})
    assert result.is_error and "is not an analysis id" in result.content[0].text


def api_error(message, code=None, body=None):
    """An SDK API error: 1.6 sets `code`; the body is the API's 409 answer."""
    exc = AudialAPIError(message, status_code=409, response=FakeResponse(body) if body else None)
    if code:
        exc.code = code
    return exc


@pytest.mark.anyio
async def test_stems_not_ready_names_the_new_analysis_for_the_retry(client, wav, sdk):
    sdk("segment", ANALYSIS, writes=SEGMENT_FILE)
    sdk(
        "lyrics",
        api_error(
            "This track is still being prepared. Try again in a minute. (STEMS_NOT_READY)",
            code="STEMS_NOT_READY",
        ),
    )
    result = await client.call_tool("get_lyrics", {"file_path": str(wav)})
    text = result.content[0].text
    assert result.is_error
    # The SDK's own words come through once, not twice.
    assert text.count("This track is still being prepared.") == 1
    # The analysis already happened and counted: the retry must reuse it.
    assert "call get_lyrics again with analysis_id -PaNew" in text
    folders = list((server_mod._settings.results_dir / "get_lyrics").iterdir())
    assert len(folders) == 1 and (folders[0] / SEGMENT_FILE).is_file()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error,expected",
    [
        (
            api_error("STEMS_NOT_READY"),
            "This track is still being prepared. Try again in a minute.",
        ),
        (
            api_error("STEMS_NOT_READY", body={"code": "STEMS_NOT_READY", "state": "failed"}),
            "We couldn't prepare this track.",
        ),
        (
            # The API's own sentence, with the code in the body: passed on as it is.
            api_error(
                "This analysis has no stems to build this from.",
                body={"code": "ADDON_UNAVAILABLE"},
            ),
            "This analysis has no stems to build this from.",
        ),
        (
            api_error("", code="ADDON_UNAVAILABLE"),
            "This result can't be added to this analysis.",
        ),
        (
            api_error(
                "This result can't be added to this analysis. This analysis has no stems to "
                "build this from. (ADDON_UNAVAILABLE)",
                code="ADDON_UNAVAILABLE",
            ),
            "This result can't be added to this analysis. This analysis has no stems to build "
            "this from. (ADDON_UNAVAILABLE)",
        ),
    ],
)
async def test_a_bare_refusal_code_gets_the_websites_words(client, sdk, error, expected):
    sdk("tags", error)
    result = await client.call_tool("tag_audio", {"analysis_id": "-PaOld"})
    assert result.is_error
    # An analysis id the caller gave needs no reminder.
    assert result.content[0].text.endswith(f"tag_audio: {expected}")


@pytest.mark.anyio
async def test_a_timeout_after_a_new_analysis_names_it(client, settings, wav, sdk, monkeypatch):
    monkeypatch.setattr(server_mod, "_settings", dataclasses.replace(settings, job_timeout_s=0.3))
    sdk("segment", ANALYSIS, writes=SEGMENT_FILE)

    def slow_lyrics(**_kwargs):
        time.sleep(1)
        return LYRICS

    monkeypatch.setattr(server_mod.audial, "lyrics", slow_lyrics, raising=False)
    result = await client.call_tool("get_lyrics", {"file_path": str(wav)})
    text = result.content[0].text
    assert result.is_error and "did not finish within" in text
    assert "call get_lyrics again with analysis_id -PaNew" in text


@pytest.mark.anyio
async def test_an_analysis_without_an_id_is_an_error_not_a_crash(client, wav, sdk):
    sdk("segment", {"audio_segmentation": {}}, writes=SEGMENT_FILE)
    sdk.refuse("lyrics")
    result = await client.call_tool("get_lyrics", {"file_path": str(wav)})
    assert result.is_error and "without an analysis id" in result.content[0].text


# ---------------------------------------------------------------------------
# generate_samples on an analysis
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_generate_samples_on_an_analysis_passes_only_the_id(client, wav, sdk):
    sdk("generate_samples", {"execution": {"exeId": "-PaSample"}}, writes="kick.wav")
    on_analysis = await client.call_tool(
        "generate_samples", {"analysis_id": "-PaOld", "components": ["drums"]}
    )
    on_file = await client.call_tool("generate_samples", {"file_path": str(wav)})
    assert not on_analysis.is_error and not on_file.is_error

    (_, by_id), (_, by_file) = sdk.calls
    assert by_id["analysis_id"] == "-PaOld" and "file_path" not in by_id
    assert by_id["components"] == ["drums"]
    assert by_file["file_path"] == str(wav.resolve()) and "analysis_id" not in by_file
    assert Path(on_analysis.structured_content["output_dir"]).name.endswith("_-PaOld")


# ---------------------------------------------------------------------------
# service_status
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool,asked",
    [
        ("tag_audio", "audio-tags"),
        ("audio_features", "audio-features"),
        ("get_lyrics", "lyrics"),
        ("audio-tags", "audio-tags"),
        ("segment", "segment"),
        (None, None),
    ],
)
async def test_service_status_knows_the_addon_tools(client, monkeypatch, tool, asked):
    seen = []

    def status(function=None):
        seen.append(function)
        return {"items": [{"function": asked or "segmentation", "warm": True}]}

    monkeypatch.setattr(server_mod.audial, "status", status)
    result = await client.call_tool("service_status", {"tool": tool} if tool else {})
    assert not result.is_error, result.content[0].text
    assert seen == [asked]


@pytest.mark.anyio
async def test_service_status_lists_the_addon_tools_when_refusing_a_name(client, monkeypatch):
    monkeypatch.setattr(server_mod.audial, "status", lambda function=None: {"items": []})
    result = await client.call_tool("service_status", {"tool": "nope"})
    assert result.is_error
    for tool in ("audio_features", "tag_audio", "get_lyrics", "segment"):
        assert tool in result.content[0].text


# ---------------------------------------------------------------------------
# What the user reads
# ---------------------------------------------------------------------------

# House rule: customer-facing text never names an upstream model, library or service.
DENYLIST = (
    "Whisper",
    "Essentia",
    "Discogs",
    "EffNet",
    "MTG",
    "Jamendo",
    "rekordbox",
    "librosa",
    "pyloudnorm",
    "OpenAI",
    "MusicBrainz",
    "RunPod",
    "scikit-learn",
)


@pytest.mark.anyio
async def test_customer_facing_text_names_no_upstream_engine(client):
    texts = {
        "instructions": server_mod.INSTRUCTIONS,
        "README.md": (ROOT / "README.md").read_text(),
        "manifest.json": (ROOT / "bundle" / "manifest.json").read_text(),
        "server.json": (ROOT / "server.json").read_text(),
    }
    for tool in (await client.list_tools()).tools:
        texts[tool.name] = json.dumps(
            [tool.description, tool.input_schema.get("properties", {})], ensure_ascii=False
        )
    texts["errors"] = (ROOT / "src" / "audial_mcp" / "errors.py").read_text()
    for where, text in texts.items():
        for name in DENYLIST:
            assert not re.search(rf"\b{re.escape(name)}\b", text, re.I), f"{name} in {where}"


@pytest.mark.anyio
async def test_the_readme_names_every_tool(client):
    """The Tools table lists every job tool; the sign-in section names the account ones."""
    readme = (ROOT / "README.md").read_text()
    table = readme.split("## Tools", 1)[1].split("\n## ", 1)[0]
    rows = set(re.findall(r"^\| `([a-z0-9_]+)` \|", table, re.M))
    account = {"account", "sign_in", "sign_out"}
    registered = {t.name for t in (await client.list_tools()).tools}
    assert rows == registered - account
    signing_in = readme.split("### Signing in", 1)[1].split("\n## ", 1)[0]
    assert all(f"`{name}`" in signing_in for name in account)
