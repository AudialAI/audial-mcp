# audial-mcp

[![AllMCPs Verified](https://allmcps.com/api/badge/audial)](https://allmcps.com/mcp/audial?verify=655ada1a-49b7-4930-9ec2-2e5ed7eaa6c6)

Powered by [Audial](https://audialmusic.ai?utm_source=github&utm_medium=readme&utm_campaign=mcp) — run Audial's hosted audio tools from any MCP
client: split stems, analyze audio, find a song's sections, tempo and bars and add audio
features, tags or timed lyrics to that analysis, master tracks, build sample packs, convert
audio to MIDI, generate music, turn a one-shot into an editable Audial Synth or Serum 2
preset, and sing lyrics in a reference voice. See the [API reference](https://audialmusic.ai/resources/api-reference?utm_source=github&utm_medium=readme&utm_campaign=mcp)
for details on what each tool calls.

mcp-name: io.github.AudialAI/audial-mcp

## Install

You need an Audial account (https://audialmusic.ai?utm_source=github&utm_medium=readme&utm_campaign=mcp) and
[`uv`](https://docs.astral.sh/uv/getting-started/installation/). There is no API key to copy:
the first time a tool runs, a sign-in page opens in your browser. Approve it and the tool
carries on.

**Claude Code**

```bash
claude mcp add audial -e AUDIAL_RESULTS_DIR=~/Audial -- uvx audial-mcp
```

**Claude Desktop, one click**

Download `audial-mcp-<version>.mcpb` from the [latest release](https://github.com/AudialAI/audial-mcp/releases/latest) and double-click it. Claude Desktop fetches what it needs, so there is nothing else to install or configure.

**Claude Desktop / Cursor / any client with a JSON config**

```json
{
  "mcpServers": {
    "audial": {
      "command": "uvx",
      "args": ["audial-mcp"],
      "env": {
        "AUDIAL_RESULTS_DIR": "~/Audial"
      }
    }
  }
}
```

### Signing in

The sign-in is saved on your computer (`~/.audial/credentials.json`, readable only by you) and
shared with the `audial` command line and Python SDK, so `audial login` in a terminal signs
the MCP server in too. If you have not approved within two minutes the tool returns the
sign-in link instead; approve it and ask again. The `account`, `sign_in` and `sign_out` tools
show and change who is signed in, and each connected computer is listed, and can be
disconnected, under Apps & API Keys in your profile at audialmusic.ai.

On a server or in CI, where nobody can sign in, set `AUDIAL_API_KEY` to a key created on that
page. A key set this way takes priority over a saved sign-in.

## Tools

| Tool | What it does |
|---|---|
| `stem_split` | Split a track into vocals, drums, bass, other (optionally retime / rekey) |
| `analyze` | BPM, key and other characteristics |
| `segment` | Song sections, tempo and bars from the mix, in about half a minute; returns an `analysis_id` |
| `audio_features` | Energy, danceability, loudness, mode, key and tatum, for the song and each section |
| `tag_audio` | Genre, moods, instruments and artist tags for the mix and each part, plus a written description |
| `get_lyrics` | Lyrics as timed lines (word timings in the saved file) |
| `master` | Mastering, optionally matched to a reference |
| `generate_samples` | Sample pack from an analysis, or from a file in one call |
| `generate_midi` | Audio to MIDI |
| `generate_music` | Text to music, covers, remixes, lego and understand with the Audial music model |
| `sound2vital` | One isolated sound → editable Audial Synth (Vital) preset. Not for loops or melodies |
| `sound2serum` | One isolated sound → editable Xfer Serum 2 (.SerumPreset) preset. Not for loops or melodies |
| `text2vox` | Lyrics + reference voice → sung vocal and MIDI |
| `pipeline_status` | Look up a pipeline by id: its state, each step, and the outputs so far |
| `service_status` | Which tools have a worker awake right now, and what a cold start will cost |
| `list_results` | Browse previous results in your results folder |

## Analysis and add-ons

`segment` analyses the mix in about half a minute: no stems needed. It returns the sections
(with their phrases) and a tempo map, plus an `analysis_id`. Beats and bars aren't listed one by
one: `grid` describes them -- where each constant-tempo piece starts, its BPM, and its bar and
beat. Every beat time is `start + k × 60 / bpm`. Most tracks have one piece.

Pass the `analysis_id` to `audio_features`, `tag_audio`, `get_lyrics` or `generate_samples` to
add that result to the analysis, whenever you like. Nothing is uploaded again. Each of them also
takes a `file_path` instead, and then runs `segment` on it first.

- Audio features need nothing more, so they can run straight after the analysis.
- Tags, lyrics and the sample pack read the track's stems. Audial makes them automatically
  after each analysis, so right after a new one these tools wait a few minutes for them.
- If a tool analysed a file and the next step failed or timed out, the error gives the new
  `analysis_id`: call the tool again with it, rather than with the file.

An analysis counts as one run. Everything you add to it (features, tags, lyrics, a sample pack)
is included.

`generate_samples` on a file runs as an Audial *pipeline*: it prepares the track (analysis and
stems together), then builds the pack, as one job. Each step is reported as it starts and
finishes ("prepare completed in 41 s; sample-pack running"). The pipeline id comes back as the
result's `execution_id` -- pass it to `pipeline_status` to check on a job whose call timed out.
`segment` with `features` (kept for older integrations) is a pipeline as well.

## Per-note lyrics

`text2vox` lines lyrics up with a melody. `lyrics_mode="per_note"` is the mode that gives
exact control: the lyrics are pipe-delimited, one token per MIDI note, and a **hold** keeps
the previous syllable going through the next note.

| Token | Meaning |
|---|---|
| `grace` | Sung on this note |
| `-` | Hold: continue the previous syllable through this note (the documented form) |
| `+` | Hold as well |
| *empty* | Hold as well -- what a double pipe produces |

```
amazing|-|-|grace|-|how|sweet|the|sound
```

That sings "amazing" across notes 1-3 and "grace" across notes 4-5, and needs a MIDI file
with exactly nine notes. There is no rest token: a note that should be silent should not be
in the MIDI. The token count must equal the note count, and the first token must be a
syllable -- a lyric string that breaks either rule comes back as a tool error naming the
note it is about ("note 1 has no syllable; the first note needs one (position 1)"), before
anything is uploaded.

## Cold starts

Audial's workers scale to zero, so the first job after an idle period spends extra time
starting one and loading the model -- a few minutes on a GPU tool; every job after it is fast.
The analysis and each add-on (`audio_features`, `tag_audio`, `get_lyrics`) run on workers of
their own, so the first add-on after a quiet spell can start up for a minute or more even when
`segment` was quick. Audial reports that phase on the job it is already polling, and every
tool here relays it as a progress notification and a log line:

```
Warming up the machine — the first request takes a few minutes longer than usual
(about 3 min); every run after this is faster
Working on your job…
```

It is the same sentence the Audial website, CLI and plugins show. `service_status` answers
the same question before you start anything, so a long wait can be explained in advance.

## What leaves your machine

Audio and text you pass to a tool are uploaded to Audial's API (https://api.audialmusic.ai)
over HTTPS and processed on Audial's servers; results are downloaded into
`AUDIAL_RESULTS_DIR` (default `~/Audial`). Nothing else is sent.

A tool given an `analysis_id` uploads nothing: it works on the audio Audial already has for
that analysis. Audial makes and keeps the track's stems with each analysis, like your
other results. To fill in artist tags, `tag_audio` has Audial's servers look up the track's
title and artist in a public music database; that lookup gets the title and artist, never the
audio.

Two other files are *read* if they exist, because the Audial SDK looks for them:
`~/.audial/.audial_config.json` and a `.env` in the directory the server was started from.
Credentials from your MCP client's config always win: `audial-mcp` snapshots its environment
before the SDK loads either file, so neither can redirect your credentials or the API host.

Every tool except `list_results` calls the Audial API, which requires an active Audial subscription on the account; without one the tool returns the API's subscription message.

## Configuration

| Variable | Required | Default |
|---|---|---|
| `AUDIAL_API_KEY` | no | browser sign-in |
| `AUDIAL_USER_ID` | no; only with an older API key that does not start with `aud_` | |
| `AUDIAL_RESULTS_DIR` | no | `~/Audial` |
| `AUDIAL_JOB_TIMEOUT_S` | no | `900` |
| `AUDIAL_API_BASE_URL` | no | production API |

## Remote server (streamable HTTP)

`audial-mcp --http` runs the same tools as a remote MCP server, for clients that connect to
a URL instead of starting a local process. It serves streamable HTTP at `/mcp`.

```bash
uvx audial-mcp --http --host 0.0.0.0 --port 8000 --public-url https://mcp.example.com
```

It works differently from the local server:

- **Each request brings its own Audial API key** as `Authorization: Bearer aud_...`, so one
  server can serve many accounts. The server never uses a key from its own environment or a
  sign-in saved on the host. The browser sign-in tools and `list_results` aren't offered.
- **Files go in as links.** Each file argument is an https link to the file, or a file the
  client attached, given as `{"download_url": ..., "file_id": ...}`. The server downloads it,
  up to 100 MB, from public addresses only, and deletes it when the job ends. Local paths
  are refused.
- **Results come back as download links.** Each file's `path` is a link under
  `<public url>/files/`, valid for 24 hours.
- **Long jobs don't hold the call open.** A job still running after 40 seconds returns
  `status: running` with a `job_id`. `check_job` takes that id and returns the result once
  the job is done. Only the account that started a job can check it.

| Variable / option | Default |
|---|---|
| `--host` / `AUDIAL_MCP_HOST` | `127.0.0.1` |
| `--port` / `AUDIAL_MCP_PORT` / `PORT` | `8000` |
| `--public-url` / `AUDIAL_MCP_PUBLIC_URL` (base of the download links) | `http://host:port` |
| `--authorization-server` / `AUDIAL_MCP_AUTHORIZATION_SERVER` (published as OAuth protected-resource metadata) | none |
| `AUDIAL_MCP_WAIT_S` (how long a call waits before returning a `job_id`) | `40` |
| `AUDIAL_MCP_RESULTS_TTL_S` (how long download links last) | `86400` |
| `AUDIAL_MCP_WORK_DIR` (downloaded inputs and results) | system temp folder |

Jobs and results are kept by the process and its work folder, so run one instance.
`GET /healthz` reports the version.

## License

MIT. Source: https://github.com/AudialAI/audial-mcp
