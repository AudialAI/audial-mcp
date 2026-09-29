"""Path checks that turn bad input into messages the model can act on."""

from __future__ import annotations

import os
from pathlib import Path

AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".aif", ".aiff", ".flac", ".m4a", ".ogg", ".aac"})
MIDI_EXTENSIONS = frozenset({".mid", ".midi"})
JSON_EXTENSIONS = frozenset({".json"})


class ValidationError(Exception):
    """Input the user gave cannot be used; the message says why."""


def check_file(path: str, kinds: frozenset[str], label: str) -> Path:
    candidate = Path(os.path.expandvars(path)).expanduser()
    if not candidate.exists():
        raise ValidationError(f"{candidate} does not exist. Give the full path to the {label}.")
    if candidate.is_dir():
        raise ValidationError(f"{candidate} is a directory; give the path to a single {label}.")
    if candidate.suffix.lower() not in kinds:
        allowed = ", ".join(sorted(kinds))
        raise ValidationError(f"{candidate.name} is not a supported {label} (expected {allowed}).")
    return candidate.resolve()
