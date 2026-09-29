from pathlib import Path

import pytest

from audial_mcp.config import ConfigError, Settings, load_settings


def test_defaults_when_only_credentials_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = load_settings({"AUDIAL_USER_ID": "u1", "AUDIAL_API_KEY": "k1"})
    assert s.user_id == "u1" and s.api_key == "k1"
    assert s.results_dir == tmp_path / "Audial"
    assert s.api_base_url is None
    assert s.job_timeout_s == 900


def test_results_dir_expands_tilde_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = load_settings({"AUDIAL_RESULTS_DIR": "~/Music/AudialOut"})
    assert s.results_dir == tmp_path / "Music" / "AudialOut"


def test_timeout_parses_int_and_rejects_garbage():
    assert load_settings({"AUDIAL_JOB_TIMEOUT_S": "120"}).job_timeout_s == 120
    with pytest.raises(ConfigError, match="AUDIAL_JOB_TIMEOUT_S"):
        load_settings({"AUDIAL_JOB_TIMEOUT_S": "soon"})


def test_require_credentials_message_names_both_variables():
    s = load_settings({})
    with pytest.raises(ConfigError) as exc:
        s.require_credentials()
    assert "AUDIAL_USER_ID" in str(exc.value) and "AUDIAL_API_KEY" in str(exc.value)
    assert "audialmusic.ai" in str(exc.value)


def test_credentials_present_passes():
    Settings(
        user_id="u", api_key="k", results_dir=Path("/tmp"), api_base_url=None, job_timeout_s=1
    ).require_credentials()
