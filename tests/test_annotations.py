"""Every tool's title and hints, as ChatGPT's and Claude's directories review them.

Both directories reject a tool without a `title` and the applicable hints. OpenAI requires
readOnlyHint, destructiveHint and openWorldHint as explicit booleans, and counts starting a job
or uploading a file as a write (plugins/plugin-guidelines). The table below is the decision for
each tool; a new tool fails here until it is added to it.
"""

from __future__ import annotations

import pytest
from mcp import Client

import audial_mcp.server as server_mod
from audial_mcp import remote as remote_mode

# Tools that start a job on Audial (uploading a file, or adding to an analysis): never
# read-only. They write new results and change nothing that exists, so not destructive. Open
# world: in remote mode they fetch whatever https link the user gives.
JOB = (False, False, True)
# Reads confined to the user's own Audial account or service: read-only, closed world.
ACCOUNT_READ = (True, False, False)
# Reads of this computer's own state: read-only, closed world.
LOCAL_READ = (True, False, False)

# (readOnlyHint, destructiveHint, openWorldHint)
EXPECTED = {
    "stem_split": JOB,
    "analyze": JOB,
    "segment": JOB,
    "audio_features": JOB,
    "tag_audio": JOB,
    "get_lyrics": JOB,
    "master": JOB,
    "generate_samples": JOB,
    "generate_midi": JOB,
    "generate_music": JOB,
    "sound2vital": JOB,
    "sound2serum": JOB,
    "text2vox": JOB,
    "service_status": ACCOUNT_READ,
    "usage": ACCOUNT_READ,
    "pipeline_status": ACCOUNT_READ,
    "check_job": ACCOUNT_READ,
    "list_results": LOCAL_READ,
    "account": LOCAL_READ,
    # Saves a key on this computer after the user approves in the browser: a write, undoable.
    "sign_in": (False, False, False),
    # Revokes this computer's key: the one tool that undoes something.
    "sign_out": (False, True, False),
}


def _check(tool) -> None:
    assert tool.title, f"{tool.name} has no title"
    assert tool.title != tool.name and "_" not in tool.title, (tool.name, tool.title)
    assert tool.title[0].isupper() and len(tool.title) <= 60, (tool.name, tool.title)
    hints = tool.annotations
    assert hints is not None, tool.name
    actual = (hints.read_only_hint, hints.destructive_hint, hints.open_world_hint)
    assert all(isinstance(h, bool) for h in actual), (tool.name, actual)
    assert actual == EXPECTED[tool.name], (tool.name, actual)


@pytest.mark.anyio
async def test_every_stdio_tool_has_a_title_and_the_decided_hints():
    async with Client(server_mod.mcp) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == set(EXPECTED) - {"check_job"}
    for tool in tools:
        _check(tool)


@pytest.mark.anyio
@pytest.mark.parametrize("issuer", [None, "https://auth.example.test"])
async def test_every_remote_tool_has_a_title_and_the_decided_hints(tmp_path, issuer):
    settings = remote_mode.remote_settings(
        host="127.0.0.1",
        port=8000,
        public_url="https://mcp.example.test",
        authorization_server=issuer,
        env={"AUDIAL_MCP_WORK_DIR": str(tmp_path)},
    )
    tools = await server_mod._remote_server(settings).list_tools()
    assert {t.name for t in tools} == set(EXPECTED) - server_mod.LOCAL_ONLY_TOOLS
    for tool in tools:
        _check(tool)


def test_only_sign_out_is_destructive_and_no_job_tool_is_read_only():
    assert [name for name, hints in EXPECTED.items() if hints[1]] == ["sign_out"]
    jobs = {name for name, hints in EXPECTED.items() if hints == JOB}
    assert "analyze" in jobs  # it uploads the file and records an execution


# --- the reference voice -----------------------------------------------------------------------


def _says_the_user_owns_or_has_consent(text: str) -> bool:
    lowered = text.lower()
    return "own" in lowered and "consent" in lowered and "responsible" in lowered


@pytest.mark.anyio
async def test_text2vox_says_the_user_must_own_the_voice_or_have_consent(tmp_path):
    """OpenAI's usage policies forbid using someone's voice without their consent. Zach,
    2026-11-27: the tool stays, and the user is responsible for owning the voice."""
    async with Client(server_mod.mcp) as client:
        local = {t.name: t for t in (await client.list_tools()).tools}["text2vox"]
    settings = remote_mode.remote_settings(
        host="127.0.0.1",
        port=8000,
        public_url="https://mcp.example.test",
        authorization_server=None,
        env={"AUDIAL_MCP_WORK_DIR": str(tmp_path)},
    )
    remote = {t.name: t for t in await server_mod._remote_server(settings).list_tools()}["text2vox"]
    for tool in (local, remote):
        assert _says_the_user_owns_or_has_consent(tool.description), tool.description
        reference = tool.input_schema["properties"]["reference_file"]["description"]
        assert _says_the_user_owns_or_has_consent(reference), reference
    assert "consent" in server_mod._remote_instructions(settings)
