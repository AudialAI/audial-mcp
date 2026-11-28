# Audial in Claude: the connector directory pack

Everything the portal at [claude.ai/directory/manage](https://claude.ai/directory/manage)
asks for, ready to paste. Written 2026-11-27 against
[connectors/building/submission](https://claude.com/docs/connectors/building/submission),
the [review criteria](https://claude.com/docs/connectors/building/review-criteria) and the
[Anthropic Software Directory Policy](https://support.claude.com/en/articles/13145358-anthropic-software-directory-policy)
(updated 2026-10-27). `tests/test_listing.py` checks the limits.

## 0. Decided: the Claude listing leaves out music and voice generation

The directory policy, section 4, verbatim:

> Unless otherwise expressly permitted by us in writing, we do not allow Software with certain
> capabilities into our Directories. [...] B. Software that uses AI models to generate images,
> video, or audio content. [...] This includes: [...] b. Audio and voice models
> (speech-to-speech/native audio models, text-to-speech, music models)

**Decision (Zach, 2026-11-28):** the Claude listing gets its own endpoint,
`https://mcp.audialmusic.ai/claude/mcp`, on the same server and process. It serves every tool
but the two that generate music or a voice: `generate_music` and `text2vox`. They are neither
listed nor callable there; a call to either fails like a call to a tool that doesn't exist. No
permission email goes to Anthropic (the unsent draft is section 9 of this file at commit
ec5786f).

Audial's tools against 4B:

| Group | Tools | Under 4B? | On `/claude/mcp` |
|---|---|---|---|
| Generate audio with a model | `generate_music` (the Audial music model), `text2vox` (sings in a voice) | Yes, by name ("music models", "text-to-speech") | Left out |
| Return audio a model made from the user's audio | `stem_split`, `generate_samples` | Arguably: "uses AI models to generate ... audio content" | Kept, by Zach's choice |
| Process audio without an AI model | `master` (Matchering: signal processing that matches a reference, no model) | No | Kept |
| Return data or files that aren't audio | `analyze`, `segment`, `audio_features`, `tag_audio`, `get_lyrics`, `generate_midi`, `sound2vital`, `sound2serum`, `service_status`, `usage`, `pipeline_status`, `check_job`, `account` | No | Kept |

- `https://mcp.audialmusic.ai/mcp` stays as it is, with all 18 tools, for ChatGPT
  (`docs/LISTING-OPENAI.md`).
- Each endpoint's tool list is fixed at submission. Adding either tool to `/claude/mcp` later
  is a material change (section 9) and would need Anthropic's written permission first.
- The two endpoints share everything else:
  - jobs, `check_job`, results and download links (a job started on one can be checked on the
    other);
  - the Audial account. A sign-in made for one endpoint is refused on the other (each checks
    the token's audience), so Claude signs in for `/claude/mcp` itself. A token issued for the
    whole server, which the API issues when a client names no resource, and a user's own API
    key work on both.

## 1. Connection

- **Server URL:** `https://mcp.audialmusic.ai/claude/mcp`.
- **How users connect:** Universal URL (everyone uses the same URL).
- **OAuth discovery:** a call without a token answers 401, naming
  `https://mcp.audialmusic.ai/.well-known/oauth-protected-resource/claude/mcp`. That metadata
  names the resource `https://mcp.audialmusic.ai/claude/mcp`, the authorization server
  `https://api.audialmusic.ai` and the scope `audial`.

## 2. Tools

The portal syncs them from the server: 16 tools, every remote tool but `generate_music` and
`text2vox`. It groups them as read-only or write. Each tool has a `title`, and every applicable
hint is set, the same as on `/mcp`:
- read-only: `service_status`, `usage`, `pipeline_status`, `check_job`, `account`;
- writes: the 11 tools that start a job (`readOnlyHint: false`, `destructiveHint: false`);
- destructive: none.

`service_status` reports only on the tools this endpoint serves.

`tests/test_annotations.py` holds the decision for each tool. No tool name is over 64
characters, and no tool takes free-form API requests.

## 3. Listing

```json
{
  "server_url": "https://mcp.audialmusic.ai/claude/mcp",
  "name": "Audial",
  "one_liner": "Split stems, analyse songs, master mixes, turn audio into MIDI and build sample packs and synth presets, from a file you link.",
  "description": "Audial runs hosted music production tools on your Audial account. Give Claude a direct link to an audio file and ask in plain words; Audial runs the job on its own servers and returns download links that work for 24 hours.\n\nWhat it does:\n- Split a song into stems (vocals, drums, bass and the rest, or an instrumental), optionally retimed or moved to a new key.\n- Analyse a track: tempo, key and loudness in seconds; sections, bars and a tempo map; energy, danceability and loudness per section; genre, instrument and mood tags with a description; and timed lyrics.\n- Master a finished mix, optionally matching a reference track.\n- Turn audio into MIDI, and build a sample pack of one-shots and loops from a track.\n- Turn one isolated sound into an editable preset for Audial Synth (.vital) or Xfer Serum 2.\n\nJobs take from a few seconds to a few minutes. A job that outlasts a reply returns a job id, and Claude checks back for the result. Files up to 100 MB; links must point to the file itself, not to a web or streaming page. Only use audio you have the rights to.",
  "categories": ["Media", "Productivity"],
  "documentation_url": "https://audialmusic.ai/integrations/mcp-server",
  "privacy_policy_url": "https://audialmusic.ai/legal/privacy",
  "support_contact": "contact@audialmusic.ai",
  "icon": "bundle/icon.png",
  "slug": "audial",
  "example_prompts": [
    "What's the tempo and key of https://example.com/my-song.mp3?",
    "Split https://example.com/my-song.mp3 into vocals, drums, bass and other stems.",
    "Find the sections of https://example.com/my-song.mp3 and give me its lyrics with timestamps.",
    "Turn https://example.com/piano-melody.wav into a MIDI file.",
    "Master https://example.com/my-mix.wav so it matches the loudness and tone of https://example.com/reference.mp3."
  ]
}
```

**Notes on the fields:**
- **Categories:** pick from the portal's list, which Anthropic doesn't publish; these are
  placeholders. Choose the closest one or two: media or creative, then productivity.
- **Documentation URL:** the existing MCP page. It needs a section on connecting from Claude and
  ChatGPT with no install, which is website work (W8). The policy requires public
  documentation by the publish date (3C): how it works, what it's for, and troubleshooting.
- **Icon:** `bundle/icon.png` (512 × 512 PNG).
- **Slug:** `audial`. It is permanent once published.
- **Example prompts:** Claude can't attach audio to a connector's tools, so each prompt carries
  a link. In the portal, and for a reviewer, replace `https://example.com/...` with links to
  Zach's own sample files: a song with vocals, a short solo melody, and for the last prompt an
  unmastered mix and a reference track.
- The portal writes its own disclaimer on connector cards; it isn't customisable.

## 4. Use cases

- **Primary use cases:**
  - find a track's tempo, key, sections and lyrics;
  - split a song into stems;
  - master a mix;
  - turn a melody into MIDI;
  - build a sample pack;
  - make a synth preset from a sound.
- **What users need first:** an Audial account (sign up at audialmusic.ai). New accounts include
  a few runs; more runs need an Audial subscription. Claude's portal asks for this; it is not
  listing text.
- **Reads or writes:** both. Most tools start a job on the user's Audial account and save
  results there. The status and usage tools only read.

## 5. Company

- **Company name:** Audial
- **Website:** https://audialmusic.ai
- **Primary contact for review updates:** REPLACE_WITH_REVIEW_CONTACT_EMAIL (for example
  review@audialmusic.ai; Zach provides it).

## 6. Authentication

- **Authentication requirement:** Always required. Every tool acts on the user's Audial account.
- **Authentication method:** OAuth sign-in ("Sign in with Audial").
- **OAuth client:** none to enter, as long as the API offers one of these:
  - CIMD, advertising `client_id_metadata_document_supported: true` **and** `"none"` in
    `token_endpoint_auth_methods_supported`;
  - DCR (`registration_endpoint`).

  Claude registers itself either way. Anthropic recommends CIMD over DCR for high-traffic
  servers, since DCR registers a new client on every fresh connection. Use Anthropic-held
  client credentials only if the API supports neither.
- **Redirect URIs the API must accept:**
  - `https://claude.ai/api/mcp/auth_callback` (claude.ai, Desktop, mobile, Cowork);
  - `http://localhost:<any port>/callback` and `http://127.0.0.1:<any port>/callback`
    (Claude Code), matched without the port.

## 7. Data handling

- **The API:** Audial's own (api.audialmusic.ai). The MCP server's domain is Audial's too.
- **Personal health data:** no.
- **Sponsored content:** no.

## 8. Test & launch, then compliance

- **Test & launch:**
  1. Before submitting, Zach adds `https://mcp.audialmusic.ai/claude/mcp` as a custom
     connector in Claude (Settings → Connectors → Add custom connector).
  2. Sign in and call each of the 16 tools once from a conversation, with the sample files.
     Check that the tool list has no `generate_music` or `text2vox`.
  3. Tick the confirmation.

  No credentials go into the portal. If the listing is escalated to Verified review, a reviewer
  emails for test access: send the reviewer account from `docs/LISTING-OPENAI.md` section 6.
- **Compliance:** seven acknowledgments, all required. They cover:
  - the directory guidelines;
  - first-party API use (yes: Audial's own API);
  - financial transactions (none);
  - **AI media generation (see section 0):** the endpoint generates no music or voice. Stem
    splitting and sample packs use AI models on the user's own audio: say so if the form asks.
    Mastering uses no AI model;
  - prompt injection (the descriptions say what each tool does and never tell Claude how to
    behave beyond that);
  - conversation data (the server receives only each tool call's arguments and keeps nothing
    else from the conversation);
  - public documentation (section 3).

## 9. After submitting

- **The automatic scan:** it lists the server as a Community connector by default. Status and
  feedback appear in the portal. Escalations go to `mcp-review@anthropic.com`.
- **Material changes** to tools, descriptions, data practices or endpoints must be reported
  to Anthropic (policy 3H) and may be re-reviewed. Changing a tool's name, description or hints
  in audial-mcp changes it on `/claude/mcp` too, unless it is one of the two left out.
- **Not for this listing:** the directory no longer accepts local servers packaged as MCPB
  desktop extensions. A local server (audial-live) can only be listed inside a Claude plugin
  ([plugins/submit](https://claude.com/docs/plugins/submit)).
