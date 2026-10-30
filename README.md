# audial-mcp

[![AllMCPs Verified](https://allmcps.com/api/badge/audial)](https://allmcps.com/mcp/audial?verify=655ada1a-49b7-4930-9ec2-2e5ed7eaa6c6)

Powered by [Audial](https://audialmusic.ai?utm_source=github&utm_medium=readme&utm_campaign=mcp) — run Audial's hosted audio tools from any MCP
client: split stems, analyze and segment audio, master tracks, build sample packs, convert
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
| `segment` | Sections and component analysis -- splits stems, then segments them; one call |
| `master` | Mastering, optionally matched to a reference |
| `generate_samples` | Sample pack from a track -- splits stems, then builds the pack; one call |
| `generate_midi` | Audio to MIDI |
| `generate_music` | Text to music, covers, remixes, lego and understand with the Audial music model |
| `sound2vital` | One isolated sound → editable Audial Synth (Vital) preset. Not for loops or melodies |
| `sound2serum` | One isolated sound → editable Xfer Serum 2 (.SerumPreset) preset. Not for loops or melodies |
| `text2vox` | Lyrics + reference voice → sung vocal and MIDI |
| `pipeline_status` | Look up a pipeline by id: its state, each step, and the outputs so far |
| `service_status` | Which tools have a worker awake right now, and what a cold start will cost |
| `list_results` | Browse previous results in your results folder |

`segment` and `generate_samples` run as Audial *pipelines*: both need separated stems, so the
API splits stems and then runs the step you asked for as one job. Each step is reported as it
starts and finishes ("stem-split completed in 41 s; sample-pack running"), and the pipeline id
comes back as the result's `execution_id` -- pass it to `pipeline_status` to check on a job
whose call timed out.

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

Audial's GPU workers scale to zero, so the first job after an idle period spends a few extra
minutes starting one and loading the model; every job after it is fast. Audial reports that
phase on the job it is already polling, and every tool here relays it as a progress
notification and a log line:

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

## License

MIT. Source: https://github.com/AudialAI/audial-mcp
