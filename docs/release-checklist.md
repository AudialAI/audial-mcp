# Release checklist
1. Bump `version` in pyproject.toml and `__version__`; update `server.json`, `bundle/manifest.json` and `bundle/pyproject.toml` (a test checks they agree); update the
   Claude plugin's `.mcp.json` pin (`uvx audial-mcp==X.Y.Z`) and plugin.json version.
   If the `audial-sdk` requirement moved, run `uv lock` once that SDK version is on PyPI: until
   then `uv sync` (and CI) cannot resolve it.
2. `uv run pytest -q`, ruff clean, manual checklist "Prod" section on the previous version.
3. `git tag -a vX.Y.Z -m "audial-mcp X.Y.Z" && git push origin main vX.Y.Z` → publish workflow.
4. Verify `pip index versions audial-mcp` / PyPI page; `uvx audial-mcp==X.Y.Z </dev/null`
   exits 0 after printing its startup line to stderr (there is no `--help`; with stdin open
   the server blocks waiting for MCP traffic).
5. `mcp-publisher login github --token "$(gh auth token)"` (an org owner token with read:org; the device flow only grants the personal namespace) then `mcp-publisher publish`, with `server.json` version bumped and its description ≤ 100 characters.
6. Push the plugin repo; the Claude directory picks up the tracked branch automatically.
7. Update the website MCP tab if tools or config changed.
8. Redeploy the hosted server (`.do/app-mcp.yaml`, app `audial-mcp-prod`) from `main`: build and
   push `zfarrell13/audial-mcp-remote:<full sha>` as the spec's header says, then pin the app's
   `image.tag` to that sha. Then check that `https://mcp.audialmusic.ai/healthz` reports the new
   version.
   - A deploy restarts the server: running jobs are forgotten, and every download link it
     served expires. Deploy at a quiet hour.
   - A change to a tool's name, description or hints is a material change for both
     directories: OpenAI's daily scan sees it, and Anthropic must be told (policy 3H).
