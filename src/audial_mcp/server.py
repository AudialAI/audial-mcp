"""audial MCP server: tools that wrap the Audial SDK."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import anyio
import audial
from audial.api import device_login
from audial.utils import config as sdk_config
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from audial_mcp import __version__
from audial_mcp.config import (
    SIGN_IN_WAIT_S,
    ConfigError,
    Settings,
    fallback_settings,
    load_settings,
)
from audial_mcp.errors import SignInRequired, to_tool_error
from audial_mcp.jobs import run_job
from audial_mcp.results import JobResult, inventory, list_jobs, new_job_dir, slugify
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
    "sound2vital, text2vox); progress notifications report elapsed time. Some tools need an "
    "active Audial subscription for every tool except list_results; relay the error text if "
    "one is returned. No API key is needed: if the user is not signed in, the first tool call "
    "opens a sign-in page in their browser and continues once they approve. If a tool instead "
    "returns a sign-in link, give the user that link and the code, and retry when they say "
    "they have approved it."
)

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

# The SDK is given a little more patience than `run_job`'s watchdog, so the watchdog fires first
# and the user gets `JobTimeout`'s guidance rather than a bare SDK timeout.
SDK_WAIT_HEADROOM_S = 60

REMOTE = ToolAnnotations(read_only_hint=False, open_world_hint=True, idempotent_hint=False)
READ_ONLY_REMOTE = ToolAnnotations(read_only_hint=True, open_world_hint=True)
LOCAL_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# A plan validates the tool's arguments and returns the job's folder-name slug together with the
# SDK call to run in it. Tools hand one to `_execute`, which runs it inside the try block so a
# bad path becomes a readable ToolError just like a failure from the service does.
Plan = Callable[[], tuple[str, Callable[[Path], Any]]]


def _execution_id(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("execution_id", "exe_id", "exeId"):
        if isinstance(raw.get(key), str):
            return raw[key]
    execution = raw.get("execution")
    if isinstance(execution, dict):
        for key in ("exeId", "exe_id", "execution_id"):
            if isinstance(execution.get(key), str):
                return execution[key]
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
    candidates = (
        _settings.api_key,
        _settings.user_id,
        signed_in.get("api_key"),
        signed_in.get("user_id"),
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
    """Replace any configured credential appearing inside `text`."""
    for secret in _secrets():
        text = text.replace(secret, REDACTED)
    return text


def _tool_error(exc: BaseException, *, tool: str, execution_id: str | None = None) -> ToolError:
    """`to_tool_error`, with credentials scrubbed out of the message it produces.

    Upstream error text can quote the request it failed on, which may carry the user id.
    """
    error = to_tool_error(
        exc, tool=tool, execution_id=execution_id, api_key_from_env=bool(_settings.api_key)
    )
    return ToolError(_redact_text(str(error)))


def _metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"result": _redact(raw)}
    return _redact({k: v for k, v in raw.items() if k != "files"})


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
    """
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


async def _execute(ctx: Context, tool: str, plan: Plan) -> JobResult:
    """Common path: validate → credentials → job folder → run in thread → inventory → JobResult."""
    if _startup_error is not None:
        raise ToolError(str(_startup_error))
    execution_id: str | None = None
    output_dir: Path | None = None
    try:
        slug, call = plan()
        await _apply_credentials(ctx)
        output_dir = new_job_dir(_settings.results_dir, tool, slug)
        started = time.monotonic()
        log.info("%s: starting; results -> %s", tool, output_dir)
        await ctx.report_progress(0, message=f"{tool}: starting; results -> {output_dir}")
        raw = await run_job(ctx, tool, lambda: call(output_dir), timeout_s=_settings.job_timeout_s)
        execution_id = _execution_id(raw)
        files = inventory(output_dir)
        elapsed = int(time.monotonic() - started)
        summary = f"{tool} finished in {elapsed} s: {len(files)} file(s) in {output_dir}"
        log.info("%s", summary)
        # The job is done; a failed progress notification must not turn it into an error.
        with contextlib.suppress(Exception):
            await ctx.report_progress(elapsed, total=elapsed, message=summary)
        return JobResult(
            tool=tool,
            execution_id=execution_id,
            output_dir=str(output_dir),
            files=files,
            metadata=_metadata(raw),
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        if output_dir is not None:
            _discard_if_empty(output_dir)
        raise _tool_error(exc, tool=tool, execution_id=execution_id) from None


FilePath = Annotated[
    str,
    Field(
        description=(
            "Full path to a local audio file (.wav, .mp3, .aif, .aiff, .flac, .m4a, .ogg, "
            ".aac). ~ is expanded."
        )
    ),
]


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
    """Split a track into separate instrument stems, optionally retimed and rekeyed."""

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.stem_split(
            file_path=str(src),
            stems=stems,
            target_bpm=target_bpm,
            target_key=target_key,
            algorithm=algorithm,
            results_folder=str(out),
        )

    return await _execute(ctx, "stem_split", plan)


@mcp.tool(title="Analyze audio", annotations=READ_ONLY_REMOTE)
async def analyze(ctx: Context, file_path: FilePath) -> JobResult:
    """Analyze a track: BPM, key, loudness and other characteristics. Results are in `metadata`."""

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.analyze(
            file_path=str(src), results_folder=str(out)
        )

    return await _execute(ctx, "analyze", plan)


@mcp.tool(title="Segment audio", annotations=REMOTE)
async def segment(
    ctx: Context,
    file_path: FilePath,
    components: Annotated[
        list[str] | None,
        Field(description="Components to analyze, e.g. ['vocals', 'drums']."),
    ] = None,
    analysis_type: Annotated[
        str | None,
        Field(description="Analysis type accepted by Audial's segmentation."),
    ] = None,
    features: Annotated[
        list[str] | None,
        Field(description="Features to compute per segment."),
    ] = None,
    genre: Annotated[
        str | None,
        Field(description="Genre hint that improves section detection."),
    ] = None,
) -> JobResult:
    """Detect song sections (intro, verse, chorus...) and analyze components within them."""

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.segment(
            file_path=str(src),
            components=components,
            analysis_type=analysis_type,
            features=features,
            genre=genre,
            results_folder=str(out),
        )

    return await _execute(ctx, "segment", plan)


@mcp.tool(title="Master a track", annotations=REMOTE)
async def master(
    ctx: Context,
    file_path: FilePath,
    reference_file: Annotated[
        str | None,
        Field(
            description=(
                "Optional reference track whose loudness and tone the master should match."
            )
        ),
    ] = None,
) -> JobResult:
    """Master a mix, optionally matching a reference track."""

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
        ref = (
            check_file(reference_file, AUDIO_EXTENSIONS, "reference audio file")
            if reference_file
            else None
        )
        return slugify(src.stem), lambda out: audial.master(
            file_path=str(src),
            reference_file=str(ref) if ref else None,
            results_folder=str(out),
        )

    return await _execute(ctx, "master", plan)


@mcp.tool(title="Generate a sample pack", annotations=REMOTE)
async def generate_samples(
    ctx: Context,
    file_path: FilePath,
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
    """Extract a sample pack (one-shots and loops) from a track."""

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.generate_samples(
            file_path=str(src),
            job_type=job_type,
            components=components,
            genre=genre,
            results_folder=str(out),
        )

    return await _execute(ctx, "generate_samples", plan)


@mcp.tool(title="Audio to MIDI", annotations=REMOTE)
async def generate_midi(
    ctx: Context,
    file_path: FilePath,
    bpm: Annotated[
        float | None,
        Field(description="Override the detected tempo for note quantisation."),
    ] = None,
) -> JobResult:
    """Transcribe an audio file to MIDI."""

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.generate_midi(
            file_path=str(src),
            bpm=bpm,
            results_folder=str(out),
            max_wait=_settings.job_timeout_s + SDK_WAIT_HEADROOM_S,
        )

    return await _execute(ctx, "generate_midi", plan)


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
        Field(
            description=(
                "text2music (default), cover, remix, extract, lego, complete, or understand."
            )
        ),
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
        Field(
            description=(
                "Local audio file; required for remix, extract, lego, complete, understand."
            )
        ),
    ] = None,
    reference_file: Annotated[
        str | None,
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
                "Track to extract/replace for extract/lego: vocals, drums, bass, guitar, piano, "
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

    Text to music, covers, remixes, stem extraction, completion, or analysis (understand).
    """

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = (
            check_file(source_file, AUDIO_EXTENSIONS, "source audio file") if source_file else None
        )
        ref = (
            check_file(reference_file, AUDIO_EXTENSIONS, "reference audio file")
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
        )

    return await _execute(ctx, "generate_music", plan)


@mcp.tool(title="Resynthesize a one-shot into a synth preset", annotations=REMOTE)
async def sound2vital(
    ctx: Context,
    file_path: Annotated[
        str,
        Field(
            description=(
                "A short one-shot (<= 20 s) audio file. The result is an editable Audial Synth "
                "(.vital) preset that reproduces its timbre."
            )
        ),
    ],
) -> JobResult:
    """Turn a one-shot sample into an editable Audial Synth preset."""

    def plan() -> tuple[str, Callable[[Path], Any]]:
        src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
        return slugify(src.stem), lambda out: audial.sound2vital(
            file_path=str(src),
            results_folder=str(out),
            max_wait=_settings.job_timeout_s + SDK_WAIT_HEADROOM_S,
        )

    return await _execute(ctx, "sound2vital", plan)


@mcp.tool(title="Sing lyrics in a reference voice", annotations=REMOTE)
async def text2vox(
    ctx: Context,
    reference_file: Annotated[
        str,
        Field(description="Short clip of the reference voice (timbre)."),
    ],
    lyrics: Annotated[str, Field(description="Lyrics to sing.")],
    midi_file: Annotated[
        str | None,
        Field(description="MIDI melody (.mid). Give this or melody_audio_file."),
    ] = None,
    melody_audio_file: Annotated[
        str | None,
        Field(description="Audio whose melody is transcribed and followed, if no MIDI."),
    ] = None,
    word_timestamps_file: Annotated[
        str | None,
        Field(description="Optional JSON word timings."),
    ] = None,
    lyrics_mode: Annotated[
        str,
        Field(description="auto (default) or an explicit lyric alignment mode."),
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
    """Synthesize a sung vocal (and MIDI) from lyrics, a melody and a reference voice."""

    def plan() -> tuple[str, Callable[[Path], Any]]:
        ref = check_file(reference_file, AUDIO_EXTENSIONS, "reference audio file")
        mid = check_file(midi_file, MIDI_EXTENSIONS, "MIDI file") if midi_file else None
        mel = (
            check_file(melody_audio_file, AUDIO_EXTENSIONS, "melody audio file")
            if melody_audio_file
            else None
        )
        wts = (
            check_file(word_timestamps_file, JSON_EXTENSIONS, "word timestamps JSON file")
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
        )

    return await _execute(ctx, "text2vox", plan)


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
    """List previous Audial results in the results folder, newest first."""
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
    """Show whether this computer is signed in to Audial, and as whom."""
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


@mcp.tool(title="Sign out of Audial", annotations=REMOTE)
async def sign_out() -> Account:
    """Sign out of Audial on this computer and disconnect it from the account."""
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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    global _settings, _startup_error
    try:
        _settings = load_settings()
        _startup_error = None
    except ConfigError as exc:
        _settings = fallback_settings()
        _startup_error = exc
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


if __name__ == "__main__":
    main()
