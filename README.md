# audial-mcp

Run Audial's hosted audio tools from any MCP client: split stems, analyze and segment audio,
master tracks, build sample packs, convert audio to MIDI, generate music, turn a one-shot into
an editable Audial Synth preset, and sing lyrics in a reference voice.

mcp-name: io.github.AudialAI/audial-mcp

## Install

You need an Audial account (user id + API key from https://audialmusic.ai) and
[`uv`](https://docs.astral.sh/uv/getting-started/installation/).

**Claude Code**

```bash
claude mcp add audial \
  -e AUDIAL_USER_ID=your-user-id -e AUDIAL_API_KEY=your-api-key -e AUDIAL_RESULTS_DIR=~/Audial \
  -- uvx audial-mcp
```

**Claude Desktop / Cursor / any client with a JSON config**

```json
{
  "mcpServers": {
    "audial": {
      "command": "uvx",
      "args": ["audial-mcp"],
      "env": {
        "AUDIAL_USER_ID": "your-user-id",
        "AUDIAL_API_KEY": "your-api-key",
        "AUDIAL_RESULTS_DIR": "~/Audial"
      }
    }
  }
}
```

## Tools

| Tool | What it does |
|---|---|
| `stem_split` | Split a track into vocals, drums, bass, other (optionally retime / rekey) |
| `analyze` | BPM, key and other characteristics |
| `segment` | Sections and component analysis |
| `master` | Mastering, optionally matched to a reference |
| `generate_samples` | Sample pack from a track |
| `generate_midi` | Audio to MIDI |
| `generate_music` | Text to music, covers, remixes, extraction, completion with the Audial music model |
| `sound2vital` | One-shot → editable Audial Synth (Vital) preset |
| `text2vox` | Lyrics + reference voice → sung vocal and MIDI |
| `list_results` | Browse previous results in your results folder |

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
| `AUDIAL_USER_ID` | yes | |
| `AUDIAL_API_KEY` | yes | |
| `AUDIAL_RESULTS_DIR` | no | `~/Audial` |
| `AUDIAL_JOB_TIMEOUT_S` | no | `900` |
| `AUDIAL_API_BASE_URL` | no | production API |

## License

MIT. Source: https://github.com/AudialAI/audial-mcp
