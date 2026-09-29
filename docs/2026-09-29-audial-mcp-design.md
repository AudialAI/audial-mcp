# audial-mcp — Design

**Status:** approved direction from Zach (local server, one central results folder); open questions listed at the end.
**Date:** 2026-09-29
**Companion:** `docs/2026-09-29-audial-mcp-implementation-plan.md`

## 1. What it is

`audial-mcp` is a local Model Context Protocol (MCP) server that exposes the nine Audial SDK
functions as tools, so any MCP client (Claude Code, Claude Desktop, Cursor, Cowork, Windsurf,
custom agents) can run Audial's hosted audio tools from a conversation:

> "Split ~/Music/demo.wav into vocals and drums and slow it to 90 bpm."

The server runs on the user's machine over stdio, is started by the client on demand, and
talks to the Audial API through `audial-sdk`. It is a thin layer: schema, validation, job
supervision, result inventory. All audio processing stays in the hosted engines.

## 2. Non-goals (v0.1)

- No hosted/remote transport. One codebase could add streamable HTTP later; auth and file
  transfer are different products and are out of scope now.
- No new Audial capabilities. Tools map 1:1 onto SDK functions plus one local helper
  (`list_results`).
- No account creation or key management inside the server. Users bring a user id and an
  API key from the Audial dashboard, exactly like SDK users.
- No MCP resources or prompts in v0.1 (tools only). Candidates for later: results as
  resources, a "how to prompt the music model" prompt.

## 3. User experience

**Discover.** Website API reference gets an "MCP" tab (install blocks + tool table); the
package is on PyPI; the server is listed in the official MCP registry; a Claude plugin bundle
is submitted to the Claude directory; Cursor users get an "Add to Cursor" button.

**Install.** One config block per client. Canonical form:

```bash
claude mcp add audial \
  -e AUDIAL_USER_ID=<user id> -e AUDIAL_API_KEY=<api key> -e AUDIAL_RESULTS_DIR=~/Audial \
  -- uvx audial-mcp
```

`uvx` fetches the package into an isolated environment; nothing else to install beyond `uv`.
Claude Desktop and Cursor take the equivalent JSON (`command: "uvx", args: ["audial-mcp"]`).
The client launches the server when it starts and kills it on exit; nothing runs otherwise.

**Use.** The model calls a tool, the client asks the user to approve it the first time, the
server validates the file, runs the SDK call, streams progress notifications while the job
runs, and returns the output folder, the files, and metadata. The model summarises and the
user opens the files in their DAW.

## 4. Architecture

```
MCP client ──stdio──▶ audial-mcp (Python, mcp>=2.2 MCPServer)
                        ├── config.py   env → Settings (ids, results dir, timeouts)
                        ├── server.py   MCPServer + 10 tools + error mapping
                        ├── jobs.py     run a blocking SDK call in a worker thread,
                        │               heartbeat progress, hard timeout
                        ├── results.py  job folder naming, inventory, list_results
                        └── audial-sdk  upload → run → poll → download (unchanged)
                                 └── Audial API (api.audialmusic.ai) → hosted GPU workers
```

- **Runtime:** Python ≥ 3.10 (mcp requires it), `mcp>=2.2,<3`, `audial-sdk>=1.2.3`.
- **Server object:** `mcp.server.MCPServer("audial", instructions=..., version=...)`.
  Tools are `async def` decorated with `@mcp.tool(title=..., annotations=ToolAnnotations(...))`;
  the JSON schema comes from type hints and `Annotated[..., Field(description=...)]`.
- **Blocking SDK calls** run via `anyio.to_thread.run_sync`, so the event loop can send
  `ctx.report_progress(...)` heartbeats every 5 s and the client sees the job is alive.
- **stdout safety:** mcp 2.x's stdio transport diverts fd 1 to stderr while serving, so the
  SDK's `print()` progress lines cannot corrupt the protocol stream. A regression test
  asserts a tool that prints still returns a valid result.

## 5. Configuration

All via environment variables (set in the client's config block):

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `AUDIAL_USER_ID` | yes | – | Audial user id |
| `AUDIAL_API_KEY` | yes | – | Audial API key (the SDK reads the same variables) |
| `AUDIAL_RESULTS_DIR` | no | `~/Audial` | Central folder every result lands in (Zach's decision) |
| `AUDIAL_API_BASE_URL` | no | prod | Passed through to the SDK (dev/staging testing) |
| `AUDIAL_JOB_TIMEOUT_S` | no | `900` | Hard cap per tool call |

Missing credentials do **not** stop the server from starting: tools are listed, and any call
returns a `ToolError` telling the user exactly which variable to set and where to get it.
A server that dies at startup shows up in clients as an opaque "failed to start"; a server
that explains itself is fixable from the conversation.

## 6. Tools

Names mirror the SDK so the API docs, SDK docs and MCP docs agree. Every tool except `list_results` calls the Audial API, which requires an active subscription (2026-09-29: the API gates every function).

| Tool | Wraps | Inputs (beyond `file_path`) | Notes |
|---|---|---|---|
| `stem_split` | `audial.stem_split` | `stems`, `target_bpm`, `target_key`, `algorithm` | stems ⊆ {vocals, drums, bass, other, full_song_without_vocals} |
| `analyze` | `audial.analyze` | – | read-only; returns analysis dict as metadata |
| `segment` | `audial.segment` | `components`, `analysis_type`, `features`, `genre` | |
| `master` | `audial.master` | `reference_file` | |
| `generate_samples` | `audial.generate_samples` | `job_type`, `components`, `genre` | |
| `generate_midi` | `audial.generate_midi` | `bpm` | one file per call in v0.1 |
| `generate_music` | `audial.generate_music` | `prompt`, `task_type`, `lyrics`, `source_file`, `reference_file`, `bpm`, `key_scale`, `time_signature`, `audio_duration`, `vocal_language`, `audio_cover_strength`, `repainting_start/end`, `batch_size`, `seed`, `audio_format`, `track_name`, `instrumental`, `negative_prompt` | no `file_path`; `understand` mode returns caption in metadata |
| `sound2vital` | `audial.sound2vital` | – | returns a `.vital` preset |
| `text2vox` | `audial.text2vox` | `reference_file`, `lyrics`, `midi_file`, `melody_audio_file`, `word_timestamps_file`, `lyrics_mode`, `reference_text`, `cfg_strength`, `nfe_steps`, `pitch_shift`, `strict_pitch`, `bend_smoothing_ms`, `no_pitch_bends`, `leading_silence_s`, `seed` | |
| `list_results` | local | `tool` (optional), `limit` | read-only; newest first |

Every processing tool returns the same structured object (also serialised as text for the
model):

```json
{
  "tool": "stem_split",
  "execution_id": "-P2h…",
  "output_dir": "/Users/zach/Audial/stem_split/2026-09-29_104512_demo",
  "files": [{"name": "vocals.wav", "path": "…/vocals.wav", "size_bytes": 5242880}],
  "metadata": {"bpm": 96, "key_scale": "D major"},
  "summary": "stem_split finished in 84 s: 2 files in ~/Audial/stem_split/2026-09-29_104512_demo"
}
```

`files` is built by walking `output_dir` after the SDK returns, so it is independent of each
SDK function's return shape. `execution_id` is pulled from `execution.exeId` /
`execution_id` when present. `metadata` is the SDK return minus file lists.

Parameter descriptions carry the domain knowledge the model needs (valid stems, what
`target_key` accepts, that `bpm` on `generate_music` is a hint not a constraint, that
`sound2vital` needs a one-shot ≤ 20 s), because the model reads the schema, not our README.

## 7. Results folder

`AUDIAL_RESULTS_DIR/<tool>/<YYYY-MM-DD_HHMMSS>_<slug>/`

- `<slug>` = source file stem for file tools (sanitised to `[A-Za-z0-9_-]`, ≤ 40 chars), or
  the first three words of the prompt for `generate_music`, or `job` as a fallback.
- The folder is created before the SDK call and passed as `results_folder`; SDK functions
  that add their own subfolder (music generation adds `<exe_id>_generated`) still land
  inside it.
- `list_results` scans the tree, newest first, optionally filtered by tool, and returns the
  same `files` inventory per job so the model can find earlier outputs without the user
  remembering paths.

## 8. Long-running jobs

Typical durations: analyze ~10 s, stems 1–3 min, music generation 1–3 min (longer on a cold
worker), sound2vital ~1.5 min, text2vox 1–2 min.

- Each tool call: validate → create job folder → `run_job(ctx, label, fn, timeout)`.
- `run_job` starts the SDK call in a thread and a heartbeat task that calls
  `ctx.report_progress(elapsed_s, message="stem_split: 45 s elapsed")` every 5 s until the
  thread finishes. Progress notifications keep clients that reset timeouts on progress
  (Claude Code, Inspector) alive and give the user a live counter.
- Hard timeout `AUDIAL_JOB_TIMEOUT_S` (900 s) via `anyio.fail_after`; on expiry the tool
  returns a `ToolError` with the execution id when known (the hosted job keeps running;
  `list_results` will not see it, so the message says to retry later or check the
  dashboard). Python threads cannot be killed; the orphaned thread finishes quietly.
  That thread is non-daemon, so after a timeout the process exits only once the abandoned
  SDK call returns or the client terminates the server.
- **SDK dependency:** `generate_music` and `generate_midi` currently hard-code their own
  5-minute waits, which is shorter than a cold-start generation. SDK 1.2.2 adds
  `max_wait` parameters (default 900) so the server's timeout is the only one that matters.
  This is Task 0 of the plan.

## 9. Errors

Raised as `mcp.server.mcpserver.exceptions.ToolError` so the **model** sees the message and
can act on it (anything else surfaces as an opaque "Error executing tool"):

| Situation | Message shape |
|---|---|
| Missing credentials | "AUDIAL_USER_ID / AUDIAL_API_KEY are not set. Add them to the audial server's env in your MCP client config; get them at audialmusic.ai → dashboard." |
| File missing / not a file / wrong extension | "…/demo.txt is not an audio file (expected .wav, .mp3, .aif, .aiff, .flac, .m4a, .ogg)." |
| `SubscriptionRequiredError` | The API's own text ("This feature needs an active Audial subscription…") |
| `AudialAuthError` | "Audial rejected the credentials. Check AUDIAL_USER_ID and AUDIAL_API_KEY." |
| Other `AudialError` | The SDK message verbatim |
| Timeout | "stem_split did not finish within 900 s (execution -P2h…). The job may still complete on Audial's side; try again or lower the file length." |
| Unexpected exception | Generic message + traceback to stderr (never to the model) |

## 10. Security and privacy

- The server reads only files the user names in the conversation and writes only under
  `AUDIAL_RESULTS_DIR`. There is no directory allow-list in v0.1: it runs as the user, on
  the user's machine, with the user's files. (Open question 4 asks whether to add one.)
- Credentials come from the environment and are never included in tool results, logs at
  INFO, or error messages.
- Uploaded audio goes to Audial's API over HTTPS exactly as the SDK does; the README states
  what leaves the machine (required by the Claude directory security scan).
- Tool annotations: `analyze` and `list_results` are `read_only_hint=True`; everything else
  `open_world_hint=True` (calls a remote service) and not idempotent.

## 11. Packaging and distribution surfaces

| Surface | Mechanism | Owner |
|---|---|---|
| PyPI `audial-mcp` | `pyproject.toml` (hatchling), console script `audial-mcp`, GitHub Actions publish on tag `v*` using the existing PyPI token as a repo secret | me |
| GitHub `AudialAI/audial-mcp` | public, MIT (matches the SDK), README is the landing page | me (creation needs Zach's OK: outward-facing) |
| Official MCP registry | README marker `mcp-name: io.github.AudialAI/audial-mcp`, `server.json` (pypi package, `runtimeHint: uvx`, stdio, env var declarations), `mcp-publisher login github` + `publish` | Zach runs the interactive GitHub login; I prepare everything |
| Claude directory | Plugin bundle repo `AudialAI/audial-claude-plugin`: `.claude-plugin/plugin.json`, `.mcp.json` (`uvx audial-mcp==0.1.0`, pinned as the checklist requires), `userConfig` for user id (string), API key (sensitive), results dir (directory, default `~/Audial`), `skills/audial/SKILL.md`, README ≥ 40 words, LICENSE, `marketplace.json` so `claude plugin marketplace add` works before approval. Submit at claude.ai/directory/manage (paid plan; Zach). Note: chat surfaces ignore local servers; the listing serves Claude Code and Cowork users | me (repo), Zach (submission) |
| Claude Desktop | `claude_desktop_config.json` snippet on the website. MCPB one-click bundle deferred (open question 5) | me |
| Cursor | "Add to Cursor" deeplink button on the website (`cursor://anysphere.cursor-deeplink/mcp/install?name=audial&config=<base64 JSON>`) with placeholder env values the user fills in; directory listing is a manual submission | me (button), Zach (listing) |
| Website | New "MCP" tab on `/resources/api-reference`: what it is, per-client install blocks, tool table, results folder, troubleshooting, links | me |
| Community catalogs (Smithery, Glama, PulseMCP, mcp.so) | Optional manual submissions after the registry listing | Zach, optional |

## 12. Testing

- **Unit (pytest + anyio):** config parsing, slug/folder naming, file validation,
  `run_job` heartbeat and timeout, error mapping.
- **Server (in-memory):** `mcp.Client(mcp_server, raise_exceptions=True)` lists tools,
  calls each tool with the SDK function monkeypatched (no network), asserts the SDK was
  called with the right kwargs and `results_folder`, and that the structured result has
  the inventory. One test lets the fake SDK `print()` and asserts the protocol survives.
- **Manual e2e (documented checklist):** `mcp dev` Inspector against the dev API with the
  smoke account; then Claude Code against prod with a subscribed test account: stem split,
  analyze, generate_music, sound2vital, text2vox, and a 402 with the unsubscribed account.
- **CI:** tests on every push/PR (3.10 and 3.12), publish on tag.

## 13. Versioning

`0.1.0` first release. Minor bumps for new tools or schema changes, patch for fixes. The
plugin's `.mcp.json` pin and the registry `server.json` version are bumped by the release
checklist (Task 13).

## 14. Open questions for Zach

1. **Registry namespace.** `io.github.AudialAI/audial-mcp` (GitHub login, no DNS work) or
   `ai.audialmusic/audial-mcp` (needs a DNS TXT record on audialmusic.ai, reads as a brand)?
   Plan assumes the GitHub namespace.
2. **Plugin repo name and skill scope.** Plan assumes `AudialAI/audial-claude-plugin` with
   one skill that teaches when to use each tool. Do you also want the SDK/API docs as a
   skill so Claude Code can write Audial integrations?
3. **Cursor directory.** Do you want me to prepare the submission text, or skip the listing
   and rely on the website button?
4. **File access scope.** Keep "any readable file" (plan default) or add an allow-list
   (`AUDIAL_ALLOWED_DIRS`)?
5. **Claude Desktop one-click bundle (MCPB).** Defer (plan default) or include in 0.1?
   It needs a bundled Python runtime story and more testing on Windows.
6. **Windows.** `uvx` works on Windows; I will test macOS only unless you have a Windows
   machine to try. OK to ship "macOS tested, Windows expected to work"?
7. **PyPI publishing.** Reuse the existing PyPI token as a GitHub secret (plan default) or
   set up trusted publishing (you'd add the publisher on pypi.org once)?
