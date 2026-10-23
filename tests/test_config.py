import os

import pytest

from audial_mcp import BOOT_ENV
from audial_mcp.config import ConfigError, fallback_settings, load_settings


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


def test_credentials_are_optional():
    """No key in the client config is a valid setup: the server signs in through the browser."""
    s = load_settings({})
    assert s.user_id is None and s.api_key is None


def test_settings_ignore_the_live_environment_after_boot(monkeypatch):
    """`load_settings()` reads the boot snapshot, not whatever `os.environ` holds now.

    The mechanism this guards: `import audial` calls `dotenv.load_dotenv()`, which copies a
    `.env` from the process's working directory (the MCP *client's* directory, not the user's
    project) into `os.environ`. Anything written there after `audial_mcp` was imported --
    a `.env`, or any other later mutation -- must not be able to redirect the credentials or
    the API host the client configured. Setting the variable with monkeypatch reproduces
    exactly what `load_dotenv()` does: an `os.environ` write after the snapshot was taken.
    """
    monkeypatch.setitem(BOOT_ENV, "AUDIAL_USER_ID", "from-client-config")
    monkeypatch.setenv("AUDIAL_USER_ID", "from-dotenv")
    monkeypatch.setenv("AUDIAL_API_KEY", "from-dotenv")
    monkeypatch.setenv("AUDIAL_API_BASE_URL", "https://evil.example/api")

    s = load_settings()
    assert s.user_id == "from-client-config"
    assert s.api_key != "from-dotenv"
    assert s.api_base_url != "https://evil.example/api"


def test_a_dotenv_in_the_working_directory_cannot_override_settings(tmp_path, monkeypatch):
    """The same guard, driven through `dotenv.load_dotenv()` itself.

    `audial/utils/config.py` calls `dotenv.load_dotenv()` at import; this reproduces the
    resulting `os.environ` writes and asserts `load_settings()` is unmoved by them.
    """
    dotenv = pytest.importorskip("dotenv")
    monkeypatch.setitem(BOOT_ENV, "AUDIAL_USER_ID", "from-client-config")
    monkeypatch.setitem(BOOT_ENV, "AUDIAL_API_KEY", "from-client-config")
    # setenv first so monkeypatch records (and restores) the pre-test state of both keys.
    monkeypatch.setenv("AUDIAL_USER_ID", "")
    monkeypatch.setenv("AUDIAL_API_KEY", "")
    monkeypatch.delenv("AUDIAL_USER_ID")
    monkeypatch.delenv("AUDIAL_API_KEY")
    (tmp_path / ".env").write_text("AUDIAL_USER_ID=from-dotenv\nAUDIAL_API_KEY=from-dotenv\n")
    monkeypatch.chdir(tmp_path)

    dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True), override=True)
    assert os.environ["AUDIAL_USER_ID"] == "from-dotenv"

    s = load_settings()
    assert s.user_id == "from-client-config" and s.api_key == "from-client-config"


def test_fallback_settings_keep_the_credentials_that_parsed():
    env = {"AUDIAL_USER_ID": "u1", "AUDIAL_API_KEY": "k1", "AUDIAL_JOB_TIMEOUT_S": "soon"}
    with pytest.raises(ConfigError):
        load_settings(env)
    s = fallback_settings(env)
    assert s.user_id == "u1" and s.api_key == "k1"
    assert s.job_timeout_s == 900
