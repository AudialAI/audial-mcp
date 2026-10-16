"""Turn every failure into a ToolError the model can read and act on."""

from __future__ import annotations

import logging

from audial.api.exceptions import AudialAuthError, AudialError, SubscriptionRequiredError
from mcp.server.mcpserver.exceptions import ToolError

from audial_mcp.config import ConfigError
from audial_mcp.jobs import JobTimeout
from audial_mcp.validation import ValidationError

log = logging.getLogger("audial_mcp")


class SignInRequired(Exception):
    """The user has to approve a sign-in in their browser before the tool can run."""

    def __init__(self, url: str, user_code: str):
        super().__init__(
            f"Audial needs you to sign in first. Open {url} in your browser and approve it "
            f"(the page should show the code {user_code}), then ask again."
        )
        self.url = url
        self.user_code = user_code


def to_tool_error(
    exc: BaseException,
    *,
    tool: str,
    execution_id: str | None = None,
    api_key_from_env: bool = False,
) -> ToolError:
    if isinstance(exc, (ConfigError, ValidationError, SubscriptionRequiredError)):
        return ToolError(str(exc))
    if isinstance(exc, SignInRequired):
        return ToolError(str(exc))
    if isinstance(exc, AudialAuthError):
        if api_key_from_env:
            return ToolError(
                "Audial rejected the credentials. Check AUDIAL_API_KEY (and AUDIAL_USER_ID, if "
                "set) in the server's env and restart the client, or remove them to sign in "
                "through the browser instead."
            )
        return ToolError(
            f"Audial did not accept the sign-in on this computer ({exc}). It may have been "
            "disconnected at audialmusic.ai. Call the sign_in tool to sign in again."
        )
    if isinstance(exc, JobTimeout):
        ref = f" (execution {execution_id})" if execution_id else ""
        return ToolError(
            f"{exc.label} did not finish within {exc.elapsed_s:.0f} s{ref}. The job may still "
            "complete on Audial's side; try again, or use a shorter file."
        )
    if isinstance(exc, AudialError):
        return ToolError(str(exc))
    log.exception("%s failed", tool)
    return ToolError(f"{tool} failed unexpectedly. Details were logged to the server's stderr.")
