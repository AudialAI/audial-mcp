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

CREDENTIALS_HELP = (
    "AUDIAL_USER_ID and AUDIAL_API_KEY are not set. Add both to the `audial` server's env in "
    "your MCP client config (for Claude Code: `claude mcp add audial -e AUDIAL_USER_ID=... "
    "-e AUDIAL_API_KEY=... -- uvx audial-mcp`). Get them from your dashboard at "
    "https://audialmusic.ai, then restart the client."
)


class ConfigError(Exception):
    """Raised when the environment cannot be turned into usable settings."""


@dataclass(frozen=True)
class Settings:
    user_id: str | None
    api_key: str | None
    results_dir: Path
    api_base_url: str | None
    job_timeout_s: int

    def require_credentials(self) -> None:
        if not self.user_id or not self.api_key:
            raise ConfigError(CREDENTIALS_HELP)


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
