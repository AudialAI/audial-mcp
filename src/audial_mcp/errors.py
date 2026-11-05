"""Turn every failure into a ToolError the model can read and act on."""

from __future__ import annotations

import logging

from audial.api.exceptions import (
    AudialAuthError,
    AudialError,
    AudialValidationError,
    SubscriptionRequiredError,
)
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


# What the API's add-on refusals mean, in the words the website uses for them.
ADDON_REFUSALS = {
    "STEMS_NOT_READY": "This track is still being prepared. Try again in a minute.",
    "ADDON_UNAVAILABLE": "This result can't be added to this analysis.",
}
STEMS_FAILED = "We couldn't prepare this track."


def _refusal(exc: BaseException) -> tuple[str | None, str | None]:
    """The add-on refusal code (and the stems' state) an API error carries, if it is one."""
    body: dict = {}
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001 — no JSON body is simply no code
            parsed = None
        if isinstance(parsed, dict):
            body = parsed
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        code = body.get("code")
    if not isinstance(code, str):
        code = str(exc).strip()
    if code not in ADDON_REFUSALS:
        return None, None
    state = body.get("state")
    return code, state if isinstance(state, str) else None


def _addon_error(exc: AudialError) -> str:
    """The error's own text, or the website's words for a refusal that came with only a code.

    The SDK words its refusals itself ("This track is still being prepared. Try again in a
    minute. (STEMS_NOT_READY)"); those pass through unchanged.
    """
    text = str(exc).strip()
    code, state = _refusal(exc)
    if code is None or (text and text != code):
        return text
    if code == "STEMS_NOT_READY" and state in ("failed", "cancelled"):
        return STEMS_FAILED
    return ADDON_REFUSALS[code]


def _reuse(tool: str, analysis_id: str) -> str:
    return (
        f" The track was analysed (analysis_id {analysis_id}): call {tool} again with "
        f"analysis_id {analysis_id} to continue without a new analysis or another run."
    )


def to_tool_error(
    exc: BaseException,
    *,
    tool: str,
    execution_id: str | None = None,
    api_key_from_env: bool = False,
    analysis_id: str | None = None,
) -> ToolError:
    """`analysis_id` is set when the tool analysed a file before the step that failed."""
    if isinstance(exc, AudialValidationError):
        # Input Audial will not accept, with the place it went wrong: per-note
        # lyrics carry the 1-based note the message is about, and the model
        # needs that number to fix the string it wrote (or to tell the user
        # which note to edit). Checked before SubscriptionRequiredError and
        # AudialError below, both of which would swallow the position.
        position = getattr(exc, "position", None)
        where = f" (position {position})" if isinstance(position, int) else ""
        return ToolError(f"{exc}{where}")
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
        if analysis_id:
            return ToolError(
                f"{exc.label} did not finish within {exc.elapsed_s:.0f} s. It keeps running on "
                f"Audial's side.{_reuse(tool, analysis_id)}"
            )
        ref = f" (execution {execution_id})" if execution_id else ""
        return ToolError(
            f"{exc.label} did not finish within {exc.elapsed_s:.0f} s{ref}. The job may still "
            "complete on Audial's side; try again, or use a shorter file."
        )
    if isinstance(exc, AudialError):
        message = _addon_error(exc)
        return ToolError(message + (_reuse(tool, analysis_id) if analysis_id else ""))
    log.exception("%s failed", tool)
    return ToolError(f"{tool} failed unexpectedly. Details were logged to the server's stderr.")
