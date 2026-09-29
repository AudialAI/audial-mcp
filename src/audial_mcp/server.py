"""audial MCP server: tools that wrap the Audial SDK."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import audial
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from audial_mcp import __version__
from audial_mcp.config import Settings, load_settings
from audial_mcp.errors import to_tool_error
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
    "active Audial subscription; relay the error text if one is returned."
)

mcp = MCPServer("audial", instructions=INSTRUCTIONS, version=__version__)

_settings: Settings = load_settings()

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


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items() if str(k).lower() not in SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


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
    """A job that failed before writing anything leaves no empty folder behind."""
    with contextlib.suppress(OSError):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


def _apply_credentials() -> None:
    """Put the configured credentials where the SDK reads them. Never logged."""
    _settings.require_credentials()
    if _settings.api_base_url:
        os.environ["AUDIAL_API_BASE_URL"] = _settings.api_base_url
    else:
        os.environ.pop("AUDIAL_API_BASE_URL", None)
    os.environ["AUDIAL_USER_ID"] = _settings.user_id or ""
    os.environ["AUDIAL_API_KEY"] = _settings.api_key or ""


async def _execute(ctx: Context, tool: str, plan: Plan) -> JobResult:
    """Common path: validate → credentials → job folder → run in thread → inventory → JobResult."""
    execution_id: str | None = None
    output_dir: Path | None = None
    try:
        slug, call = plan()
        _apply_credentials()
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
        raise to_tool_error(exc, tool=tool, execution_id=execution_id) from None


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
    """Turn a one-shot sample into an editable Audial Synth preset.

    Requires an Audial subscription.
    """

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
    """Synthesize a sung vocal (and MIDI) from lyrics, a melody and a reference voice.

    Requires an Audial subscription.
    """

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
    try:
        return list_jobs(_settings.results_dir, _tool_filter(tool) if tool else None, limit)
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        raise to_tool_error(exc, tool="list_results") from None


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    global _settings
    _settings = load_settings()
    log.info(
        "audial-mcp %s; results dir %s; credentials %s",
        __version__,
        _settings.results_dir,
        "present" if _settings.user_id and _settings.api_key else "MISSING",
    )
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
