"""Environment-driven settings for the audial MCP server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audial_mcp import BOOT_ENV

DEFAULT_RESULTS_DIR = "~/Audial"
DEFAULT_JOB_TIMEOUT_S = 900

# Seconds a tool call waits for the user to approve a sign-in in their browser before it
# returns the link instead. An approval that arrives later is picked up by the next call.
SIGN_IN_WAIT_S = 120


class ConfigError(Exception):
    """Raised when the environment cannot be turned into usable settings."""


@dataclass(frozen=True)
class Settings:
    # Credentials from the client's config, for setups that pin an API key. Both are optional:
    # without a key the server uses the sign-in saved on this machine (`audial login`, or the
    # browser sign-in a tool call starts), and a key of the form aud_... needs no user id.
    user_id: str | None
    api_key: str | None
    results_dir: Path
    api_base_url: str | None
    job_timeout_s: int


def _always_parseable(env: Mapping[str, str]) -> dict[str, Any]:
    """The settings fields that cannot fail to parse: credentials, base URL, results dir."""
    results_dir = Path(
        os.path.expandvars(env.get("AUDIAL_RESULTS_DIR") or DEFAULT_RESULTS_DIR)
    ).expanduser()
    return {
        "user_id": (env.get("AUDIAL_USER_ID") or "").strip() or None,
        "api_key": (env.get("AUDIAL_API_KEY") or "").strip() or None,
        "results_dir": results_dir,
        "api_base_url": (env.get("AUDIAL_API_BASE_URL") or "").strip() or None,
    }


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read settings from `env`, defaulting to the environment as it was at import time.

    The default is `audial_mcp.BOOT_ENV`, not the live `os.environ`: the Audial SDK calls
    `dotenv.load_dotenv()` when it is imported, so a `.env` file in whatever directory the MCP
    client happened to launch the server from would otherwise be able to override the
    credentials and API host the client configured.
    """
    env = BOOT_ENV if env is None else env
    raw_timeout = env.get("AUDIAL_JOB_TIMEOUT_S", str(DEFAULT_JOB_TIMEOUT_S))
    try:
        timeout = int(raw_timeout)
        if timeout <= 0:
            raise ValueError
    except ValueError:
        raise ConfigError(
            "AUDIAL_JOB_TIMEOUT_S must be a positive integer number of seconds, "
            f"got {raw_timeout!r}"
        ) from None

    return Settings(**_always_parseable(env), job_timeout_s=timeout)


def fallback_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Settings to run with when `load_settings` raised: what parsed, defaults for what did not.

    The server keeps starting on a bad `AUDIAL_JOB_TIMEOUT_S` so that it can list its tools and
    return the configuration error as a readable message. A server that dies at import shows up
    in clients as an opaque "failed to start".
    """
    env = BOOT_ENV if env is None else env
    return Settings(**_always_parseable(env), job_timeout_s=DEFAULT_JOB_TIMEOUT_S)
