# Manual e2e checklist — audial-mcp

Date: 2026-09-29. Environment: the dev API (`AUDIAL_API_BASE_URL` pointed at it), the dev
smoke account credentials,
`AUDIAL_RESULTS_DIR=/tmp/audial-e2e`, `AUDIAL_JOB_TIMEOUT_S=1500`.

**Method note (controller ruling on this task):** the brief's "Inspector" section assumes a
browser UI, which isn't available here. Instead the server was driven in-process with the MCP
Python SDK's `Client` against the real `MCPServer` object (`audial_mcp.server.mcp`) — the same
object the unit tests use, but *without* monkeypatching the SDK, so every call below is a real
upload + a real Audial job. One script per check under
`.superpowers/sdd/2026-09-29-audial-mcp-implementation-plan/` scratch scripts (not committed;
deleted after the run, see "Cleanup" at the bottom).

## Results — dev API

- [x] **1. `list_tools`** — exactly ten tools, every parameter documented.
  Tools: `analyze, generate_midi, generate_music, generate_samples, list_results, master,
  segment, sound2vital, stem_split, text2vox`. Missing-description scan over every tool's
  `input_schema.properties`: **0 missing**.

- [ ] **2. `analyze`** on `song30s.wav` — **FAILED (remote, dev environment).** Three attempts
  (two through the MCP tool, one direct SDK call for diagnosis), all ~6.5–9.8 s, all failing at
  the same point with `Audio analysis failed: API error: 500`. Root-caused with a diagnostic
  script that calls the SDK's `AudialProxy` directly (bypassing `audial-mcp` entirely): the
  upload succeeds, `create_execution` succeeds (execution ids `-P2hpB7GckSe2RMsf-ue` on
  `song30s.wav` and `-P2hpEMgNcJQmOFgqpqw` on `voice.wav`), and the POST to
  `/functions/run/primary-analysis` returns **HTTP 500 with body `Request failed with status
  code 409`**. Tracing that into the API's primary-analysis route, the 409 originates from
  the backend's call to Audial's hosted GPU workers for primary analysis — a cold-start/worker
  conflict on the dev deployment, not something `audial-mcp` or the SDK sent wrong (same error
  reproduces calling the proxy directly with no `audial-mcp` code in the path at all). Retried
  a fourth time ~5 minutes later (11:47:55); same 500. **No `audial-mcp` fix applies here** —
  nothing to change in this repo; this is a dev API-side outage. Recorded, not fixed, per task
  instructions.

- [ ] **3. `stem_split`** on `song30s.wav`, `stems=["vocals","drums"]` — **FAILED (remote, same
  root cause as #2).** `stem_split` also runs `primary-analysis` first (see
  `audial-sdk/audial/functions/stem_split.py`), so it hits the identical dev-side 500. 6.7 s
  elapsed; 2 progress notifications received (`0s: starting...` and `5s elapsed`) before the
  error. Never reached the actual stem-splitting stage.

- [x] **4. `generate_music`** text2music — **PASSED.** `prompt="upbeat country, acoustic
  guitar, female vocal"`, short `[Verse]` lyrics, `audio_duration=30`, `seed=7`. 118.8 s,
  execution id `-P2hpd-1LtuDAxM7zc9g`, 25 progress notifications (5 s heartbeat). Produced 1
  file: `-P2hpd-1LtuDAxM7zc9g_0.mp3` (1,201,965 bytes).
  `metadata.execution.generation_metadata.lm_model == "audial-music-1"` — confirmed.
  Redaction check: regex-scanned the full JSON dump of the returned `metadata` for `userid`
  (case-insensitive) — **zero matches**; the only "userId" text in the run's output was in my
  own harness script's print label, not in the tool's structured content.

- [x] **5. `sound2vital`** on `oneshot.wav` — **PASSED.** 104.8 s, execution id
  `-P2hpdM-CErNCfFTpzEb`. 6 files in the job folder including
  `-P2hpdM-CErNCfFTpzEb_sound2vital/preset.vital` (57,134 bytes) plus `render.wav`,
  `report.json`, `scores.json`, and two `keyboard_*.wav` previews.

- [ ] **6. `text2vox`** with `reference_file=voice.wav`,
  `lyrics="down by the river where the water runs slow"`, `midi_file=melody.mid` — **FAILED
  twice (remote, dev GPU resource exhaustion).** Attempt 1: execution `-P2hpdaNWIoqDHlG3wE4`,
  40.4 s. Attempt 2 (retry ~1 min later): execution `-P2hprIE4e1WGLsIRJZ4`, 41.8 s. Both fail
  identically inside the Audial text2vox worker's render step with
  `torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 3.65 GiB. GPU 0 has a total
  capacity of 23.52 GiB of which 2.98 GiB is free ... this process has 20.03 GiB memory in
  use.` — the dev text2vox GPU already had ~20 GB allocated before either job's inference
  started, so this is a dev-worker capacity/leak issue, not an `audial-mcp` defect (the tool
  correctly surfaced the SDK's `Render error: ... (exit 1): ...` traceback as a readable
  `ToolError`). Not retried a third time per the "don't spend forever on remote flakiness"
  guidance; recorded with both execution ids.

- [x] **7. `list_results`** — **PASSED.** `list_results()` (no filter, limit 20) returned the 2
  completed jobs (`generate_music`, `sound2vital`) newest-first by job-folder timestamp.
  `list_results(tool="analyze")` returned `[]` — correct, since every `analyze` attempt failed
  before producing files and `audial-mcp` discards the empty job folder it created
  (`_discard_if_empty`), so there was nothing to list. `list_results(tool="..")` was refused:
  `is_error=True`, message `"'..' is not a tool name. Give one of the Audial tool names
  (letters, digits, '_' and '-' only), or omit it to list every tool."`, and
  `structured_content` was `None` (no directory-escape or content leak).

- [x] **8. Error paths** — **PASSED**, all three:
  - Missing file (`analyze` on a nonexistent path): readable `ValidationError` —
    `"<path> does not exist. Give the full path to the audio file."`
  - Credentials unset (`Settings(user_id=None, api_key=None, ...)`): readable `ConfigError`
    naming both variables — `"AUDIAL_USER_ID and AUDIAL_API_KEY are not set. Add both to the
    audial server's env in your MCP client config (for Claude Code: claude mcp add audial -e
    AUDIAL_USER_ID=... -e AUDIAL_API_KEY=... -- uvx audial-mcp). Get them from your dashboard at
    https://audialmusic.ai, then restart the client."`
  - PROD unsubscribed account (`AUDIAL_API_BASE_URL=https://api.audialmusic.ai/api`, a prod
    test account without a subscription), calling `sound2vital` on `oneshot.wav`:
    failed fast in **1.6 s** (upload happened, then the API refused before any GPU job started)
    with the exact text:
    `"This feature needs an active Audial subscription. Subscribe at audialmusic.ai and try
    again."`
    No job folder was left behind (empty folder auto-discarded).

## Claude Code

- [x] **9a. `claude mcp add`** — from the repo directory:
  `claude mcp add audial-dev -s local -e AUDIAL_USER_ID=... -e AUDIAL_API_KEY=... -e
  AUDIAL_API_BASE_URL=<dev API>/api -e AUDIAL_RESULTS_DIR=/tmp/audial-e2e -e
  AUDIAL_JOB_TIMEOUT_S=1500 -- uv run --directory "$PWD" audial-mcp` → added to local config.
  `claude mcp list` confirmed: `audial-dev: uv run --directory .../audial-mcp audial-mcp - ✔
  Connected`.

- [x] **9b. Non-interactive prompt** —
  `claude -p "Use the audial-dev analyze tool on <song30s.wav path> and tell me the bpm and
  key." --allowedTools "mcp__audial-dev__analyze"`. Claude called the tool (by its own account,
  twice) and got the same `API error: 500` both times (the known dev-environment issue from
  check #2 above), then reported back clearly: *"I couldn't get the BPM or key. The audial-dev
  `analyze` tool returned `Audio analysis failed: API error: 500` both times I ran it on
  `song30s.wav`. A 500 is a server-side error, so there's no analysis output to report..."*
  followed by sensible next steps (check backend logs, verify the file, try another input).
  This confirms the full Claude Code integration path — tool discovery, approval-free call via
  `--allowedTools`, argument passing, and error-text relay — works end-to-end; the only failure
  is the same upstream dev 500 documented above, not anything in the Claude Code wiring.

- [x] **9c. Cleanup** — `claude mcp remove audial-dev -s local` → removed;
  `claude mcp list` confirmed no `audial` entries remain.

## Orphan check

- [x] **10.** `uv run audial-mcp </dev/null` with the dev env: exits **immediately** (duration
  0 s) with **exit code 0** when stdin closes, after logging one stderr line: `INFO audial_mcp:
  audial-mcp 0.1.0; results dir /tmp/audial-e2e; credentials present`. No orphaned process.

## Prod (after Task 9 publishes 0.1.0)

- [ ] Not tested — out of scope for this task; `audial-mcp` 0.1.0 has not been published to
  PyPI/the registry yet (see `docs/release-checklist.md`). The one prod check this task *did*
  run is the 402/subscription-required path under "Error paths" above, using
  `api.audialmusic.ai` directly.
- [ ] Windows: not tested (see design open question 6).

## Summary

| # | Check | Result | Evidence |
|---|-------|--------|----------|
| 1 | `list_tools` | PASS | 10 tools, 0 undocumented params |
| 2 | `analyze` | **FAIL (remote)** | dev `primary-analysis` 500s; exe `-P2hpB7GckSe2RMsf-ue` |
| 3 | `stem_split` | **FAIL (remote)** | same root cause as #2 |
| 4 | `generate_music` | PASS | 118.8 s, exe `-P2hpd-1LtuDAxM7zc9g`, `lm_model` ok, redacted |
| 5 | `sound2vital` | PASS | 104.8 s, exe `-P2hpdM-CErNCfFTpzEb`, `.vital` present |
| 6 | `text2vox` | **FAIL (remote)** | CUDA OOM x2, exe `-P2hpdaNWIoqDHlG3wE4` / `-P2hprIE4e1WGLsIRJZ4` |
| 7 | `list_results` | PASS | newest-first, filter, `..` refused |
| 8 | error paths | PASS | missing file / missing creds / prod 402 all readable |
| 9 | Claude Code | PASS | connects, calls tool, relays the dev 500 clearly, cleans up |
| 10 | orphan check | PASS | exit 0, immediate, on stdin close |

**No `audial-mcp` code changes were made.** Every failure in this run (#2, #3, #6) traces to the
dev-environment API and Audial's hosted GPU workers (a 409-wrapped-as-500 from
`primary-analysis`, and a CUDA OOM on the text2vox worker with ~20 GB already allocated
before the job started) — confirmed by
reproducing #2's failure with a raw SDK call that never touches `audial-mcp`, and by
`stem_split` (#3) failing at the exact same shared `primary-analysis` step. `audial-mcp` itself
behaved correctly throughout: it surfaced every remote failure as a readable `ToolError`
(including the full upstream traceback for #6), never left an empty job folder behind, and its
progress notifications, redaction, filtering, and process lifecycle all passed.

Unit tests and lint are unaffected by this run: `uv run pytest -q -W error` (40 passed) and
`uv run ruff check .` (clean) both pass on `develop` before and after.
