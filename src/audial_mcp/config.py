"""Environment-driven settings for the audial MCP server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
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

    results_dir = Path(
        os.path.expandvars(env.get("AUDIAL_RESULTS_DIR") or DEFAULT_RESULTS_DIR)
    ).expanduser()

    return Settings(
        user_id=(env.get("AUDIAL_USER_ID") or "").strip() or None,
        api_key=(env.get("AUDIAL_API_KEY") or "").strip() or None,
        results_dir=results_dir,
        api_base_url=(env.get("AUDIAL_API_BASE_URL") or "").strip() or None,
        job_timeout_s=timeout,
    )
