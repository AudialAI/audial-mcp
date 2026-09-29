import pytest

from audial_mcp.validation import AUDIO_EXTENSIONS, MIDI_EXTENSIONS, ValidationError, check_file


def test_accepts_audio_and_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "demo.WAV"
    f.write_bytes(b"0")
    assert check_file("~/demo.WAV", AUDIO_EXTENSIONS, "audio file") == f.resolve()


def test_missing_file_message(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        check_file(str(tmp_path / "missing.wav"), AUDIO_EXTENSIONS, "audio file")


def test_directory_rejected(tmp_path):
    with pytest.raises(ValidationError, match="is a directory"):
        check_file(str(tmp_path), AUDIO_EXTENSIONS, "audio file")


def test_wrong_extension_lists_allowed(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    with pytest.raises(ValidationError) as exc:
        check_file(str(f), MIDI_EXTENSIONS, "MIDI file")
    assert ".mid" in str(exc.value) and "notes.txt" in str(exc.value)
