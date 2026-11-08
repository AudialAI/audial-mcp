# Audial in ChatGPT

Backlog item R-7. Written 2026-11-07, with OpenAI's documentation as it stood that day.

**Where this leaves us.** ChatGPT can only call a remote MCP server, and it signs users in
with OAuth only. It cannot send an API key. audial-mcp 0.8.0 adds the remote server
(`audial-mcp --http`), built the way ChatGPT needs it. Two things are still missing before
ChatGPT users can call Audial:

1. **An OAuth sign-in on the Audial API.** This is the critical path.
2. **A hosted instance at a permanent URL.**

Then comes OpenAI's review. The recommendation is in section 6.

OpenAI renamed "apps" to "plugins" in July 2026: the Apps SDK docs moved to
`developers.openai.com/plugins`, and the App Directory became the Plugin Directory. What a
server has to do did not change.

## 1. What OpenAI requires

### Transport
- **Streamable HTTP at a stable public HTTPS URL**, usually ending in `/mcp`. A tunnel or a
  local endpoint isn't accepted for submission.
  [build/mcp-server](https://developers.openai.com/plugins/build/mcp-server)
- **The origin is locked once published.** Changing it means a new plugin.
  [deploy/app-review](https://developers.openai.com/plugins/deploy/app-review)
- **Stateless servers work.** OpenAI's own quickstart is stateless.
  [build/app-quickstart](https://developers.openai.com/plugins/build/app-quickstart)
- **Streaming must not be buffered** by the load balancer or CDN.
  [deploy/troubleshooting](https://developers.openai.com/plugins/deploy/troubleshooting)
- **Tool calls time out after about 60 s to 2 min, and progress notifications don't extend
  that.** OpenAI's docs give no number. The figure comes from OpenAI support on the
  community forum, which is not a primary source:
  [forum thread](https://community.openai.com/t/call-remote-mcp-server-tool-timed-out-resulting-in-error-424/1364167).
  OpenAI's Secure MCP Tunnel documents a 120 s deadline that progress doesn't reset:
  [secure-mcp-tunnels](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
  Most Audial jobs take longer than that, so long jobs have to return before they finish.

### Sign-in
Source: [build/auth](https://developers.openai.com/plugins/build/auth) unless noted.

- **Supported modes:** no auth, OAuth 2.1 (authorization code with PKCE `S256`), or a mix
  per tool. ChatGPT "cannot present custom API keys", and public submission rejects static
  server headers. [deploy/submission](https://developers.openai.com/plugins/deploy/submission)
- **Discovery:**
  - Protected-resource metadata (RFC 9728) at `/.well-known/oauth-protected-resource`.
  - Authorization-server metadata (RFC 8414, or OpenID discovery), whose `issuer` must match
    exactly.
  - A `401` with `WWW-Authenticate: Bearer resource_metadata="…"` starts the sign-in.
- **Client registration, three options:**
  - Client ID metadata documents (CIMD), which OpenAI prefers.
    `client_id_metadata_document_supported: true`; ChatGPT's client id is
    `https://chatgpt.com/oauth/client.json`.
  - Dynamic client registration (`registration_endpoint`).
  - A predefined client.
- **Redirect URI:** `https://chatgpt.com/connector_platform_oauth_redirect` when the server
  returns `iss` in every authorization response and advertises
  `authorization_response_iss_parameter_supported: true`. Otherwise it is a
  per-connection `https://chatgpt.com/connector/oauth/{callback_id}`.
- **Audience:** ChatGPT sends `resource=` on both the authorization and the token request,
  and the token should carry it as its audience.
- **Per-tool auth policy** goes in `securitySchemes`; `_meta.securitySchemes` is the
  mirror for clients that only read `_meta`.
  [reference](https://developers.openai.com/plugins/reference)
- **Refresh tokens:** the help center recommends them, otherwise users may have to sign in
  again.
  [help: custom MCP servers](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)

### Tools
Sources: [reference](https://developers.openai.com/plugins/reference) and
[plugin-guidelines](https://developers.openai.com/plugins/plugin-guidelines).

- **Annotations:** `readOnlyHint`, `destructiveHint` and `openWorldHint` are required, as
  explicit booleans; `idempotentHint` is optional. A tool that starts a job is not
  read-only.
- **Output:** declare an `outputSchema` for any tool that returns `structuredContent`, and
  the content must match it.
- **Server instructions** are used and reviewed. Put the important part in the first 512
  characters.
- **Files the user attaches** reach a tool as objects, for each argument listed in
  `_meta["openai/fileParams"]`:
  - The object is `{download_url, file_id, mime_type, file_name}`.
  - Its schema must declare all four properties, with `download_url` and `file_id`
    required.
  - The link is temporary. OpenAI states no size or type limits.
- **A UI widget is optional.** A plugin can be tools only. A widget could add an audio
  player later. [concepts/ui-guidelines](https://developers.openai.com/plugins/concepts/ui-guidelines)

### Listing and review
Sources: [deploy/submission](https://developers.openai.com/plugins/deploy/submission),
[deploy/app-review](https://developers.openai.com/plugins/deploy/app-review) and
[plugin-guidelines](https://developers.openai.com/plugins/plugin-guidelines).

- **Who can submit:** a verified individual or organization, as org owner or with
  Apps Management Write.
- **The package:** a ZIP (plugin manifest plus MCP config) around one remote MCP server,
  uploaded at platform.openai.com.
- **Steps:**
  1. Domain verification: the server's origin serves a token at
     `/.well-known/openai-apps-challenge`.
  2. Scan Tools.
  3. Review details and attestations.
  4. An approval email, then Publish.
- **What the listing needs:**
  - A name and a subtitle of at most 30 characters each.
  - A description of at most 4000 characters, and a category.
  - Website, support, privacy policy and terms URLs.
  - A square logo.
  - Exactly 5 positive and 3 negative test cases.
  - A video walkthrough and release notes.
  - A reviewer account that works at once, with no MFA, email codes or magic links.
- **The privacy policy** must cover data categories, purposes, recipients, retention and
  user controls.
- **Countries:** set as an allowlist.
- **After publishing,** OpenAI scans the server daily.
- **Money:**
  - Plugins "may conduct commerce only for physical goods".
  - Users "may sign in to an existing paid account and access features already included in
    their subscription".
  - Plugins "must not display subscription plans, initiate new subscriptions, or promote
    upgrades", and must not link to a checkout.
  - Descriptions must not advertise pricing, subscriptions or trials.
  - Trial or demo plugins are refused, and a plugin must not be a worse version of the
    website.

### Plans
- **Directory plugins** are available "across ChatGPT plans", subject to plan, workspace,
  role and region. There is no per-plan table.
  [help: plugins](https://help.openai.com/en/articles/20001256-plugins-in-chatgpt-and-codex)
- **Custom MCP connections** (adding a server by URL before it is listed) have full MCP
  support on Business, Enterprise and Edu, on the web. Pro gets read/fetch only. Free, Go
  and Plus aren't mentioned.
  [help: custom MCP servers](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
- **So most consumers will reach Audial only through a published listing**, and testing
  the write tools before submission needs a Business workspace.

### Content
OpenAI's [usage policies](https://openai.com/policies/usage-policies/) prohibit:
- using someone's voice "without their consent in ways that could confuse authenticity";
- infringing intellectual property;
- impersonation.

Nothing is specific to music generation, stems or remixes. text2vox, which sings in a
reference voice, is the tool a reviewer is most likely to ask about.

## 2. Gap analysis

| Requirement | audial-mcp 0.7.0 | 0.8.0 (this PR) | Still needed |
|---|---|---|---|
| Remote streamable HTTP | stdio only | `--http`, stateless, `/mcp` | hosting |
| Per-user credentials | env var or browser sign-in saved on the machine | bearer Audial key per request, checked against the API | OAuth on the API |
| OAuth discovery | none | RFC 9728 metadata and 401 challenge with `--authorization-server`; `securitySchemes` on every tool | the authorization server itself |
| Files in | local paths | `openai/fileParams` objects or https links, downloaded with SSRF and size checks; local paths refused | — |
| Files out | written to the user's disk | download links, 24 h | optional: signed URLs from the API |
| Jobs longer than a tool call | the call waits until done | waits 40 s, then returns a `job_id` for `check_job` | — |
| Annotations | `destructiveHint` unset | all three hints explicit | — |
| Upsell-free refusal text | "Subscribe at audialmusic.ai…" | neutral wording in remote mode | — |
| Domain verification | none | `AUDIAL_MCP_DOMAIN_CHALLENGE` serves the token | the token, at submission |
| Widget | none | none, which is allowed | optional audio player |

## 3. What this PR adds

**The remote server.** `audial-mcp --http [--host --port --public-url --authorization-server]`.
The stdio server is unchanged: running `audial-mcp` with no options behaves exactly as it did.

- **One account per request.**
  - The bearer token must be an `aud_` Audial key. The API's `/auth/whoami` checks it, and
    the result is cached for 5 minutes.
  - The SDK's credential lookups are pointed at the request being served
    (`remote.install_sdk_credentials`). They never fall back to the host's own key.
  - A test runs two accounts' jobs at the same moment and checks that neither sees the
    other's key.
- **File inputs.**
  - A ChatGPT file object, or a plain https link.
  - Only https, only public addresses, every redirect re-checked, at most 100 MB.
  - Downloaded into a private folder for the job, which is deleted when the job ends.
- **Results.**
  - Served at `<public url>/files/<random 32-character folder>/<name>` for 24 h.
  - Server paths never appear in results or errors.
- **Long jobs.** A call waits up to `AUDIAL_MCP_WAIT_S` (40 s). It returns the result if the
  job finished, or `status: running` with a `job_id`. `check_job` waits again. Only the
  account that started a job can check it.
- **The remote tool list.**
  - Every tool except `list_results`, `sign_in` and `sign_out`, plus `check_job`.
  - File arguments are file-object schemas, listed in `openai/fileParams`.
  - The instructions explain links and job ids, and ask for audio and reference voices the
    user has rights to.
- **ChatGPT-ready wording.** The subscription refusal says what the account's plan covers,
  without urging a purchase.
- **Annotations.** `destructiveHint` is now explicit on every tool, in both modes: `true`
  only for `sign_out`.
- **Version.** 0.8.0, not published.

**Known limits.**
- Jobs and results live in one process and its work folder, so run a single instance.
- A restart forgets running jobs; their results stay downloadable until they expire.
- Each result file is downloaded from Audial and then served again: two transfers per file.

## 4. What's left

### 4.1 OAuth on the Audial API (blocking, about 1–2 days, Claude can build it)
The API already has most of the parts:
- **Device sign-in** (`src/lib/services/device-auth.ts`, RFC 8628). A pending request in
  Firestore, an approval page on the website (`/activate`) for the signed-in user, and an
  API key minted on approval with `createApiKey`, replacing that client's earlier key.
- **`aud_` API keys,** which identify their owner, are revocable, and are listed under
  Apps & API Keys.

OAuth needs the same flow with a browser redirect instead of polling:
1. **Authorization-server metadata** at the API root:
   `/.well-known/oauth-authorization-server`, with `issuer` `https://api.audialmusic.ai`.
   - Endpoints: `authorization_endpoint` (a website page) and `token_endpoint` (the API).
   - `code_challenge_methods_supported: ["S256"]`, `response_types_supported: ["code"]`,
     `grant_types_supported: ["authorization_code"]`,
     `token_endpoint_auth_methods_supported: ["none"]`.
   - `client_id_metadata_document_supported: true` and
     `authorization_response_iss_parameter_supported: true`.

   The API's routes live under `/api`, so this route goes outside that mount. Alternatively,
   use issuer `…/api` with the RFC 8414 path form.
2. **A consent page on the website** (`/oauth/authorize`), like `/activate`. It checks the
   signed-in user, the client and the redirect URI, then shows "ChatGPT wants to use your
   Audial account" with Approve and Deny buttons.
   - Clients: accept CIMD client ids from an allowlist (`https://chatgpt.com/oauth/…`). That
     avoids open dynamic registration.
   - On approval, store a one-time code in Firestore: hashed, valid for 10 minutes, bound to
     the user, the client, the redirect URI, the PKCE challenge and `resource`. Then redirect
     with `code`, `state` and `iss`.
3. **`POST /api/auth/oauth/token`** (a public route). It checks the code, the PKCE verifier,
   the redirect URI and the client. It then mints an `aud_` key ("ChatGPT", a new `oauth`
   kind, client `chatgpt`) and returns it as the `access_token`.
   - Store `resource` on the key and return it from `/auth/whoami`. The MCP server can then
     check the audience (`validate_token_resource`).
   - The first version can omit expiry and refresh tokens: a key lasts until the user
     revokes it in their profile. Expiring keys with refresh tokens can come later.

After that, start the MCP server with `--authorization-server https://api.audialmusic.ai`.
Nothing else in audial-mcp has to change: an OAuth token is an Audial key.

### 4.2 Hosting (Zach decides)
- **What it needs:**
  - A permanent HTTPS origin, for example `mcp.audialmusic.ai` (it can't change once
    published).
  - Responses streamed without buffering, and requests allowed to run about 60 s. Calls
    return within about 45 s.
  - One always-on instance, so no scale-to-zero.
  - A work folder big enough for 24 h of results.
- **The container:** the repo's Dockerfile, run as `audial-mcp --http --host 0.0.0.0
  --public-url https://mcp.audialmusic.ai`, with `AUDIAL_MCP_AUTHORIZATION_SERVER` set.
  `AUDIAL_MCP_DOMAIN_CHALLENGE` is set at submission time. Health check: `GET /healthz`.
- **Cost:** a basic CPU instance on the platform the API already uses, about $10–25 a month.
  - Storage: a stem split is a few tens of MB, so tens of jobs a day is a few GB, rolling.
  - Bandwidth: twice each result's size.
  - The real cost is GPU time per run, the same as on the website, and the subscription
    gate and free allowance apply unchanged.
- **Later:** if the API returned short-lived signed download URLs, the server could pass
  those through and store nothing.

### 4.3 Submission (Zach, with Claude drafting)
See section 5.

### 4.4 Follow-ups (not blocking)
- **SDK:** `stem_split`, `master` and `generate_midi` look the user id up without the key
  they were given. The remote server works around this; fixing it in the SDK would make the
  workaround unnecessary.
- **`analyze` is marked read-only,** but it uploads the file and records an execution.
  OpenAI counts starting a job as a write, so a reviewer may ask for `readOnlyHint: false`.
- **An audio-player widget** for results: optional, and good for the listing.
- **More than one instance** would need the job board and results in shared storage.

## 5. What Zach must do

1. **Decide on hosting** (section 4.2) and add a DNS record for the chosen host name.
2. **Approve building OAuth** on the API and website (section 4.1).
3. **Verify an identity on platform.openai.com:** yourself (published under your name) or
   the company (published as Audial). Use an account that is org owner.
4. **Create the reviewer account:**
   - email and password, no MFA or email codes;
   - an active subscription at no charge;
   - a few sample audio files that you own.
5. **Update the privacy policy and terms:**
   - audio and text arriving from ChatGPT;
   - files held on the MCP server for up to 24 h;
   - Audial's processors;
   - how to disconnect (Apps & API Keys).
6. **Prepare the listing:**
   - name "Audial", subtitle, category, logo (`bundle/icon.png`);
   - a description without pricing or subscription talk;
   - website, support, privacy policy and terms URLs;
   - 5 positive and 3 negative test cases, and release notes (Claude can draft these);
   - a video walkthrough, which you record and Claude can script.
7. **Choose the countries** to list in.
8. **Submit:**
   1. Paste the domain token into `AUDIAL_MCP_DOMAIN_CHALLENGE`.
   2. Run Scan Tools.
   3. Accept the attestations.
   4. Press Publish after approval.
9. **Optional:** a ChatGPT Business workspace, to try the write tools as a custom
   connection before review.

## 6. Recommendation

Go ahead, in this order:
1. Merge this PR. Nothing has to be released until there is a host.
2. Build OAuth on the API. It is the critical path, and Claude's own custom connectors use
   the same OAuth flow, so it pays off twice.
3. Host one instance at `mcp.audialmusic.ai` and check it end to end with an OAuth client.
4. Submit.

ChatGPT already sends most of Audial's visitors. A listing would let it hand people Audial
at the moment it recommends a tool, instead of sending them to the website. Two risks:
- **The review timeline is unknown.**
- **The money rules forbid upselling inside ChatGPT.** New users get the free allowance and
  have to subscribe on the website. That fits the rule that users "sign in to an existing
  paid account", but it does not sell subscriptions inside ChatGPT.
