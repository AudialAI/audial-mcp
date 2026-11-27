# Privacy policy and terms: what the ChatGPT and Claude listings need

For the website agent (W9). The page is `audial-fe-re/src/content/legal/privacy.md`, served at
https://audialmusic.ai/legal/privacy, plus `terms.md` beside it.

Both directories review the policy:
- **OpenAI** wants "the categories of personal data collected, the purposes of use, the
  categories of recipients", "data retention timelines, and any controls offered to your users".
- **Anthropic** wants data collection, usage and storage, third-party sharing, retention and
  contact information (policy 3A). Missing or incomplete policies are refused outright.

This checklist describes what the hosted server (audial-mcp 0.11.0 at
https://mcp.audialmusic.ai) actually does, so the policy can say it accurately. Written
2026-11-27. Zach reads the changes once before they go live; no lawyer is involved.

## Fix first: one sentence is wrong for the hosted server

- [ ] **"The plugins and the MCP server"** says the MCP server runs on your machine and
  "Results are written to your disk". True for the local MCP server, false for the hosted one.
  Split the section:
  - **The local MCP server, the SDK and the plugins:** keep the current text.
  - **Audial in ChatGPT and Claude (the hosted connector at mcp.audialmusic.ai):** a new
    section covering everything below.

## Data categories (what we receive)

- [ ] **Account identity from sign-in.** Connecting ChatGPT or Claude signs the user in to
  their Audial account ("Sign in with Audial"). We store:
  - an access key tied to that account, kept hashed like other API keys;
  - which app it was issued to (ChatGPT or Claude);
  - when it was created and last used.

  The app keeps the key so it can act for the user.
- [ ] **What each tool call contains, and nothing else from the conversation:**
  - the tool chosen;
  - its settings;
  - text the user or the assistant put in it: prompts, lyrics, reference transcripts;
  - the files: an attached file arrives as a temporary download link from OpenAI with its name
    and type; a linked file as its URL.
- [ ] **The audio, MIDI and JSON files themselves,** downloaded by our server from those links.
- [ ] **Hints ChatGPT adds to each call** (locale, a coarse location, anonymous user,
  conversation and organisation ids, a user-agent hint). Our server checks only whether they
  are there, to tell ChatGPT apart from other apps, and does not store them, log them or pass
  them on. Say so.
- [ ] **Results** (stems, masters, MIDI, presets, songs, vocals, analyses, tags, lyrics), as
  for any job.
- [ ] **Logs.** The hosted server writes:
  - the tool name;
  - a short job reference;
  - how long the job took and how many files it made;
  - error messages, which can include a file's name.

  It does not log IP addresses or download links (the request log is off).
  - Most requests come from OpenAI's or Anthropic's servers. Claude Code connects from the
    user's own computer, and a browser downloading a result reaches DigitalOcean directly.
  - The Audial API logs each job as it does for any API call (the existing "Usage and
    technical data" paragraph).

## Purposes

- [ ] Run the job the user asked for and return its results to the app they asked in.
- [ ] Identify the account, and apply its runs and limits as on the website.
- [ ] Keep the service secure and working: abuse prevention, rate limits, debugging.
- [ ] Unchanged and still true, so keep them: no sale of data, no advertising, no training on
  users' audio.

## Recipients (add to "Who processes it for us")

- [ ] **DigitalOcean:** now also hosts the connector. Users' input files are held there only
  while a job runs; results for up to 24 hours (see retention).
- [ ] **OpenAI and Anthropic, as the apps the user chose:** tool results (download links, the
  analysis, tags, lyrics and job details) go back into the user's ChatGPT or Claude
  conversation. OpenAI and Anthropic handle them under their own privacy policies. Link both.
  - This is a disclosure the user directs, not a processor relationship; word it that way.
  - OpenAI is already listed as a processor for tag guessing. Keep that separate.
- [ ] Unchanged for jobs from ChatGPT and Claude: RunPod (processing), Google Cloud and
  Firebase (account, storage), MusicBrainz (tags).

## Retention

- [ ] **Input files on the hosted server:** deleted when the job ends, whether it succeeds or
  fails.
- [ ] **Results on the hosted server:** kept up to **24 hours** behind unguessable links, then
  deleted.
  - They can go sooner: if its storage fills, the oldest results are removed first, and a
    server update or restart clears it.
  - Anyone the user gives a link to can download that file until it expires.
- [ ] **The same results in the user's Audial account:** kept like any other job's, until the
  user deletes them (existing "How long we keep it" text).
- [ ] **Job status on the hosted server:** in memory only, kept up to 24 hours after a job ends
  and lost on restart.
- [ ] **Sign-in check:** our server remembers that a key is valid for up to 5 minutes, in
  memory.
- [ ] **The access key for ChatGPT or Claude:** kept until the user disconnects or revokes it,
  or closes the account.
- [ ] **Hosted-server logs:** kept by DigitalOcean for its log retention period, then deleted.
  The website agent should confirm the current period in DigitalOcean's docs before stating a
  number.

## User controls, including disconnecting

Check each app's menu path before publishing; the apps rename their settings now and then.
- [ ] **Disconnect in ChatGPT:** Settings → Apps (or Connectors) → Audial → Disconnect.
- [ ] **Disconnect in Claude:** Settings → Connectors → Audial → Disconnect.
- [ ] **Revoke from Audial's side:** audialmusic.ai → Profile → Apps & API Keys → revoke the
  "ChatGPT" or "Claude" key. That app then can't use the account. Our server stops accepting
  the key within 5 minutes.
  - The exact label comes from the OAuth work (W1). Match it.
- [ ] **Delete results:** in the dashboard, as today. A download link on the hosted server
  expires within 24 hours; to remove one sooner, email contact@audialmusic.ai.
- [ ] Unchanged: access, correction, export and deletion by email, answered within 30 days;
  EU, UK and California rights.

## Contact

- [ ] Keep "Audial, contact@audialmusic.ai". Both directories need a working contact. The
  support page https://audialmusic.ai/resources/support is the support URL in both listings.

## Terms (`terms.md`)

- [ ] **Voices.** The terms already require "the consent of the person whose voice is used, or
  the voice must be your own" and forbid cloning a voice without consent. Add one sentence:
  the user is responsible for having that right when they use a reference voice, including
  through ChatGPT or Claude (Zach, 2026-11-27).
- [ ] **ChatGPT and Claude are third-party services** under their own terms. Audial is
  responsible for its own tools, not for what the assistant says.
- [ ] **Results reached through ChatGPT or Claude are download links for 24 hours,** and stay in
  the user's Audial account as usual.
- [ ] **Leave the website's pricing paragraph as it is.** It is the website's text, not the
  plugin's, and the ChatGPT listing links to the terms only as a document.

## Keep out of both pages

- Anything that names the music engine or an upstream model: say "the Audial music model".
- Any promise the server doesn't keep. If a later change starts storing more (for example
  ChatGPT's location hints), this checklist and the policy change first.
