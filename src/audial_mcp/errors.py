"""Turn every failure into a ToolError the model can read and act on."""

from __future__ import annotations

import logging

from audial.api.exceptions import AudialAuthError, AudialError, SubscriptionRequiredError
from mcp.server.mcpserver.exceptions import ToolError

from audial_mcp.config import ConfigError
from audial_mcp.jobs import JobTimeout
from audial_mcp.validation import ValidationError

log = logging.getLogger("audial_mcp")


def to_tool_error(exc: BaseException, *, tool: str, execution_id: str | None = None) -> ToolError:
    if isinstance(exc, (ConfigError, ValidationError, SubscriptionRequiredError)):
        return ToolError(str(exc))
    if isinstance(exc, AudialAuthError):
        return ToolError(
            "Audial rejected the credentials. Check AUDIAL_USER_ID and AUDIAL_API_KEY in the "
            "server's env and restart the client."
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
