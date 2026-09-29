# Release checklist
1. Bump `version` in pyproject.toml and `__version__`; update `server.json` version; update the
   Claude plugin's `.mcp.json` pin (`uvx audial-mcp==X.Y.Z`) and plugin.json version.
2. `uv run pytest -q`, ruff clean, manual checklist "Prod" section on the previous version.
3. `git tag -a vX.Y.Z -m "audial-mcp X.Y.Z" && git push origin main vX.Y.Z` → publish workflow.
4. Verify `pip index versions audial-mcp` / PyPI page; `uvx audial-mcp==X.Y.Z </dev/null`
   exits 0 after printing its startup line to stderr (there is no `--help`; with stdin open
   the server blocks waiting for MCP traffic).
5. `mcp-publisher publish` (Task 10) so the registry shows the new version.
6. Push the plugin repo; the Claude directory picks up the tracked branch automatically.
7. Update the website MCP tab if tools or config changed.
