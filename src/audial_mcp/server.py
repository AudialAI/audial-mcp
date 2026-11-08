"""audial MCP server: tools that wrap the Audial SDK."""

from __future__ import annotations

import argparse
import contextlib
import copy
import logging
import os
import re
import sys
import time
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

import anyio
import audial
from audial.api import device_login
from audial.api.constants import STATUS_FUNCTIONS
from audial.api.exceptions import AudialAuthError, AudialError
from audial.utils import config as sdk_config
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from mcp.types import ToolAnnotations
from pydantic import BaseModel, BeforeValidator, Field
from starlette.applications import Starlette

from audial_mcp import BOOT_ENV, __version__
from audial_mcp import remote as remote_mode
from audial_mcp.config import (
    SIGN_IN_WAIT_S,
    ConfigError,
    Settings,
    fallback_settings,
    load_settings,
)
from audial_mcp.errors import SignInRequired, to_tool_error
from audial_mcp.jobs import JobTimeout, run_job
from audial_mcp.progress import ProgressRelay
from audial_mcp.results import FileInfo, JobResult, inventory, list_jobs, new_job_dir, slugify
from audial_mcp.validation import (
    AUDIO_EXTENSIONS,
    JSON_EXTENSIONS,
    MIDI_EXTENSIONS,
    ValidationError,
    check_file,
)

log = logging.getLogger("audial_mcp")

INSTRUCTIONS = (
    "Audial runs hosted audio tools. Give tools full local file paths. Results are written to "
    "the user's Audial results folder and returned as file paths; tell the user where they are. "
    "Jobs take from a few seconds (analyze) to a few minutes (stem_split, generate_music, "
    "sound2vital, sound2serum, text2vox); progress notifications report elapsed time. segment "
    "takes about half a minute and returns an analysis_id; audio_features, tag_audio, "
    "get_lyrics and generate_samples take it to add to that analysis (given a file, they run "
    "segment first). Tags, lyrics and sample packs wait a few minutes after a new analysis "
    "while its stems are made. pipeline_status looks up a pipeline (generate_samples on a "
    "file) by id. Audial's workers scale to zero, so the first job after an idle period "
    "spends extra time starting one: that shows up as a 'Warming up the machine' progress "
    "line, worth relaying to the user as the reason for the wait; service_status says in "
    "advance which tools are warm. An analysis counts as one run and everything added to it "
    "is included; every other job but analyze counts as one run; a failed run does not "
    "count. Accounts without a subscription get a small free allowance and then need a "
    "subscription: relay the error text if one is returned. "
    "No API key is needed: if the user is not signed in, the first tool call "
    "opens a sign-in page in their browser and continues once they approve. If a tool instead "
    "returns a sign-in link, give the user that link and the code, and retry when they say "
    "they have approved it. "
    "sound2vital and sound2serum model one isolated sound, so check the input first: a file "
    "that is a loop, melody or drum pattern (a tempo or 'loop' in its name is a clue) gives a "
    "poor preset even under 20 s, so say so before running it and suggest trimming it to one "
    "note. After either tool, relay anything in `summary` about the match to the user."
)

RESYNTH_INPUT = (
    "One isolated sound: a single note, chord stab or hit, ideally 1-4 s and dry, 20 s at most. "
    "Not a loop, melody, drum pattern or anything with several notes in sequence: those produce "
    "a poor preset even under 20 s. To use a loop, trim it to one note first."
)
RESYNTH_TOOLS = frozenset({"sound2vital", "sound2serum"})

mcp = MCPServer("audial", instructions=INSTRUCTIONS, version=__version__)

# Settings are read at import so the tool bodies can use them, but a bad value must not kill
# the import: that surfaces in clients as an opaque "server failed to start" with a raw
# traceback, before `main()` has even configured logging. Instead the server comes up with
# usable defaults, remembers the error, and returns it from every tool call.
_startup_error: ConfigError | None = None
try:
    _settings: Settings = load_settings()
except ConfigError as exc:
    _settings = fallback_settings()
    _startup_error = exc

_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

# The add-on tools, by the name GET /functions/status reports them under. Their tool names are
# this server's own, so the SDK's table of its function names does not translate them.
_ADDON_FUNCTIONS = {
    "audio_features": "audio-features",
    "tag_audio": "audio-tags",
    "get_lyrics": "lyrics",
}

# GET /functions/status reports each function under the API's own name
# ("sample-pack"); the SDK accepts either spelling and `service_status` takes
# both, so a model that read a status document back can ask about what it saw.
_API_FUNCTIONS = frozenset(STATUS_FUNCTIONS.values()) | frozenset(_ADDON_FUNCTIONS.values())

# The SDK is given a little more patience than `run_job`'s watchdog, so the watchdog fires first
# and the user gets `JobTimeout`'s guidance rather than a bare SDK timeout.
SDK_WAIT_HEADROOM_S = 60

# Every hint is set explicitly: ChatGPT requires readOnlyHint, destructiveHint and openWorldHint
# as booleans, and an unset destructiveHint means "destructive" to MCP clients. The job tools
# start work and write new results, but never delete or overwrite the user's data.
REMOTE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, open_world_hint=True, idempotent_hint=False
)
READ_ONLY_REMOTE = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=True
)
LOCAL_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=False
)
# sign_out revokes this computer's key: the one tool that undoes something.
SIGN_OUT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, open_world_hint=True, idempotent_hint=False
)

# Set when the server runs with --http (see `create_http_app`); None for the stdio server.
_remote: remote_mode.Remote | None = None

# A plan validates the tool's arguments and returns the job's folder-name slug together with the
# SDK call to run in it. Tools hand one to `_execute`, which runs it inside the try block so a
# bad path becomes a readable ToolError just like a failure from the service does.
Plan = Callable[[], tuple[str, Callable[[Path], Any]]]


def _execution_id(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("execution_id", "exe_id", "exeId", "pipelineId", "pipeline_id"):
        if isinstance(raw.get(key), str):
            return raw[key]
    execution = raw.get("execution")
    if isinstance(execution, dict):
        # A pipeline reports a pipelineId instead of a single execution id; it
        # is what `pipeline_status` takes, so it belongs in the same field.
        for key in ("exeId", "exe_id", "execution_id", "pipelineId", "pipeline_id"):
            if isinstance(execution.get(key), str):
                return execution[key]
    # An analysis (segment, and the add-ons on one) is identified by its analysis id.
    if isinstance(raw.get("analysis_id"), str):
        return raw["analysis_id"]
    return None


# Keys the service echoes back that identify or authenticate the user. They are stripped from
# `metadata` at every depth: a tool result is shown to the model and written into transcripts.
SECRET_KEYS = frozenset(
    {"userid", "user_id", "api_key", "apikey", "x-api-key", "apikeyid", "authorization", "token"}
)


REDACTED = "<redacted>"


# The browser sign-in in use, as last read from the credentials file by `_apply_credentials`.
# Remembered so its key and user id are redacted from results like the configured ones.
_signed_in: dict[str, Any] | None = None

# A browser sign-in that has been started and not yet approved. Kept between tool calls so an
# approval that arrives after one call stopped waiting is picked up by the next.
_pending_sign_in: device_login.DeviceLogin | None = None
_pending_sign_in_expires = 0.0


def _secrets() -> tuple[str, ...]:
    """The credential strings in use, longest first so the longer match wins."""
    signed_in = _signed_in or {}
    access = remote_mode.caller() if _remote is not None else None
    candidates = (
        _settings.api_key,
        _settings.user_id,
        signed_in.get("api_key"),
        signed_in.get("user_id"),
        access.token if access else None,
        access.subject if access else None,
    )
    values = {v for v in candidates if v}
    return tuple(sorted(values, key=len, reverse=True))


def _redact(value: Any) -> Any:
    """Strip credentials from anything on its way back to the model.

    Two passes, because the service does not always echo secrets back under a predictable
    key: known credential *keys* are dropped, and any string that *is* one of the configured
    credentials is replaced, wherever it appears.
    """
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items() if str(k).lower() not in SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(text: str) -> str:
    """Replace any configured credential appearing inside `text`.

    In remote mode server paths go too: a result's becomes its download link.
    """
    for secret in _secrets():
        text = text.replace(secret, REDACTED)
    return _remote.scrub(text) if _remote is not None else text


def _tool_error(
    exc: BaseException,
    *,
    tool: str,
    execution_id: str | None = None,
    analysis_id: str | None = None,
) -> ToolError:
    """`to_tool_error`, with credentials scrubbed out of the message it produces.

    Upstream error text can quote the request it failed on, which may carry the user id.
    """
    error = to_tool_error(
        exc,
        tool=tool,
        execution_id=execution_id,
        api_key_from_env=bool(_settings.api_key),
        analysis_id=analysis_id,
        remote=_remote is not None,
    )
    return ToolError(_redact_text(str(error)))


def _metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"result": _redact(raw)}
    return _redact({k: v for k, v in raw.items() if k != "files"})


def _resynth_advice(tool: str, raw: Any) -> str:
    """What a finished resynthesis needs the model to pass on, so a weak preset has a reason."""
    if tool not in RESYNTH_TOOLS or not isinstance(raw, dict):
        return ""
    notes = [w for w in raw.get("warnings") or [] if isinstance(w, str)]
    scores = raw.get("scores")
    if isinstance(scores, dict) and scores.get("at_threshold") is False:
        score, target = scores.get("huang_similarity"), scores.get("threshold")
        if isinstance(score, (int, float)) and isinstance(target, (int, float)):
            notes.append(f"The preset scored {score:.3f} against a {target:.3f} match target.")
        else:
            notes.append("The preset is below the match target.")
        # The API's own warning already gives this advice when it spotted several notes.
        if not notes[:-1]:
            notes.append(
                "The usual cause is an input that is not one isolated sound: a loop, melody or "
                "drum pattern gives a poor preset even under 20 s. If this sounds wrong, trim "
                "the input to a single note and run it again."
            )
    return "".join(f" {note}" for note in notes)


ADDON_TOOLS = ("audio_features", "tag_audio", "get_lyrics", "generate_samples")


def _analysis_note(tool: str, raw: Any) -> str:
    """The analysis id a result carries, said in the summary so the model reuses it."""
    if not isinstance(raw, dict) or not isinstance(raw.get("analysis_id"), str):
        return ""
    analysis_id = raw["analysis_id"]
    if tool == "segment":
        others = ", ".join(ADDON_TOOLS[:-1]) + f" or {ADDON_TOOLS[-1]}"
        return (
            f" analysis_id: {analysis_id}. Pass it to {others} to add to this analysis; that "
            "uploads nothing and does not count as another run."
        )
    note = f" analysis_id: {analysis_id} (reuse it for the other add-ons)."
    if tool == "get_lyrics" and raw.get("is_instrumental") is True:
        note += " No vocals in this track."
    return note


def _tool_filter(tool: str) -> str:
    """Check a model-supplied tool name before it is joined onto the results directory."""
    if not _TOOL_NAME_RE.fullmatch(tool):
        raise ValidationError(
            f"{tool!r} is not a tool name. Give one of the Audial tool names "
            "(letters, digits, '_' and '-' only), or omit it to list every tool."
        )
    root = _settings.results_dir.resolve()
    if not (root / tool).resolve().is_relative_to(root):
        raise ValidationError(f"{tool!r} does not name a folder inside the results directory.")
    return tool


def _discard_if_empty(folder: Path) -> None:
    """A job that failed before writing anything leaves no empty folder behind.

    The job folder's parent is the per-tool folder; if this job was the only thing in it, that
    goes too, so a tool that has never produced anything does not show up in the results
    directory at all.
    """
    with contextlib.suppress(OSError):
        if not folder.is_dir() or any(folder.iterdir()):
            return
        folder.rmdir()
        parent = folder.parent
        if parent != _settings.results_dir and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


def _saved_sign_in() -> dict[str, Any] | None:
    """The browser sign-in saved on this machine for the configured API, if any."""
    global _signed_in
    if _settings.api_base_url:
        os.environ["AUDIAL_API_BASE_URL"] = _settings.api_base_url
    else:
        os.environ.pop("AUDIAL_API_BASE_URL", None)
    _signed_in = sdk_config.load_credentials()
    return _signed_in


async def _sign_in(ctx: Context) -> dict[str, Any]:
    """Sign the user in through their browser and wait for them to approve.

    Opens the approval page, reports the link as progress, and polls. If the user has not
    approved within SIGN_IN_WAIT_S the link is raised as `SignInRequired`; the sign-in stays
    pending, so calling again after approving succeeds at once.
    """
    global _pending_sign_in, _pending_sign_in_expires, _signed_in
    pending = _pending_sign_in if time.monotonic() < _pending_sign_in_expires else None
    if pending is None:
        pending = await anyio.to_thread.run_sync(device_login.start_device_login, "audial-mcp")
        _pending_sign_in = pending
        _pending_sign_in_expires = time.monotonic() + pending.expires_in
        # Best effort: over SSH or in a container there is no browser, and the link below
        # is what the user gets.
        with contextlib.suppress(Exception):
            await anyio.to_thread.run_sync(webbrowser.open, pending.verification_uri_complete)

    url, code = pending.verification_uri_complete, pending.user_code
    message = f"Sign in to Audial: open {url} and approve (the page should show code {code})"
    log.info("%s", message)
    deadline = time.monotonic() + SIGN_IN_WAIT_S
    while True:
        with contextlib.suppress(Exception):
            await ctx.report_progress(0, message=message)
        try:
            credentials = await anyio.to_thread.run_sync(device_login.poll_device_login, pending)
        except Exception:
            _pending_sign_in = None  # refused or expired: the next call starts a fresh one
            raise
        if credentials:
            _pending_sign_in = None
            _signed_in = credentials
            log.info("signed in to Audial")
            return credentials
        if time.monotonic() + pending.interval > deadline:
            raise SignInRequired(url, code)
        await anyio.sleep(max(pending.interval, 0.01))


async def _apply_credentials(ctx: Context) -> None:
    """Put the credentials to use where the SDK reads them, signing in first if there are none.

    An API key in the client's config wins. Without one the SDK reads the sign-in saved on this
    machine, which this starts (in the browser) when it does not exist yet. Never logged.

    In remote mode the request's own verified key is the only credential, and the SDK reads it
    from the request (see `remote.install_sdk_credentials`); nothing is set or signed in here.
    """
    if _remote is not None:
        if remote_mode.caller() is None:
            raise AudialAuthError("This request carries no Audial credentials.")
        return
    saved = _saved_sign_in()
    if _settings.api_key:
        os.environ["AUDIAL_API_KEY"] = _settings.api_key
        if _settings.user_id:
            os.environ["AUDIAL_USER_ID"] = _settings.user_id
        else:
            os.environ.pop("AUDIAL_USER_ID", None)
        return
    os.environ.pop("AUDIAL_API_KEY", None)
    os.environ.pop("AUDIAL_USER_ID", None)
    if saved is None:
        await _sign_in(ctx)


def _job_result(tool: str, raw: Any, output_dir: Path, elapsed: int) -> tuple[JobResult, str]:
    """The result a finished job returns, and its one-line summary for the log and progress."""
    files = inventory(output_dir)
    shown_dir = str(output_dir)
    note = ""
    if _remote is not None:
        files = [
            FileInfo(
                name=f["name"], path=_remote.results.url_for(f["path"]), size_bytes=f["size_bytes"]
            )
            for f in files
        ]
        shown_dir = _remote.results.url_for(output_dir)
        hours = max(1, _remote.settings.results_ttl_s // 3600)
        note = f" Each file's `path` is its download link, valid for {hours} h."
    summary = f"{tool} finished in {elapsed} s: {len(files)} file(s) in {shown_dir}"
    advice = _resynth_advice(tool, raw) + _analysis_note(tool, raw)
    result = JobResult(
        tool=tool,
        execution_id=_execution_id(raw),
        output_dir=shown_dir,
        files=files,
        metadata=_metadata(raw),
        summary=summary + note + advice,
    )
    return result, summary


async def _execute(
    ctx: Context,
    tool: str,
    plan: Plan,
    relay: ProgressRelay | None = None,
    made: dict[str, str] | None = None,
) -> JobResult:
    """Common path: validate → credentials → job folder → run in thread → inventory → JobResult.

    `relay` is the tool's progress relay: the SDK fills it from the worker
    thread with each warm-up phase (and, for the pipeline tools, each step
    change) and `run_job` drains it to the client.

    `made` is filled by an add-on tool's call with the `analysis_id` of an analysis it made
    from a file on the way. If the add-on then fails or times out, the error names that id,
    so the retry reuses the analysis instead of paying for a new one.
    """
    if _startup_error is not None:
        raise ToolError(str(_startup_error))
    if _remote is not None:
        return await _execute_remote(ctx, tool, plan, relay or ProgressRelay(), made or {})
    execution_id: str | None = None
    output_dir: Path | None = None
    try:
        slug, call = plan()
        await _apply_credentials(ctx)
        output_dir = new_job_dir(_settings.results_dir, tool, slug)
        started = time.monotonic()
        log.info("%s: starting; results -> %s", tool, output_dir)
        await ctx.report_progress(0, message=f"{tool}: starting; results -> {output_dir}")
        raw = await run_job(
            ctx,
            tool,
            lambda: call(output_dir),
            timeout_s=_settings.job_timeout_s,
            relay=relay,
        )
        execution_id = _execution_id(raw)
        elapsed = int(time.monotonic() - started)
        result, summary = _job_result(tool, raw, output_dir, elapsed)
        log.info("%s", summary)
        # The job is done; a failed progress notification must not turn it into an error.
        with contextlib.suppress(Exception):
            await ctx.report_progress(elapsed, total=elapsed, message=summary)
        return result
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        if output_dir is not None:
            _discard_if_empty(output_dir)
        raise _tool_error(
            exc,
            tool=tool,
            execution_id=execution_id,
            analysis_id=(made or {}).get("analysis_id"),
        ) from None


# --- remote mode: jobs that outlive the request ----------------------------------------------
#
# ChatGPT gives a tool call about a minute, and progress notifications do not extend it, while
# most Audial jobs take several. So in remote mode a job runs in the background: the call waits
# up to `wait_s` for it and returns the result if it finished, or else a "running" result with
# a job_id, which `check_job` takes to wait again.


def _pending(job: remote_mode.RemoteJob) -> JobResult:
    """What a call returns for a job that is still running."""
    elapsed = int(time.monotonic() - job.started)
    latest = f" Latest news: {job.relay.lines[-1]}" if job.relay.lines else ""
    return JobResult(
        tool=job.tool,
        execution_id=None,
        output_dir="",
        files=[],
        metadata={"status": "running", "job_id": job.id, "elapsed_s": elapsed},
        summary=(
            f"{job.tool} is still running after {elapsed} s (jobs like this can take a few "
            f"minutes).{latest} Tell the user it is in progress, then call check_job with "
            f"job_id {job.id} to get the result."
        ),
    )


async def _await_job(ctx: Context, job: remote_mode.RemoteJob) -> JobResult:
    """Wait up to the remote wait budget for `job`, then report where it stands."""
    assert _remote is not None
    budget_end = time.monotonic() + _remote.settings.wait_s
    next_beat = time.monotonic() + 5
    while not job.done.is_set():
        now = time.monotonic()
        if now >= budget_end or now >= job.deadline:
            break
        await anyio.sleep(0.25)
        await job.relay.drain(ctx)
        if now >= next_beat:
            next_beat = now + 5
            elapsed = int(now - job.started)
            with contextlib.suppress(Exception):
                await ctx.report_progress(elapsed, message=f"{job.tool}: {elapsed} s elapsed")
    await job.relay.drain(ctx)
    if not job.done.is_set():
        if time.monotonic() < job.deadline:
            return _pending(job)
        # Past its deadline, but the job may still finish: a later check_job then has it.
        error: BaseException = JobTimeout(job.tool, time.monotonic() - job.started)
    elif job.error is not None:
        error = job.error
        _discard_if_empty(job.output_dir)
    else:
        if job.result is None:
            elapsed = int((job.finished or time.monotonic()) - job.started)
            job.result, _summary = _job_result(job.tool, job.raw, job.output_dir, elapsed)
            # The folder name is the download links' secret: it stays out of the log.
            count = len(job.result["files"])
            log.info(
                "%s: job %s finished in %d s, %d file(s)", job.tool, job.id[:6], elapsed, count
            )
        return job.result
    raise _tool_error(
        error,
        tool=job.tool,
        execution_id=_execution_id(job.raw),
        analysis_id=job.made.get("analysis_id"),
    ) from None


async def _execute_remote(
    ctx: Context, tool: str, plan: Plan, relay: ProgressRelay, made: dict[str, str]
) -> JobResult:
    """`_execute` for remote mode: fetch the inputs, start the job, wait a while for it."""
    remote = _remote
    assert remote is not None
    inputs = remote.open_inputs()
    try:
        try:
            with remote.use_inputs(inputs):
                # Downloads happen here, so off the event loop. The slug is unused: a remote
                # job's folder is named by its id.
                _slug, call = await anyio.to_thread.run_sync(plan)
            await _apply_credentials(ctx)
        except BaseException:
            remote.close_inputs(inputs)
            raise
        owner = remote_mode.caller()
        output_dir = remote.results.new_job_dir()
        job = remote.jobs.start(
            owner=owner.subject if owner else "",
            tool=tool,
            output_dir=output_dir,
            fn=lambda: call(output_dir),
            relay=relay,
            made=made,
            timeout_s=_settings.job_timeout_s,
            cleanup=lambda: remote.close_inputs(inputs),
        )
        log.info("%s: started job %s", tool, job.id[:6])
        with contextlib.suppress(Exception):
            await ctx.report_progress(0, message=f"{tool}: starting")
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        raise _tool_error(exc, tool=tool, analysis_id=made.get("analysis_id")) from None
    return await _await_job(ctx, job)


async def check_job(
    ctx: Context,
    job_id: Annotated[
        str,
        Field(description="The job_id a tool returned with status 'running'."),
    ],
) -> JobResult:
    """Remote mode only: the result of a job that was still running when its tool returned.

    Waits up to the server's wait budget (`AUDIAL_MCP_WAIT_S`) for the job. Only the account
    that started a job can check it; for any other, the job does not exist. The description
    the remote server lists is set in `create_http_app`.
    """
    assert _remote is not None
    owner = remote_mode.caller()
    job = _remote.jobs.get(job_id, owner.subject if owner else None)
    if job is None:
        raise ToolError(
            "There is no job with that id for this account. Its results may have expired, or "
            "the server restarted; run the tool again."
        )
    return await _await_job(ctx, job)


AUDIO_FILE = (
    "Full path to a local audio file (.wav, .mp3, .aif, .aiff, .flac, .m4a, .ogg, .aac). "
    "~ is expanded."
)

# Every argument that names a file takes this validator. A local client passes a path, which
# goes through unchanged. ChatGPT passes a file the user attached as an object
# ({download_url, file_id, mime_type, file_name}), which becomes its download link (see
# `remote.file_arg`); the advertised schema stays a plain string for the stdio server.
FileArg = BeforeValidator(remote_mode.file_arg)

# The argument names that hold files, across all tools: remote mode advertises them as file
# objects (`openai/fileParams`).
FILE_PARAMS = frozenset(
    {
        "file_path",
        "reference_file",
        "source_file",
        "midi_file",
        "melody_audio_file",
        "word_timestamps_file",
    }
)


def _file(value: str, kinds: frozenset[str], label: str) -> Path:
    """A file argument as a local path: checked on disk (stdio) or downloaded (remote)."""
    if _remote is None:
        return check_file(value, kinds, label)
    return remote_mode.fetch(value, kinds, label)


FilePath = Annotated[str, FileArg, Field(description=AUDIO_FILE)]

AnalysisId = Annotated[
    str | None,
    Field(
        description=(
            "The analysis_id a segment result returned (in its metadata and summary). Reuses "
            "that analysis: nothing is uploaded and it does not count as another run. Give "
            "this or file_path."
        )
    ),
]

AnalysisFilePath = Annotated[
    str | None,
    FileArg,
    Field(
        description=(
            f"{AUDIO_FILE} For a track that has not been analysed yet: segment runs on it "
            "first (one run). Give this or analysis_id."
        )
    ),
]

# Analysis ids are execution ids (Firebase push keys, or a pipeline id for an analysis made by
# a pipeline). The SDK puts them into request paths, so they are checked here first.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _analysis_source(
    analysis_id: str | None, file_path: str | None
) -> tuple[str | None, Path | None]:
    """Exactly one of an analysis id or a file: `(analysis_id, None)` or `(None, file)`."""
    if analysis_id and file_path:
        raise ValidationError(
            "Give analysis_id or file_path, not both: analysis_id reuses an analysis, "
            "file_path analyses the file first."
        )
    if analysis_id:
        if not _ID_RE.fullmatch(analysis_id):
            raise ValidationError(
                f"{analysis_id!r} is not an analysis id (letters, digits, '_' and '-' only). "
                "Use the analysis_id a segment result returned."
            )
        return analysis_id, None
    if file_path:
        return None, _file(file_path, AUDIO_EXTENSIONS, "audio file")
    raise ValidationError(
        "Give the analysis_id a segment result returned, or the file_path of a track to "
        "analyse first."
    )


def _segment(src: Path, out: Path, relay: ProgressRelay, **options: Any) -> Any:
    """One analysis of the mix, saved into the job folder.

    `expand_beats=False`: the SDK can add per-beat and per-bar lists rebuilt from the tempo
    map, for scripts written against older analysis files. A model reads the tempo map
    (`grid`) directly, and the lists would only make the result longer.
    """
    return audial.segment(
        file_path=str(src),
        **options,
        results_folder=str(out),
        on_progress=relay.on_progress,
        expand_beats=False,
        max_wait=_settings.job_timeout_s + SDK_WAIT_HEADROOM_S,
    )


def _analysis_id_of(analysis: Any) -> str:
    analysis_id = analysis.get("analysis_id") if isinstance(analysis, dict) else None
    if not isinstance(analysis_id, str) or not analysis_id:
        raise AudialError("The analysis finished without an analysis id, so nothing was added.")
    return analysis_id


def _without_alias(analysis: Any) -> Any:
    """The SDK also returns the analysis as `segmentation` (its 1.5 name): drop the copy."""
    if isinstance(analysis, dict) and analysis.get("segmentation") is analysis.get(
        "audio_segmentation"
    ):
        return {k: v for k, v in analysis.items() if k != "segmentation"}
    return analysis


async def _addon(
    ctx: Context,
    tool: str,
    sdk_name: str,
    analysis_id: str | None,
    file_path: str | None,
    shape: Callable[[Any], dict[str, Any]],
) -> JobResult:
    """Run one add-on on an analysis, analysing `file_path` first when no id is given.

    The SDK's add-on functions wait for the run to finish (and, for the add-ons that read
    them, for the stems), save the result into the job folder and return it; `shape` turns it
    into `metadata`. When a file was analysed here, the analysis is saved in the same folder.
    """
    relay = ProgressRelay()
    made: dict[str, str] = {}

    def plan() -> tuple[str, Callable[[Path], Any]]:
        given, src = _analysis_source(analysis_id, file_path)
        slug = slugify(src.stem) if src is not None else slugify(given or "", "analysis")

        def call(out: Path) -> dict[str, Any]:
            aid = given
            if src is not None:
                aid = _analysis_id_of(_segment(src, out, relay))
                made["analysis_id"] = aid
            result = getattr(audial, sdk_name)(
                analysis_id=aid,
                results_folder=str(out),
                on_progress=relay.on_progress,
                max_wait=_settings.job_timeout_s + SDK_WAIT_HEADROOM_S,
            )
            return {**shape(result), "analysis_id": aid}

        return slug, call

    return await _execute(ctx, tool, plan, relay=relay, made=made)


def _as_dict(result: Any) -> dict[str, Any]:
    return dict(result) if isinstance(result, dict) else {"result": result}


def _word(entry: Any) -> str:
    """The text of one timed word: `[word, start, end]` or `{"word"|"text", ...}`."""
    if isinstance(entry, (list, tuple)) and entry and isinstance(entry[0], str):
        return entry[0].strip()
    if isinstance(entry, dict):
        text = entry.get("word", entry.get("text"))
        return text.strip() if isinstance(text, str) else ""
    return ""


def _timed_lines(lines: Any) -> list[dict[str, Any]]:
    """Lyric lines as `{start, end, text}`; the word timings stay in the saved file."""
    timed: list[dict[str, Any]] = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        text = line.get("text")
        if not isinstance(text, str):
            text = " ".join(w for w in map(_word, line.get("words") or []) if w)
        timed.append({"start": line.get("start"), "end": line.get("end"), "text": text.strip()})
    return timed


def _lyrics_metadata(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"result": result}
    # `segments` is the same lines again in the older shape.
    shaped = {k: v for k, v in result.items() if k not in ("lines", "segments")}
    shaped["lines"] = _timed_lines(result.get("lines"))
    return shaped


@mcp.tool(title="Split stems", annotations=REMOTE)
async def stem_split(
    ctx: Context,
    file_path: FilePath,
    stems: Annotated[
        list[str] | None,
        Field(
            description=(
                "Stems to extract from: vocals, drums, bass, other, full_song_without_vocals. "
                "Default: vocals, drums, bass, other."
            )
        ),
    ] = None,
    target_bpm: Annotated[
        float | None,
        Field(description="Retime the stems to this tempo (beats per minute)."),
    ] = None,
    target_key: Annotated[
        str | None,
        Field(description="Transpose the stems to this key, e.g. 'A minor' or 'F# major'."),
    ] = None,
    algorithm: Annotated[
        str,
        Field(description="Separation algorithm. Default 'primaudio'."),
    ] = "primaudio",
) -> JobResult:
    """Split a track into separate instrument stems, optionally retimed and rekeyed.

    Use for a full song or mix when the user wants vocals, drums, bass or an instrumental on
    their own. For tempo and key alone use analyze; for sections use segment. Takes a few
    minutes and counts as one run. Stems go to a new results folder; the source is unchanged.
    """
    relay = ProgressRelay()

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = _file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.stem_split(
            file_path=str(src),
            stems=stems,
            target_bpm=target_bpm,
            target_key=target_key,
            algorithm=algorithm,
            results_folder=str(out),
            on_progress=relay.on_progress,
        )

    return await _execute(ctx, "stem_split", plan, relay=relay)


@mcp.tool(title="Analyze audio", annotations=READ_ONLY_REMOTE)
async def analyze(ctx: Context, file_path: FilePath) -> JobResult:
    """Analyze a track: BPM, key, loudness and other characteristics.

    Use this first whenever the user asks for tempo, key or loudness. It is the fastest tool
    (seconds), does not count as a run, and changes nothing. The answer is in `metadata` and
    the same data is saved to the results folder.
    """
    relay = ProgressRelay()

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = _file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.analyze(
            file_path=str(src), results_folder=str(out), on_progress=relay.on_progress
        )

    return await _execute(ctx, "analyze", plan, relay=relay)


@mcp.tool(title="Segment audio", annotations=REMOTE)
async def segment(
    ctx: Context,
    file_path: FilePath,
    genre: Annotated[
        str | None,
        Field(
            description=(
                "Genre hint for the section names, e.g. 'Tech House', 'Pop' or 'Drum N Bass' "
                "(a name Audial does not know returns the list)."
            )
        ),
    ] = None,
    edm: Annotated[
        bool | None,
        Field(
            description=(
                "true: dance section names (INTRO, UP, CHORUS, DOWN, OUTRO); false: song "
                "section names (INTRO, VERSE, UP, CHORUS, OUTRO). Default: from the genre, "
                "else from the audio."
            )
        ),
    ] = None,
    features: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional, kept for older integrations: also compute these in the same call "
                "(mode, energy, loudness, danceability, tatum, tags, lyrics). The call then "
                "waits for them, minutes for tags and lyrics. Simpler: audio_features, "
                "tag_audio or get_lyrics afterwards with the analysis_id."
            )
        ),
    ] = None,
) -> JobResult:
    """Find song sections, tempo and bars (about half a minute).

    Use for song structure. Send the mix: no stems needed, and it counts as one run. The result
    has the sections (with their phrases) and a tempo map. Beats and bars are not listed one by
    one: `grid` describes them -- where each constant-tempo piece starts, its BPM, and its bar
    and beat. Every beat time is start + k * 60 / bpm, and most tracks have one piece. The
    `analysis_id` it returns adds audio_features, tag_audio, get_lyrics or generate_samples to
    this analysis without another run. For tempo and key alone, analyze is quicker.
    """
    relay = ProgressRelay()

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = _file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: _without_alias(
            _segment(src, out, relay, genre=genre, edm=edm, features=features)
        )

    return await _execute(ctx, "segment", plan, relay=relay)


@mcp.tool(title="Audio features", annotations=REMOTE)
async def audio_features(
    ctx: Context,
    analysis_id: AnalysisId = None,
    file_path: AnalysisFilePath = None,
) -> JobResult:
    """Measure energy, danceability, loudness, mode, key and tatum, for the song and each section.

    Give the analysis_id from segment: nothing is uploaded and it is included in that analysis's
    run. A file_path runs segment first (one run). Needs no stems, so it can run straight after
    segment. Energy and danceability run 0-1: energy reflects loudness, rhythmic activity and
    low end; danceability, how strong and regular the beat is. Both are measured per section,
    so a drop reads higher than its breakdown. Loudness is in LUFS. The values are in
    `metadata` (`song`, `sections`) and saved to the results folder.
    """
    return await _addon(ctx, "audio_features", "audio_features", analysis_id, file_path, _as_dict)


@mcp.tool(title="Tag audio", annotations=REMOTE)
async def tag_audio(
    ctx: Context,
    analysis_id: AnalysisId = None,
    file_path: AnalysisFilePath = None,
) -> JobResult:
    """Tag a track: genre, moods, instruments and artist tags for the mix and each part, plus a written description.

    Give the analysis_id from segment: nothing is uploaded and it is included in that analysis's
    run. A file_path runs segment first (one run). Tags read the track's stems, which Audial
    makes automatically after each analysis: right after a new analysis this waits for them (a
    few minutes). The tags are in `metadata` (`genre`, `song_tags`, `description`) and saved to
    the results folder.
    """  # noqa: E501 (the first line is the bundle manifest's text, word for word)
    return await _addon(ctx, "tag_audio", "tags", analysis_id, file_path, _as_dict)


@mcp.tool(title="Get lyrics", annotations=REMOTE)
async def get_lyrics(
    ctx: Context,
    analysis_id: AnalysisId = None,
    file_path: AnalysisFilePath = None,
) -> JobResult:
    """Transcribe a track's lyrics as timed lines.

    Give the analysis_id from segment: nothing is uploaded and it is included in that analysis's
    run. A file_path runs segment first (one run). Lyrics read the vocal stem, which Audial makes
    automatically after each analysis: right after a new analysis this waits for it (a few
    minutes). `metadata.lines` has one {start, end, text} per line in seconds on the track's
    timeline, `lyrics` the whole text and `section_lyrics` one entry per section. Word timings
    are in the saved lyrics.json. An instrumental comes back with `is_instrumental` true.
    """
    return await _addon(ctx, "get_lyrics", "lyrics", analysis_id, file_path, _lyrics_metadata)


@mcp.tool(title="Master a track", annotations=REMOTE)
async def master(
    ctx: Context,
    file_path: FilePath,
    reference_file: Annotated[
        str | None,
        FileArg,
        Field(
            description=(
                "Optional reference track whose loudness and tone the master should match."
            )
        ),
    ] = None,
) -> JobResult:
    """Master a mix, optionally matching a reference track.

    Use for a finished stereo mix, not for separate stems. Pass a reference track when the user
    names one. Takes a few minutes and counts as one run. The master goes to a new results
    folder; the source is unchanged.
    """
    relay = ProgressRelay()

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = _file(file_path, AUDIO_EXTENSIONS, "audio file")
        ref = (
            _file(reference_file, AUDIO_EXTENSIONS, "reference audio file")
            if reference_file
            else None
        )
        return slugify(src.stem), lambda out: audial.master(
            file_path=str(src),
            reference_file=str(ref) if ref else None,
            results_folder=str(out),
            on_progress=relay.on_progress,
        )

    return await _execute(ctx, "master", plan, relay=relay)


@mcp.tool(title="Generate a sample pack", annotations=REMOTE)
async def generate_samples(
    ctx: Context,
    file_path: Annotated[
        str | None, FileArg, Field(description=f"{AUDIO_FILE} Give this or analysis_id.")
    ] = None,
    analysis_id: AnalysisId = None,
    job_type: Annotated[
        str | None,
        Field(
            description=(
                "Sample pack job type accepted by Audial (default engine choice when omitted)."
            )
        ),
    ] = None,
    components: Annotated[
        list[str] | None,
        Field(description="Which components to sample, e.g. ['drums', 'bass']."),
    ] = None,
    genre: Annotated[str | None, Field(description="Genre hint.")] = None,
) -> JobResult:
    """Extract a sample pack (one-shots and loops) from a track.

    Use when the user wants drum hits or loops taken from a finished track. With the
    analysis_id from segment, the pack is built from that analysis's sections and the stems
    Audial made for it: nothing is uploaded and it is included in the analysis's run (right
    after a new analysis it waits a few minutes for the stems). With file_path it runs the
    `sample-pack` pipeline: prepares the track (analysis and stems together), then builds the
    pack; one call, counted as one run, each step reported as it starts and finishes. If that
    call times out the job keeps running: pipeline_status finds it. Samples go to a new
    results folder.
    """
    relay = ProgressRelay()

    def plan() -> tuple[str, Callable[[Path], Any]]:
        given, src = _analysis_source(analysis_id, file_path)
        # Only the source that was given is passed, so an SDK without `analysis_id` still
        # takes a file.
        source = {"file_path": str(src)} if src is not None else {"analysis_id": given}
        slug = slugify(src.stem) if src is not None else slugify(given or "", "analysis")
        return slug, lambda out: audial.generate_samples(
            **source,
            job_type=job_type,
            components=components,
            genre=genre,
            results_folder=str(out),
            on_progress=relay.on_progress,
        )

    return await _execute(ctx, "generate_samples", plan, relay=relay)


@mcp.tool(title="Audio to MIDI", annotations=REMOTE)
async def generate_midi(
    ctx: Context,
    file_path: FilePath,
    bpm: Annotated[
        float | None,
        Field(description="Override the detected tempo for note quantisation."),
    ] = None,
) -> JobResult:
    """Transcribe an audio file to MIDI.

    Use to get notes out of audio. On a full mix, run stem_split first and transcribe the stem
    you want. Counts as one run. MIDI files go to a new results folder; the source is unchanged.
    """
    relay = ProgressRelay()

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = _file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.generate_midi(
            file_path=str(src),
            bpm=bpm,
            results_folder=str(out),
            max_wait=_settings.job_timeout_s + SDK_WAIT_HEADROOM_S,
            on_progress=relay.on_progress,
        )

    return await _execute(ctx, "generate_midi", plan, relay=relay)


@mcp.tool(title="Generate music", annotations=REMOTE)
async def generate_music(
    ctx: Context,
    prompt: Annotated[
        str,
        Field(
            description=(
                "Style description: genre, mood, instruments, vocal character. Tempo and key "
                "words in the prompt steer the model more than the numeric bpm/key fields, "
                "which are hints, not constraints."
            )
        ),
    ],
    task_type: Annotated[
        str,
        Field(description=("text2music (default), cover, remix, lego, or understand.")),
    ] = "text2music",
    lyrics: Annotated[
        str | None,
        Field(
            description=(
                "Lyrics with optional section tags like [Verse] and [Chorus]. Omit for "
                "instrumental."
            )
        ),
    ] = None,
    source_file: Annotated[
        str | None,
        FileArg,
        Field(description=("Local audio file; required for remix, lego, understand.")),
    ] = None,
    reference_file: Annotated[
        str | None,
        FileArg,
        Field(description="Local audio file; required for cover."),
    ] = None,
    bpm: Annotated[int | None, Field(description="Tempo hint in beats per minute.")] = None,
    key_scale: Annotated[str | None, Field(description="Key hint, e.g. 'G major'.")] = None,
    time_signature: Annotated[str | None, Field(description="e.g. '4/4'.")] = None,
    audio_duration: Annotated[
        float | None,
        Field(description="Length in seconds (10-600)."),
    ] = None,
    vocal_language: Annotated[
        str,
        Field(description="Language code for vocals, e.g. 'en'."),
    ] = "en",
    audio_cover_strength: Annotated[
        float | None,
        Field(description="0-1 fidelity to the original for cover/remix."),
    ] = None,
    repainting_start: Annotated[
        float | None,
        Field(description="Remix start time in seconds."),
    ] = None,
    repainting_end: Annotated[
        float | None,
        Field(description="Remix end time in seconds (-1 = end)."),
    ] = None,
    batch_size: Annotated[int, Field(description="Number of variations, 1-8.")] = 1,
    seed: Annotated[int | None, Field(description="Seed for reproducible output.")] = None,
    audio_format: Annotated[
        str,
        Field(description="mp3, wav, flac, opus or aac."),
    ] = "mp3",
    track_name: Annotated[
        str | None,
        Field(
            description=(
                "Track to emphasise for lego: vocals, drums, bass, guitar, piano, "
                "strings, synth, other."
            )
        ),
    ] = None,
    instrumental: Annotated[bool, Field(description="Generate without vocals.")] = False,
    negative_prompt: Annotated[
        str | None,
        Field(description="What to avoid, comma-separated."),
    ] = None,
) -> JobResult:
    """Generate music with the Audial music model.

    Text to music, covers, remixes, lego (re-render with one track emphasised),
    or analysis (understand). Use for new or transformed music, not to edit an existing mix:
    that is master or stem_split. Takes a few minutes and counts as one run, and audio goes to
    a new results folder.
    """
    relay = ProgressRelay()

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = _file(source_file, AUDIO_EXTENSIONS, "source audio file") if source_file else None
        ref = (
            _file(reference_file, AUDIO_EXTENSIONS, "reference audio file")
            if reference_file
            else None
        )
        slug = slugify("_".join(prompt.split()[:3]), fallback="music")
        return slug, lambda out: audial.generate_music(
            prompt=prompt,
            task_type=task_type,
            lyrics=lyrics,
            source_file=str(src) if src else None,
            reference_file=str(ref) if ref else None,
            bpm=bpm,
            key_scale=key_scale,
            time_signature=time_signature,
            audio_duration=audio_duration,
            vocal_language=vocal_language,
            audio_cover_strength=audio_cover_strength,
            repainting_start=repainting_start,
            repainting_end=repainting_end,
            batch_size=batch_size,
            seed=seed,
            audio_format=audio_format,
            track_name=track_name,
            instrumental=instrumental,
            negative_prompt=negative_prompt,
            results_folder=str(out),
            max_wait=_settings.job_timeout_s + SDK_WAIT_HEADROOM_S,
            on_progress=relay.on_progress,
        )

    return await _execute(ctx, "generate_music", plan, relay=relay)


@mcp.tool(title="Resynthesize a one-shot into a synth preset", annotations=REMOTE)
async def sound2vital(
    ctx: Context,
    file_path: Annotated[
        str,
        FileArg,
        Field(
            description=(
                f"{RESYNTH_INPUT} The result is an editable Audial Synth (.vital) preset that "
                "reproduces its timbre."
            )
        ),
    ],
) -> JobResult:
    """Turn one isolated sound into an editable Audial Synth preset.

    Not for loops or melodies, which give a poor preset. If the result's summary says the match
    is weak, tell the user and suggest trimming the input to one note.
    """
    relay = ProgressRelay()

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = _file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.sound2vital(
            file_path=str(src),
            results_folder=str(out),
            max_wait=_settings.job_timeout_s + SDK_WAIT_HEADROOM_S,
            on_progress=relay.on_progress,
        )

    return await _execute(ctx, "sound2vital", plan, relay=relay)


@mcp.tool(title="Resynthesize a one-shot into a Serum 2 preset", annotations=REMOTE)
async def sound2serum(
    ctx: Context,
    file_path: Annotated[
        str,
        FileArg,
        Field(
            description=(
                f"{RESYNTH_INPUT} The result is an editable Xfer Serum 2 (.SerumPreset) preset "
                "that reproduces its timbre. No plugin needed -- open it in Serum 2.1+."
            )
        ),
    ],
) -> JobResult:
    """Turn one isolated sound into an editable Xfer Serum 2 preset.

    Not for loops or melodies, which give a poor preset. If the result's summary says the match
    is weak, tell the user and suggest trimming the input to one note.
    """
    relay = ProgressRelay()

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = _file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.sound2serum(
            file_path=str(src),
            results_folder=str(out),
            max_wait=_settings.job_timeout_s + SDK_WAIT_HEADROOM_S,
            on_progress=relay.on_progress,
        )

    return await _execute(ctx, "sound2serum", plan, relay=relay)


@mcp.tool(title="Sing lyrics in a reference voice", annotations=REMOTE)
async def text2vox(
    ctx: Context,
    reference_file: Annotated[
        str,
        FileArg,
        Field(description="Short clip of the reference voice (timbre)."),
    ],
    lyrics: Annotated[
        str,
        Field(
            description=(
                "Lyrics to sing. With lyrics_mode 'per_note' this is pipe-delimited, one "
                "token per MIDI note, e.g. 'amazing|-|-|grace|-|how|sweet|the|sound'; "
                "otherwise it is ordinary text."
            )
        ),
    ],
    midi_file: Annotated[
        str | None,
        FileArg,
        Field(
            description=(
                "MIDI melody (.mid). Give this or melody_audio_file. text2vox sings one note "
                "at a time: the MIDI must be a single melody line, not chords or harmony. Two "
                "notes that start together, or a note that starts more than 50 ms before the "
                "previous one ends, is refused before anything runs, naming the note number "
                "and its time (overlaps of 50 ms or less are fine). With lyrics_mode=per_note, "
                "a note with no length or a note that never ends is refused too."
            )
        ),
    ] = None,
    melody_audio_file: Annotated[
        str | None,
        FileArg,
        Field(description="Audio whose melody is transcribed and followed, if no MIDI."),
    ] = None,
    word_timestamps_file: Annotated[
        str | None,
        FileArg,
        Field(description="Optional JSON word timings."),
    ] = None,
    lyrics_mode: Annotated[
        str,
        Field(
            description=(
                "How the lyrics line up with the melody. 'auto' (default): ordinary text, "
                "one word per MIDI note, aligned for you (word_timestamps_file helps). "
                "'1to1': the text is passed to the synthesizer untouched. 'per_note': "
                "exact control -- the lyrics are pipe-delimited with one token per MIDI "
                "note, in order. In per_note mode a token is either a syllable sung on "
                "that note ('grace') or a HOLD, which keeps the previous syllable going "
                "through this note: write a hold as '-' (preferred), '+', or an empty "
                "token (a double pipe). So 'amazing|-|-|grace|-|how|sweet|the|sound' sings "
                "'amazing' across notes 1-3 and 'grace' across notes 4-5, and needs a MIDI "
                "file with exactly 9 notes. There is no rest token: a note that should be "
                "silent must not be in the MIDI. The token count has to equal the note "
                "count and the first token has to be a syllable, never a hold. The MIDI must also "
                "be a single melody line (one note at a time, no chords or harmony)."
            )
        ),
    ] = "auto",
    reference_text: Annotated[
        str | None,
        Field(description="Transcript of the reference clip, if known."),
    ] = None,
    cfg_strength: Annotated[float | None, Field(description="Guidance strength.")] = None,
    nfe_steps: Annotated[
        int | None,
        Field(description="Synthesis steps; more is slower and cleaner."),
    ] = None,
    pitch_shift: Annotated[
        float | None,
        Field(description="Semitones to shift the melody."),
    ] = None,
    strict_pitch: Annotated[
        bool | None,
        Field(description="Force exact pitch to the melody."),
    ] = None,
    bend_smoothing_ms: Annotated[
        float | None,
        Field(description="Pitch-bend smoothing window in ms."),
    ] = None,
    no_pitch_bends: Annotated[bool | None, Field(description="Disable pitch bends.")] = None,
    leading_silence_s: Annotated[
        float | None,
        Field(description="Silence before the first note, seconds."),
    ] = None,
    seed: Annotated[int | None, Field(description="Seed for reproducible output.")] = None,
) -> JobResult:
    """Synthesize a sung vocal (and MIDI) from lyrics, a melody and a reference voice.

    Needs both a reference clip of the voice and a melody, as MIDI or audio. A MIDI melody must be
    one note at a time: a single melody line, not chords or harmony. lyrics_mode decides
    how the lyrics are lined up with the melody's notes; "per_note" is the one that gives exact
    control. See the lyrics_mode and lyrics descriptions. Takes a few minutes and counts as one
    run. Audio and MIDI go to a new results folder.
    """
    relay = ProgressRelay()

    def plan() -> tuple[str, Callable[[Path], Any]]:
        ref = _file(reference_file, AUDIO_EXTENSIONS, "reference audio file")
        mid = _file(midi_file, MIDI_EXTENSIONS, "MIDI file") if midi_file else None
        mel = (
            _file(melody_audio_file, AUDIO_EXTENSIONS, "melody audio file")
            if melody_audio_file
            else None
        )
        wts = (
            _file(word_timestamps_file, JSON_EXTENSIONS, "word timestamps JSON file")
            if word_timestamps_file
            else None
        )
        return slugify(ref.stem), lambda out: audial.text2vox(
            reference_file=str(ref),
            lyrics=lyrics,
            midi_file=str(mid) if mid else None,
            melody_audio_file=str(mel) if mel else None,
            word_timestamps_file=str(wts) if wts else None,
            lyrics_mode=lyrics_mode,
            reference_text=reference_text,
            cfg_strength=cfg_strength,
            nfe_steps=nfe_steps,
            pitch_shift=pitch_shift,
            strict_pitch=strict_pitch,
            bend_smoothing_ms=bend_smoothing_ms,
            no_pitch_bends=no_pitch_bends,
            leading_silence_s=leading_silence_s,
            seed=seed,
            results_folder=str(out),
            max_wait=_settings.job_timeout_s + SDK_WAIT_HEADROOM_S,
            on_progress=relay.on_progress,
        )

    return await _execute(ctx, "text2vox", plan, relay=relay)


@mcp.tool(title="Service status", annotations=READ_ONLY_REMOTE)
async def service_status(
    ctx: Context,
    tool: Annotated[
        str | None,
        Field(
            description=(
                "Audial tool to report on: stem_split, analyze, segment, audio_features, "
                "tag_audio, get_lyrics, master, generate_samples, generate_midi, "
                "generate_music, sound2vital, sound2serum or text2vox. Omit for all of them."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Which Audial tools have a worker awake right now, and what a cold one will cost.

    Audial's GPU workers scale to zero. A tool reported `warm: false` will spend
    `expectedColdStartMs` starting a worker before it does any work -- a few minutes, once --
    on top of the job's own `expectedRunMs`. Worth checking before starting a long job, and
    worth telling the user about with the `notice` text when something is cold: otherwise the
    wait looks like a hang. Nothing here starts a job or costs anything.
    """
    if _startup_error is not None:
        raise ToolError(str(_startup_error))
    try:
        if (
            tool is not None
            and tool not in STATUS_FUNCTIONS
            and tool not in _ADDON_FUNCTIONS
            and tool not in _API_FUNCTIONS
        ):
            known = ", ".join(sorted({*STATUS_FUNCTIONS, *_ADDON_FUNCTIONS}))
            raise ValidationError(
                f"{tool!r} is not an Audial tool. Give one of: {known}, or omit it for all."
            )
        await _apply_credentials(ctx)
        function = _ADDON_FUNCTIONS.get(tool, tool) if tool is not None else None
        doc = await anyio.to_thread.run_sync(lambda: audial.status(function))
        if not isinstance(doc, dict):
            return {"result": _redact(doc)}

        result: dict[str, Any] = _redact(doc)
        cold = [
            item.get("function")
            for item in (doc.get("items") or [])
            if isinstance(item, dict) and item.get("warm") is False
        ]
        if cold:
            # The same sentence the SDK's CLI, the website and both plugins show, so the
            # user hears one explanation for the wait wherever they are.
            coldest = max(
                (
                    item.get("expectedColdStartMs") or 0
                    for item in doc.get("items") or []
                    if isinstance(item, dict) and item.get("warm") is False
                ),
                default=0,
            )
            result["cold"] = cold
            result["notice"] = audial.warmup.cold_start_message(coldest)
        return result
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        raise _tool_error(exc, tool="service_status") from None


# Pipeline ids are Firebase push keys; the id is interpolated into the request path, so it is
# checked before it leaves here rather than trusted because the model supplied it.
_PIPELINE_ID_RE = _ID_RE


@mcp.tool(title="Pipeline status", annotations=READ_ONLY_REMOTE)
async def pipeline_status(
    ctx: Context,
    pipeline_id: Annotated[
        str,
        Field(
            description=(
                "Id of a pipeline, as returned in `execution_id` by generate_samples on a file "
                "or by segment with `features`."
            )
        ),
    ],
) -> dict[str, Any]:
    """Look up an Audial pipeline by id: its state, each step, and the outputs so far.

    Useful when a generate_samples call on a file, or a segment call with `features`, timed
    out: the pipeline keeps running on Audial's side and this says how far it got.
    """
    if _startup_error is not None:
        raise ToolError(str(_startup_error))
    try:
        if not _PIPELINE_ID_RE.fullmatch(pipeline_id):
            raise ValidationError(
                f"{pipeline_id!r} is not a pipeline id (letters, digits, '_' and '-' only). "
                "Use the execution_id a segment or generate_samples result returned."
            )
        await _apply_credentials(ctx)
        status = await anyio.to_thread.run_sync(lambda: audial.pipelines.get(pipeline_id))
        return _redact(status) if isinstance(status, dict) else {"result": _redact(status)}
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        raise _tool_error(exc, tool="pipeline_status") from None


@mcp.tool(title="List previous results", annotations=LOCAL_READ_ONLY)
async def list_results(
    tool: Annotated[
        str | None,
        Field(description="Filter by tool name, e.g. 'stem_split'. Omit for all tools."),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Maximum number of jobs to return, newest first.", ge=1, le=100),
    ] = 10,
) -> list[JobResult]:
    """List previous Audial results in the results folder, newest first.

    Use when the user asks where an earlier output went. Reads the local folder only: no account
    needed and nothing is sent anywhere.
    """
    if _startup_error is not None:
        raise ToolError(str(_startup_error))
    try:
        return list_jobs(_settings.results_dir, _tool_filter(tool) if tool else None, limit)
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        raise _tool_error(exc, tool="list_results") from None


class Account(BaseModel):
    """Which Audial account this server is using."""

    signed_in: bool
    email: str | None = None
    source: str | None = Field(
        default=None, description="Where the credentials come from: 'sign-in' or 'api-key'."
    )
    summary: str


API_KEY_CONFIGURED = (
    "This server is configured with an API key (AUDIAL_API_KEY in its env), which takes "
    "priority. Remove it from the MCP client's config and restart to use browser sign-in."
)


def _account() -> Account:
    if _remote is not None:
        access = remote_mode.caller()
        email = (access.claims or {}).get("email") if access else None
        return Account(
            signed_in=access is not None,
            email=email,
            source="api-key",
            summary=f"Connected to Audial as {email or 'an Audial account'}.",
        )
    saved = _saved_sign_in()
    if _settings.api_key:
        return Account(
            signed_in=True,
            source="api-key",
            summary="Using the API key from the server's configuration.",
        )
    if saved:
        who = saved.get("email") or "your Audial account"
        return Account(
            signed_in=True,
            email=saved.get("email"),
            source="sign-in",
            summary=f"Signed in as {who}.",
        )
    return Account(
        signed_in=False,
        summary="Not signed in. Call sign_in, or any Audial tool, to sign in through the browser.",
    )


@mcp.tool(title="Show the Audial account in use", annotations=LOCAL_READ_ONLY)
async def account() -> Account:
    """Show whether this computer is signed in to Audial, and as whom.

    Use when a tool reports a credentials problem or the user asks which account is in use.
    Reads local settings only.
    """
    try:
        return _account()
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        raise _tool_error(exc, tool="account") from None


@mcp.tool(title="Sign in to Audial", annotations=REMOTE)
async def sign_in(
    ctx: Context,
    switch_account: Annotated[
        bool,
        Field(description="Sign out first and sign in again, to change or repair the account."),
    ] = False,
) -> Account:
    """Sign in to Audial through the user's browser. Opens an approval page and waits for it.

    Not needed before other tools: they start the same sign-in when nobody is signed in.
    """
    try:
        if _settings.api_key:
            raise ConfigError(API_KEY_CONFIGURED)
        if switch_account:
            _saved_sign_in()
            await anyio.to_thread.run_sync(device_login.logout)
        if _saved_sign_in() is None:
            await _sign_in(ctx)
        return _account()
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        raise _tool_error(exc, tool="sign_in") from None


@mcp.tool(title="Sign out of Audial", annotations=SIGN_OUT)
async def sign_out() -> Account:
    """Sign out of Audial on this computer and disconnect it from the account.

    Only when the user asks to sign out or switch accounts. The next job asks them to sign in
    again. Results already downloaded stay where they are.
    """
    global _pending_sign_in
    try:
        if _settings.api_key:
            raise ConfigError(API_KEY_CONFIGURED)
        _saved_sign_in()
        _pending_sign_in = None
        await anyio.to_thread.run_sync(device_login.logout)
        return _account()
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        raise _tool_error(exc, tool="sign_out") from None


# --- remote mode: the server ChatGPT and other remote clients connect to --------------------

# Tools that only make sense on the user's own computer: the local results folder, and the
# browser sign-in that saves a key on this machine.
LOCAL_ONLY_TOOLS = frozenset({"list_results", "sign_in", "sign_out"})

REMOTE_FILE_NOTE = (
    'A file the user attached to the chat, or {"download_url": "https://...", "file_id": '
    '"any id"} for a file at a public https link (the link to the file itself).'
)


# Tools whose local description would be wrong for a remote caller.
REMOTE_DESCRIPTIONS = {
    "account": (
        "Show which Audial account this connection uses, by its email. Use when a tool reports "
        "a credentials problem or the user asks which account is in use."
    ),
}


def _remote_instructions(settings: remote_mode.RemoteSettings) -> str:
    hours = max(1, settings.results_ttl_s // 3600)
    return (
        "Audial runs hosted audio tools on the user's Audial account. Give each tool its audio "
        "as a file the user attached to the chat, or as an https link to the file itself. "
        f"Results come back as download links (each file's `path`), valid for {hours} h: give "
        "them to the user. A job still running after "
        f"{settings.wait_s} s returns status 'running' with a job_id: tell the user it is in "
        "progress, then call check_job with that job_id for the result. "
        "segment takes about half a minute and returns an analysis_id; audio_features, "
        "tag_audio, get_lyrics and generate_samples take it to add to that analysis without "
        "uploading again (given a file, they run segment first). Tags, lyrics and sample packs "
        "wait a few minutes after a new analysis while its stems are made. Audial's workers "
        "scale to zero, so the first job after an idle period spends extra time starting one: "
        "that shows up as a 'Warming up the machine' line, worth relaying as the reason for "
        "the wait; service_status says in advance which tools are warm. If a tool reports "
        "that the account's plan does not cover a run, pass that on as it is. "
        "Only use audio the user has the rights to, and for text2vox a reference voice that is "
        "the user's own or one they have permission to use. "
        "sound2vital and sound2serum model one isolated sound: a loop, melody or drum pattern "
        "gives a poor preset, so say so before running one and suggest trimming it to one "
        "note. After either tool, relay anything in `summary` about the match."
    )


def _remote_file_schema(description: str) -> dict[str, Any]:
    """A file argument as ChatGPT attaches it (`openai/fileParams`)."""
    return {
        "type": "object",
        "description": description,
        "properties": {
            "download_url": {"type": "string", "description": "https link to the file."},
            "file_id": {"type": "string", "description": "Id of the attached file."},
            "mime_type": {"type": "string", "description": "The file's type, if known."},
            "file_name": {"type": "string", "description": "The file's name, if known."},
        },
        "required": ["download_url", "file_id"],
    }


def _remote_description(description: str) -> str:
    """A file argument's description with the local-path wording replaced."""
    text = description.replace(AUDIO_FILE, "An audio file (.wav, .mp3, .aif, .flac, .m4a, ...).")
    text = text.replace("Local audio file", "Audio file")
    return f"{text} {REMOTE_FILE_NOTE}"


def _remote_tool(tool: Tool, settings: remote_mode.RemoteSettings) -> Tool:
    """A copy of one tool for the remote server: file arguments as file objects, plus `_meta`."""
    params = copy.deepcopy(tool.parameters)
    properties = params.get("properties", {})
    files = sorted(name for name in properties if name in FILE_PARAMS)
    for name in files:
        properties[name] = _remote_file_schema(
            _remote_description(properties[name].get("description", ""))
        )
    meta = dict(tool.meta or {})
    if files:
        meta["openai/fileParams"] = files
    if settings.authorization_server:
        # Every tool needs the user's account (OpenAI reads the `_meta` mirror of this field).
        meta["securitySchemes"] = [{"type": "oauth2", "scopes": []}]
    description = REMOTE_DESCRIPTIONS.get(tool.name, tool.description)
    return tool.model_copy(
        update={"parameters": params, "meta": meta or None, "description": description}
    )


def create_http_app(
    settings: remote_mode.RemoteSettings,
    verifier: TokenVerifier | None = None,
) -> Starlette:
    """Switch this process to remote mode and return the ASGI app to serve.

    The remote server is a separate `MCPServer` holding copies of the tools, so the stdio
    server's own tool list is never changed. The tool functions are shared; they read
    `_remote` to fetch their inputs, publish their results and run their jobs in the
    background. The SDK's credential lookups are pointed at the request being served, and a
    key in this process's environment is ignored: each caller brings their own.
    """
    global _remote, _settings
    if _settings.api_key:
        log.warning("AUDIAL_API_KEY is ignored with --http: each request brings its own key")
    for name in ("AUDIAL_API_KEY", "AUDIAL_USER_ID"):
        os.environ.pop(name, None)
    if _settings.api_base_url:
        os.environ["AUDIAL_API_BASE_URL"] = _settings.api_base_url
    remote = remote_mode.Remote.create(settings)
    remote.restore_sdk = remote_mode.install_sdk_credentials()
    remote.saved_settings = _settings
    _settings = replace(_settings, api_key=None, user_id=None, results_dir=remote.results.root)
    _remote = remote

    tools = [
        _remote_tool(tool, settings)
        for tool in mcp._tool_manager.list_tools()  # noqa: SLF001 (the Tool objects, not wire copies)
        if tool.name not in LOCAL_ONLY_TOOLS
    ]
    server = MCPServer(
        "audial",
        instructions=_remote_instructions(settings),
        version=__version__,
        tools=tools,
    )
    server.add_tool(
        check_job,
        title="Check on a running job",
        description=(
            "Get the result of a job that was still running when its tool returned. Waits up "
            f"to {settings.wait_s} s for it to finish; if it is still running, the result says "
            "so again with the same job_id: tell the user it is in progress and check again. "
            "Starts nothing and costs nothing."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, open_world_hint=False, idempotent_hint=True
        ),
        meta=(
            {"securitySchemes": [{"type": "oauth2", "scopes": []}]}
            if settings.authorization_server
            else None
        ),
    )
    return remote_mode.build_app(server, remote, verifier or remote_mode.AudialKeyVerifier())


def disable_remote() -> None:
    """Undo `create_http_app` (for tests): the stdio server's state comes back."""
    global _remote, _settings
    if _remote is None:
        return
    if _remote.restore_sdk is not None:
        _remote.restore_sdk()
    if _remote.saved_settings is not None:
        _settings = _remote.saved_settings
    _remote = None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="audial-mcp",
        description=(
            "Audial's audio tools as an MCP server. Without options it speaks MCP over stdio "
            "to the client that started it."
        ),
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help=(
            "serve streamable HTTP at /mcp for remote clients instead; each request "
            "authenticates with its own Audial API key (Authorization: Bearer aud_...)"
        ),
    )
    parser.add_argument("--host", help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port to listen on (default $PORT or 8000)")
    parser.add_argument(
        "--public-url",
        help="the URL clients reach this server at, for download links (default http://host:port)",
    )
    parser.add_argument(
        "--authorization-server",
        help="OAuth authorization server that issues Audial keys, published as RFC 9728 metadata",
    )
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        # Arguments used to be ignored entirely; a client config that passes some keeps working.
        log.warning("ignoring unknown arguments: %s", " ".join(unknown))
    return args


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    args = _parse_args(argv)
    global _settings, _startup_error
    try:
        _settings = load_settings()
        _startup_error = None
    except ConfigError as exc:
        _settings = fallback_settings()
        _startup_error = exc
    if args.http:
        _serve_http(args)
        return
    log.info(
        "audial-mcp %s; results dir %s; credentials %s",
        __version__,
        _settings.results_dir,
        "API key from env" if _settings.api_key else "browser sign-in",
    )
    if _startup_error is not None:
        # Serve anyway: the client sees a working server whose tools all explain this.
        log.error("configuration error; every tool call will return it: %s", _startup_error)
    mcp.run()  # stdio


def _serve_http(args: argparse.Namespace) -> None:
    import uvicorn

    try:
        settings = remote_mode.remote_settings(
            host=args.host,
            port=args.port,
            public_url=args.public_url,
            authorization_server=args.authorization_server,
            env=BOOT_ENV,
        )
    except ValueError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from None
    if _startup_error is not None:
        log.error("configuration error; every tool call will return it: %s", _startup_error)
    app = create_http_app(settings)
    log.info("audial-mcp %s over streamable HTTP: %s", __version__, remote_mode.describe(settings))
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
