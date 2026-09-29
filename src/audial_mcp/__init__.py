"""audial-mcp: a local MCP server for the Audial audio tools."""

import os

__version__ = "0.1.1"

# The environment as the client handed it to us, snapshotted before anything else runs.
#
# `import audial` calls `dotenv.load_dotenv()`, which copies a `.env` in the *process's working
# directory* into `os.environ`. The working directory of an MCP server is the client's, not the
# user's project, and a `.env` that happens to sit there would silently redirect credentials and
# the API host. This package is imported before `audial_mcp.server` executes its `import audial`
# (importing a submodule initialises its package first), so this snapshot predates that call.
# `config.load_settings()` reads from here rather than from the live `os.environ`.
BOOT_ENV: dict[str, str] = dict(os.environ)
