"""Path checks that turn bad input into messages the model can act on."""

from __future__ import annotations

from pathlib import Path

AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".aif", ".aiff", ".flac", ".m4a", ".ogg", ".aac"})
MIDI_EXTENSIONS = frozenset({".mid", ".midi"})
JSON_EXTENSIONS = frozenset({".json"})


class ValidationError(Exception):
    """Input the user gave cannot be used; the message says why."""


def check_file(path: str, kinds: frozenset[str], label: str) -> Path:
    """Resolve and check a path the model supplied.

    `~` is expanded; `$VAR` deliberately is not. These paths come from the model and the
    messages built from them go back to the model, so expanding environment variables would
    let `check_file("$AUDIAL_API_KEY.wav", ...)` read the API key out of the process
    environment and echo it into the error text.
    """
    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise ValidationError(f"{candidate} does not exist. Give the full path to the {label}.")
    if candidate.is_dir():
        raise ValidationError(f"{candidate} is a directory; give the path to a single {label}.")
    if candidate.suffix.lower() not in kinds:
        allowed = ", ".join(sorted(kinds))
        raise ValidationError(f"{candidate.name} is not a supported {label} (expected {allowed}).")
    return candidate.resolve()
