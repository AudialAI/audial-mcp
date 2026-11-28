# Audial in ChatGPT: the plugin listing pack

Everything the OpenAI submission asks for, ready to paste. Written 2026-11-27 against
[deploy/submission](https://developers.openai.com/plugins/deploy/submission) and
[plugin-guidelines](https://developers.openai.com/plugins/plugin-guidelines).
`tests/test_listing.py` checks the limits (subtitle 30 characters, description 4000, exactly 5
positive and 3 negative test cases, the countries) and that no listing text mentions prices,
plans or upgrades.

**Before submitting:**
- OAuth ("Sign in with Audial") is live on api.audialmusic.ai.
- The server runs at `https://mcp.audialmusic.ai/mcp` (`.do/app-mcp.yaml`). ChatGPT uses this
  endpoint, with every tool. (The Claude listing uses `/claude/mcp`, without music and voice
  generation: `docs/LISTING-CLAUDE.md`.)
- The privacy policy has the additions in `docs/PRIVACY-CHECKLIST-LISTINGS.md`.

**Zach fills in four things:** the links to his sample files, the video link, and the
reviewer account's email and password. Each is marked `REPLACE_WITH_…`.

## 1. The package

A folder zipped as one file:

```
audial/
├── plugin.json          # section 2
├── mcp.json             # section 3
└── assets/
    └── logo.png         # a copy of bundle/icon.png (512 × 512 PNG)
```

Upload it at platform.openai.com, under the organization verified as "Audial". Nothing in
it is secret: reviewer credentials go into the dashboard's **Review details** form (section 6),
never into the ZIP.

## 2. plugin.json

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "audial",
  "version": "1.0.0",
  "description": "Audial's audio tools in ChatGPT: stems, analysis, mastering, MIDI, sample packs, music, synth presets and sung vocals.",
  "author": { "name": "Audial", "email": "contact@audialmusic.ai", "url": "https://audialmusic.ai" },
  "homepage": "https://audialmusic.ai",
  "license": "MIT",
  "keywords": ["music", "audio", "stems", "mastering", "midi", "lyrics", "bpm", "key", "sample pack", "synth preset", "vocals", "producer"],
  "extensions": {
    "com.openai": {
      "interface": {
        "displayName": "Audial",
        "shortDescription": "Stems, mastering, MIDI, music",
        "longDescription": "Audial brings hosted music production tools into ChatGPT. Attach a song, a stem or a single sound to the chat, or give a direct link to the file, and ask in plain words. Audial runs the job on its own servers and gives back download links.\n\nWhat you can do:\n- Split a song into stems (vocals, drums, bass and the rest, or an instrumental), optionally retimed to a new tempo or moved to a new key.\n- Analyse a track: tempo, key and loudness in seconds; sections, bars and a tempo map in about half a minute; energy, danceability and loudness for each section; genre, instrument and mood tags with a written description; and the lyrics as timed lines.\n- Master a finished mix, optionally matching a reference track.\n- Turn audio into MIDI.\n- Build a sample pack of one-shots and loops from a track.\n- Generate music from a text description with the Audial music model: with your lyrics, with lyrics Audial writes from your description, or as an instrumental. Make covers and remixes of audio you have the rights to.\n- Turn one isolated sound into an editable preset for Audial Synth (.vital) or Xfer Serum 2.\n- Sing your lyrics to a melody (MIDI or audio) in a reference voice. The voice must be your own, or you must have the consent of the person whose voice it is.\n\nWho it is for: producers, songwriters, DJs, beatmakers and anyone who works with audio and wants these jobs done in a conversation.\n\nHow it works:\n- Sign in with your Audial account when ChatGPT asks. Jobs run on that account and also appear in your Audial dashboard.\n- Jobs take from a few seconds (tempo and key) to a few minutes (stems, mastering, music). When a job outlasts a reply, ChatGPT says it is still running and checks back for the result.\n- Download links work for 24 hours.\n\nLimitations:\n- Files up to 100 MB: WAV, MP3, AIFF, FLAC, M4A, OGG or AAC audio, and MIDI for melodies.\n- A link must point to the file itself. Web pages and streaming links, such as YouTube or Spotify, can't be used.\n- Presets need one isolated sound, such as a single note or hit, not a loop or a melody.\n- The first job after a quiet period can take a few extra minutes while a server starts.\n- Only use audio you have the rights to.",
        "developerName": "Audial",
        "category": "Design",
        "capabilities": [
          "Split songs into stems",
          "Find tempo, key, sections, tags and timed lyrics",
          "Master a mix",
          "Turn audio into MIDI",
          "Build sample packs",
          "Generate music with the Audial music model",
          "Make synth presets from a sound",
          "Sing lyrics in your own voice"
        ],
        "websiteURL": "https://audialmusic.ai",
        "supportURL": "https://audialmusic.ai/resources/support",
        "privacyPolicyURL": "https://audialmusic.ai/legal/privacy",
        "termsOfServiceURL": "https://audialmusic.ai/legal/terms",
        "defaultPrompt": [
          "What's the tempo and key of this song?",
          "Split this song into vocals, drums and bass",
          "Turn this melody into a MIDI file"
        ],
        "brandColor": "#6366f1",
        "brandColorDark": "#818cf8",
        "logo": "./assets/logo.png"
      },
      "review": {
        "test_cases": {
          "positive": [
            {
              "description": "Tempo, key and loudness of an attached song, in seconds.",
              "prompt": "What's the tempo and key of this song?",
              "tools_triggered": "analyze",
              "expected_behavior": "Within a few seconds ChatGPT reports the song's BPM, key and loudness. The answer comes from the attached file; nothing is returned to download.",
              "file_attachment_urls": ["REPLACE_WITH_LINK_TO_SAMPLE_SONG"]
            },
            {
              "description": "Stems of an attached song, as download links.",
              "prompt": "Split this song into vocals, drums, bass and other stems.",
              "tools_triggered": "stem_split, check_job",
              "expected_behavior": "Audial starts a stem split. If it is still running after about 40 seconds, ChatGPT says it is in progress and calls check_job with the job_id until it finishes (a few minutes; longer if a server has to start). It ends with four download links (vocals, drums, bass, other) that work for 24 hours.",
              "file_attachment_urls": ["REPLACE_WITH_LINK_TO_SAMPLE_SONG"]
            },
            {
              "description": "Song sections, then timed lyrics from the same analysis.",
              "prompt": "Find the sections of this song and give me its lyrics with timestamps.",
              "tools_triggered": "segment, get_lyrics, check_job",
              "expected_behavior": "segment returns the tempo, key and the sections (intro, verse, chorus and so on) with their start times and bars, plus an analysis_id. get_lyrics is then called with that analysis_id, without uploading the file again, and returns the lyrics as timed lines. The lyrics can take a few minutes while Audial prepares the vocal; ChatGPT uses check_job if needed.",
              "file_attachment_urls": ["REPLACE_WITH_LINK_TO_SAMPLE_SONG"]
            },
            {
              "description": "An attached melody turned into MIDI.",
              "prompt": "Turn this piano melody into a MIDI file.",
              "tools_triggered": "generate_midi",
              "expected_behavior": "ChatGPT returns a download link to a .mid file with the melody's notes. If the job takes longer than about 40 seconds, ChatGPT says so and calls check_job for the result.",
              "file_attachment_urls": ["REPLACE_WITH_LINK_TO_SAMPLE_MELODY"]
            },
            {
              "description": "A short instrumental generated from a description.",
              "prompt": "Make a 30-second instrumental lo-fi hip hop beat at 80 BPM with soft piano and vinyl crackle.",
              "tools_triggered": "generate_music, check_job",
              "expected_behavior": "Audial generates an instrumental of about 30 seconds with the Audial music model. ChatGPT says it is in progress, calls check_job until it finishes (a few minutes), then gives an MP3 download link and the track's length."
            }
          ],
          "negative": [
            {
              "description": "Imitating a real singer's voice without their consent. Audial's reference-voice tool needs the user's own voice or the owner's consent, so ChatGPT should decline and offer to use a recording of the user's own voice. No Audial tool runs.",
              "prompt": "Make Taylor Swift sing these lyrics I wrote: 'City lights, we're running late'."
            },
            {
              "description": "A streaming page instead of an audio file. Audial takes only attached files or links to the file itself, so ChatGPT should ask for the audio file. If stem_split is called with the page, it refuses the link and asks for the file, and no job runs.",
              "prompt": "Split the stems of this song: https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            },
            {
              "description": "A request outside what Audial does. Song recommendations are not an Audial tool, so ChatGPT should answer by itself without calling Audial.",
              "prompt": "Recommend five songs that sound like Daft Punk."
            }
          ]
        },
        "demo_recording_url": "REPLACE_WITH_LINK_TO_THE_WALKTHROUGH_VIDEO",
        "commerce": false,
        "commerce_description": "Audial sells nothing inside ChatGPT. Users sign in to an Audial account they already have."
      },
      "publication": {
        "countries": ["AD", "AE", "AF", "AG", "AL", "AM", "AO", "AR", "AT", "AU", "AW", "AX", "AZ", "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI", "BJ", "BL", "BM", "BN", "BO", "BR", "BS", "BT", "BW", "BZ", "CA", "CD", "CF", "CG", "CH", "CI", "CL", "CM", "CO", "CR", "CV", "CY", "CZ", "DE", "DJ", "DK", "DM", "DO", "DZ", "EC", "EE", "EG", "ER", "ES", "ET", "FI", "FJ", "FM", "FO", "FR", "GA", "GB", "GD", "GE", "GF", "GH", "GL", "GM", "GN", "GP", "GQ", "GR", "GT", "GW", "GY", "HN", "HR", "HT", "HU", "ID", "IE", "IL", "IQ", "IS", "IT", "JM", "JO", "JP", "KE", "KG", "KH", "KI", "KM", "KN", "KR", "KW", "KY", "KZ", "LA", "LB", "LC", "LI", "LK", "LR", "LS", "LT", "LU", "LV", "LY", "MA", "MC", "MD", "ME", "MF", "MG", "MH", "MK", "ML", "MM", "MN", "MQ", "MR", "MT", "MU", "MV", "MW", "MX", "MY", "MZ", "NA", "NC", "NE", "NG", "NI", "NL", "NO", "NP", "NR", "NZ", "OM", "PA", "PE", "PF", "PG", "PH", "PK", "PL", "PM", "PS", "PT", "PW", "PY", "QA", "RE", "RO", "RS", "RW", "SA", "SB", "SC", "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM", "SN", "SO", "SR", "SS", "ST", "SV", "SZ", "TD", "TF", "TG", "TH", "TJ", "TL", "TM", "TN", "TO", "TR", "TT", "TV", "TW", "TZ", "UA", "UG", "US", "UY", "UZ", "VA", "VC", "VN", "VU", "WF", "WS", "YE", "YT", "ZA", "ZM", "ZW"],
        "release_notes": "First release. Audial's audio tools in ChatGPT: stem splitting; analysis (tempo, key, sections, audio features, tags and timed lyrics); mastering; audio to MIDI; sample packs; music generation with the Audial music model; synth presets from one sound; and sung vocals in a reference voice the user owns or has consent to use. Files come from the chat or a direct link; results are download links that work for 24 hours. Jobs longer than a reply return a job id that ChatGPT checks back on."
      }
    }
  }
}
```

**Notes on the fields:**
- **Category:** the dashboard has its own list, which OpenAI doesn't publish. "Design" is a
  placeholder. Pick a music or audio category if there is one, then a creative-tools one, then
  Productivity.
- **Countries:** every country and territory on OpenAI's supported list
  ([supported-countries](https://developers.openai.com/api/docs/supported-countries), 208
  entries on 2026-11-27) except India (IN), as the website blocks it. If the portal refuses a
  code, drop that code only.
- **Logo:** `bundle/icon.png` copied to `assets/logo.png`. It is square, 512 px, a PNG and under
  5 MiB, as required. No dark variant is needed.
- **Brand colours:** the site's indigo. `#6366f1` has a contrast of 4.47:1 on white, and
  `#818cf8` 5.40:1 on #212121; OpenAI needs 2:1.
- **Sample files:** use Zach's own sounds, at links a reviewer can open without signing in:
  - a full song with vocals, about 1–3 minutes (cases 1–3);
  - a solo piano or synth melody, 10–30 s (case 4).
- **Starter prompts** need no attachment to make sense, so ChatGPT can show them as they are.

## 3. mcp.json

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "audial": {
      "type": "streamable-http",
      "url": "https://mcp.audialmusic.ai/mcp"
    }
  }
}
```

The URL can't change once the plugin is published. There are no `headers`, because public
submission refuses them: each user signs in through OAuth.

## 4. Domain verification

1. The portal shows a token.
2. Set it as `AUDIAL_MCP_DOMAIN_CHALLENGE` on the App Platform service (console → Settings →
   Environment variables) and redeploy.
3. Check that `https://mcp.audialmusic.ai/.well-known/openai-apps-challenge` returns the token
   as plain text.
4. Press Verify.

## 5. Scan Tools

The portal lists the server's tools. Each one should show:
- a title;
- explicit `readOnlyHint`, `destructiveHint` and `openWorldHint` values;
- `securitySchemes` (oauth2).

Read-only: `service_status`, `usage`, `pipeline_status`, `check_job`, `account`. Every tool
that starts a job is a write. Nothing is destructive. `tests/test_annotations.py` holds the
decision for each tool.

## 6. Review details (dashboard, not the ZIP)

**Reviewer account.** A dedicated Audial account, never a real user's:
- email and password sign-in, with no MFA, email codes or magic links (the website supports
  email and password);
- an active subscription at no charge (admin override), so every test case runs;
- no other setup.

Paste into the form:

> **Login URL:** https://audialmusic.ai (ChatGPT opens Audial's sign-in page when you connect
> the plugin).
> **Email:** REPLACE_WITH_REVIEWER_EMAIL
> **Password:** REPLACE_WITH_REVIEWER_PASSWORD
>
> **Steps:**
> 1. In ChatGPT, add Audial and choose Connect.
> 2. On Audial's page, sign in with the email and password above, then approve access for
>    ChatGPT.
> 3. Run the test cases with the sample files linked in each one.
>
> **What to expect:**
> - Jobs take from a few seconds (tempo and key) to a few minutes (stems, music). A job still
>   running after about 40 seconds comes back with a job id, and ChatGPT checks on it with
>   check_job.
> - The first job after a quiet period can take a few extra minutes while a server starts.
> - Results are download links that work for 24 hours.

**Attestations:** Audial sells nothing in ChatGPT (`commerce: false`), serves no ads, and
collects only what each job needs.

## 7. Walkthrough video script (2–3 minutes; Zach records)

Record ChatGPT in a browser at 1080p or higher, signed in to the reviewer account, with the two
sample files on the desktop. Keep the narration as captions or a voice-over. The video
demonstrates the five test cases, as OpenAI asks.

| Time | On screen | Say |
|---|---|---|
| 0:00–0:15 | ChatGPT home. Open the plugin directory, find Audial. | "This is Audial in ChatGPT: music production tools you use by asking. I'll connect it, then run five jobs." |
| 0:15–0:35 | Connect → Audial's sign-in page → sign in with email and password → Approve → back in ChatGPT. | "Connecting opens Audial's own sign-in page. I sign in and approve ChatGPT. Jobs now run on this Audial account." |
| 0:35–0:55 | Attach the song. Type "What's the tempo and key of this song?" Show the answer. | "Tempo, key and loudness come back in seconds." |
| 0:55–1:30 | Same chat: "Split this song into vocals, drums, bass and other stems." Show "still running" and the check-back, then four links. Click one; the file downloads. | "A stem split takes a few minutes. ChatGPT says it's still running and checks back by itself. Each stem is a download link that works for 24 hours." |
| 1:30–1:55 | "Find the sections of this song and give me its lyrics with timestamps." Show the section list, then the timed lines. | "Audial finds the sections and bars, then transcribes the lyrics from the same analysis, without uploading the song twice." |
| 1:55–2:10 | New message, attach the melody: "Turn this piano melody into a MIDI file." Show the .mid link. | "Audio to MIDI, ready for any DAW." |
| 2:10–2:40 | "Make a 30-second instrumental lo-fi hip hop beat at 80 BPM with soft piano and vinyl crackle." Show progress, then play the MP3 for a few seconds. | "Audial can also generate music with the Audial music model, here a 30-second instrumental from a description." |
| 2:40–2:55 | Scroll the tool list briefly (sample packs, mastering, synth presets, vocals). | "It also masters mixes, builds sample packs and makes synth presets. It can sing your lyrics in your own voice; the voice must be yours, or used with its owner's consent." |
| 2:55–3:00 | Audial logo. | "Audial, at audialmusic.ai." |

Leave out prices, plans, sign-up offers and upgrade screens: OpenAI's rules forbid them in the
plugin and its listing.

## 8. After approval

- Press Publish.
- OpenAI scans the server every day. Keep the tool list and the hints as submitted. A change
  to a tool's behaviour or description may need another review.
- Record the publish date in `audial-admin/docs/AUDIAL-ASSIST-LAUNCH-TRACKER.md`.
