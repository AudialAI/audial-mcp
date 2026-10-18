"""Pipeline step reporting: the relay, its wiring into run_job, and the tools.

The Audial SDK is mocked throughout; nothing here talks to the service.
"""

from pathlib import Path

import audial
import pytest

import audial_mcp.server as server_mod
from audial_mcp.config import Settings
from audial_mcp.jobs import run_job
from audial_mcp.pipelines import StepRelay


class FakeCtx:
    def __init__(self):
        self.progress = []
        self.logs = []

    async def report_progress(self, progress, total=None, message=None):
        self.progress.append((progress, total, message))

    async def info(self, message):
        self.logs.append(message)


def _running():
    return {
        "pipelineId": "pl-1",
        "pipeline": "sample-pack",
        "state": "running",
        "currentStep": "sample-pack",
        "steps": [
            {"name": "stem-split", "state": "completed", "exeId": "exe-stem", "durationMs": 41000},
            {"name": "sample-pack", "state": "running", "exeId": "exe-sample"},
        ],
        "outputs": {"stems": {"drumsmp3": {"filename": "d.mp3", "url": "https://x/d.mp3"}}},
    }


def _completed():
    return {
        "pipelineId": "pl-1",
        "pipeline": "sample-pack",
        "state": "completed",
        "durationMs": 128000,
        "steps": [
            {"name": "stem-split", "state": "completed", "exeId": "exe-stem", "durationMs": 41000},
            {
                "name": "sample-pack",
                "state": "completed",
                "exeId": "exe-sample",
                "durationMs": 87000,
            },
        ],
        "outputs": {"samples": ["https://x/kick.wav"]},
    }


# ---------------------------------------------------------------------------
# StepRelay
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_relay_reports_step_changes_as_progress_and_log_lines():
    ctx = FakeCtx()
    relay = StepRelay()

    relay.on_progress(_running())
    await relay.drain(ctx)

    assert relay.lines == ["stem-split completed in 41 s; sample-pack running"]
    assert ctx.logs == relay.lines
    # One of two steps done, so the client can draw a bar.
    assert ctx.progress == [(1, 2, "stem-split completed in 41 s; sample-pack running")]

    # Nothing changed: nothing is sent again.
    relay.on_progress(_running())
    await relay.drain(ctx)
    assert len(ctx.logs) == 1

    relay.on_progress(_completed())
    await relay.drain(ctx)
    assert "sample-pack completed in 87 s" in ctx.logs[-1]
    assert ctx.progress[-1][0] == 2


@pytest.mark.anyio
async def test_relay_survives_a_context_that_cannot_report():
    class Mute:
        pass

    relay = StepRelay()
    relay.on_progress(_running())
    await relay.drain(Mute())  # no report_progress / info at all
    assert len(relay.lines) == 1


@pytest.mark.anyio
async def test_relay_survives_a_disconnected_client():
    class Broken(FakeCtx):
        async def report_progress(self, progress, total=None, message=None):
            raise ConnectionResetError("client disconnected")

        async def info(self, message):
            raise ConnectionResetError("client disconnected")

    relay = StepRelay()
    relay.on_progress(_running())
    await relay.drain(Broken())
    assert len(relay.lines) == 1


# ---------------------------------------------------------------------------
# run_job drains the relay while the job runs, and once more after it ends
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_job_forwards_relay_messages_including_the_last_one():
    ctx = FakeCtx()
    relay = StepRelay()

    def work():
        # As the SDK does: report from the worker thread.
        relay.on_progress(_running())
        relay.on_progress(_completed())
        return "done"

    result = await run_job(
        ctx,
        "generate_samples",
        work,
        timeout_s=5,
        heartbeat_s=5,
        relay=relay,
        relay_interval_s=0.05,
    )

    assert result == "done"
    step_messages = [m for _, _, m in ctx.progress if "stem-split" in m or "sample-pack" in m]
    assert step_messages
    assert any("sample-pack completed in 87 s" in line for line in ctx.logs)


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


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
    from mcp import Client

    async with Client(server_mod.mcp, raise_exceptions=True) as c:
        yield c


def _watch_relays(monkeypatch):
    """Capture the relays the tools create, so what reached the client is visible."""
    created: list[StepRelay] = []

    class Watched(StepRelay):
        def __init__(self):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(server_mod, "StepRelay", Watched)
    return created


def _pipeline_sdk(monkeypatch, name, produce):
    """A fake SDK function that reports two pipeline polls, then writes its output."""
    calls = []

    def fn(*args, **kwargs):
        calls.append((args, kwargs))
        on_progress = kwargs["on_progress"]
        on_progress(_running())
        on_progress(_completed())
        folder = Path(kwargs["results_folder"])
        for fname in produce:
            (folder / fname).write_bytes(b"0")
        return {"execution": _completed()}

    monkeypatch.setattr(server_mod.audial, name, fn)
    return calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool,sdk_name,produce",
    [
        ("generate_samples", "generate_samples", ("kick.wav",)),
        ("segment", "segment", ("pruned_audio_segmentation.json",)),
    ],
)
async def test_pipeline_tools_forward_each_step_change(
    client, settings, wav, monkeypatch, tool, sdk_name, produce
):
    relays = _watch_relays(monkeypatch)
    calls = _pipeline_sdk(monkeypatch, sdk_name, produce)

    result = await client.call_tool(tool, {"file_path": str(wav)})
    assert not result.is_error, result.content[0].text

    # The SDK is given the relay's callback...
    assert callable(calls[0][1]["on_progress"])
    # ...and every step change it reported came back out through the context.
    assert len(relays) == 1
    assert relays[0].lines == [
        "stem-split completed in 41 s; sample-pack running",
        "sample-pack completed in 87 s; pipeline completed in 128 s",
    ]

    data = result.structured_content
    assert [f["name"] for f in data["files"]] == [produce[0]]
    # A pipeline has no single execution id; its pipeline id takes that place.
    assert data["execution_id"] == "pl-1"


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["generate_samples", "segment"])
async def test_pipeline_tool_descriptions_name_the_steps(client, tool):
    described = {t.name: t.description for t in (await client.list_tools()).tools}
    description = described[tool]
    assert "splits stems" in description.lower()
    assert "one call" in description.lower()


@pytest.mark.anyio
async def test_pipeline_status_returns_the_status_dict(client, settings, monkeypatch):
    def fake_get(pipeline_id, *args, **kwargs):
        assert pipeline_id == "pl-1"
        return dict(_completed(), userId="u1")

    monkeypatch.setattr(audial.pipelines, "get", fake_get)

    result = await client.call_tool("pipeline_status", {"pipeline_id": "pl-1"})
    assert not result.is_error, result.content[0].text
    status = result.structured_content
    assert status["state"] == "completed"
    assert [step["name"] for step in status["steps"]] == ["stem-split", "sample-pack"]
    assert status["outputs"]["samples"] == ["https://x/kick.wav"]
    # Credentials never come back out, whatever the service echoes.
    assert "userId" not in status


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["../../db/u1/execution", "pl 1", "", "a/b"])
async def test_pipeline_status_refuses_an_id_that_is_not_an_id(client, settings, monkeypatch, bad):
    def fake_get(pipeline_id, *args, **kwargs):
        raise AssertionError("must not reach the API")

    monkeypatch.setattr(audial.pipelines, "get", fake_get)

    result = await client.call_tool("pipeline_status", {"pipeline_id": bad})
    assert result.is_error
    assert "pipeline id" in result.content[0].text


@pytest.mark.anyio
async def test_pipeline_status_turns_an_sdk_error_into_a_readable_one(
    client, settings, monkeypatch
):
    from audial.api.exceptions import AudialAPIError

    def fake_get(pipeline_id, *args, **kwargs):
        raise AudialAPIError("Failed to get pipeline pl-9: not found (PIPELINE_NOT_FOUND)")

    monkeypatch.setattr(audial.pipelines, "get", fake_get)

    result = await client.call_tool("pipeline_status", {"pipeline_id": "pl-9"})
    assert result.is_error
    assert "PIPELINE_NOT_FOUND" in result.content[0].text
