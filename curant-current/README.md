# Curant — Mac Setup Guide (Draft)

## Architecture change in this update

Curant's server used to be "the brain" — it held personas, standing
instructions, important people, long-term memory, and each customer's
Anthropic API key, and it made the actual call to Claude.

**That's no longer true.** The server now stores only what's needed to
enforce a subscription: `license_key`, `customer_name`, `plan`, `active`,
`unlocked_addons`. Nothing else. Everything else — the API key, persona,
instructions, important people, memories, recent message history — lives
locally in `~/.curant/` on the customer's own Mac, and `curant-cli` calls
Claude directly from there.

This means:
- Curant's server never sees a message, a persona choice, or a memory.
- If Curant's server is ever breached, there's nothing customer-specific
  to leak beyond billing basics already visible to Stripe anyway.
- `curant-cli` is no longer a "dumb" relay — it now contains the personas,
  the memory-extraction prompt, and the proactivity logic. **Treat it as
  real product IP**, not a throwaway client (see the note in `curant.rb`).
- One less network hop per message (Mac → Claude directly, instead of
  Mac → Curant server → Claude), which should also help latency.

## Pieces in this bundle

- `server/app.py` — license/billing gate only. Verifies a license key is
  active and returns plan/addon info. That's it.
- `server/requirements.txt` — just Flask now; no `anthropic`, no
  `cryptography` (nothing here is a secret worth encrypting anymore).
- `curant-cli` — activation, local storage, persona/memory assembly, and
  the actual Claude calls. This is the real brain now.
- `mac/curant-watcher.py` — watches Messages for new texts/voice memos,
  hands them to `curant-cli`, sends the reply back (text or voice memo).
- `mac/com.curant.watcher.plist` — launchd service for the watcher.
- `mac/com.curant.proactive.plist` — launchd service for the optional
  daily proactive check-in (opt-in per customer).

## Setup order

1. **Start the server** (billing/license only — no encryption key needed anymore):
   ```
   cd server
   pip install -r requirements.txt --break-system-packages
   export CURANT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
   export CURANT_ADMIN_PASSWORD="pick something long and random, not reused anywhere else"
   python app.py
   ```
   Runs on port 5050 by default. For real use this needs a real domain +
   HTTPS — still to be set up. `CURANT_SECRET_KEY` should be set once and
   kept stable (regenerating it logs everyone out); `CURANT_ADMIN_PASSWORD`
   gates the owner page at `/owner` — if it's unset, owner login is
   disabled entirely rather than falling back to anything guessable.
   Customer login lives at `/login`.

2. **Provision a test customer.** No API key involved here anymore —
   that gets set up locally on the customer's own Mac in step 4:
   ```python
   from app import init_db, provision_customer
   init_db()
   key = provision_customer("Your Name", phone_number="+1512...", plan="base")
   print(key)  # this is the license key to activate with
   ```

3. **Install curant-cli** (via Homebrew formula, or run the script
   directly for now) and activate it:
   ```
   curant-cli activate CRT-XXXX-XXXX-XXXX
   ```

4. **Give it your own API key.** Anthropic (Claude) is the default
   provider; the key is stored locally in `~/.curant/config.json`
   (permissions locked to owner-only) and is never sent to Curant's server:
   ```
   curant-cli set-api-key sk-ant-...
   ```
   Prefer OpenAI instead? Switch providers first — persona, instructions,
   and memories carry over unchanged either way, that continuity across
   providers is the actual point (see `PORTABLE_MEMORY_SPEC.md`):
   ```
   curant-cli set-provider openai
   curant-cli set-api-key sk-...
   ```
   Curant won't respond to anything until a key is set for whichever
   provider is active.

5. **Grant Full Disk Access** to Terminal (or wherever you'll run the
   watcher) in System Settings > Privacy & Security — required to read
   `chat.db`.

6. **Edit `curant-watcher.py`** — set `CUSTOMER_APPLE_ID` to the Apple ID
   this Mac's Curant should listen to.

   **Two supported ways to set this up, depending on your hardware:**

   - **Dedicated Mac/Apple ID (recommended if you have the hardware).**
     Curant runs on its own machine, signed into its own Apple ID,
     separate from your personal phone. Real messages you send it from
     your phone are always a genuinely different Apple ID, so they land
     as normal incoming messages — no extra config needed. Set
     `CUSTOMER_APPLE_ID` to your own personal Apple ID (the one Curant
     should reply to).

   - **One Mac, same Apple ID as your phone.** If you're running Curant
     on your everyday Mac instead of a second machine, add a second
     address in **Messages > Settings > iMessage > "You can be reached
     by iMessage at"** (a free email alias on the same Apple ID works
     fine) dedicated purely to texting Curant. Set `CUSTOMER_APPLE_ID`
     to *that dedicated address*, not your primary one, and set
     `"self_message_mode": true` in `~/.curant/config.json`. Without
     this, messages you send from your phone to that address sync to
     the Mac as *outgoing* (iMessage syncs your own sent messages
     across every device on one Apple ID) and Curant will never see
     them as something to reply to — `self_message_mode` is what makes
     that setup work correctly. See `_read_self_message_mode()` in
     `mac/curant-watcher.py` for the full explanation.

7. **Install Whisper for voice memo transcription** (optional):
   ```
   brew install ffmpeg
   pip install openai-whisper --break-system-packages
   ```

8. **Voice replies work out of the box** — `curant-cli set-provider`'s
   `voice_tier` setting picks which TTS tier answers with: `standard`
   (macOS's built-in `say`, free, no setup needed), `natural` (OpenAI —
   needs `curant-cli set-api-key <key> --provider openai`), or `realistic`
   (ElevenLabs — needs `curant-cli set-api-key <key> --provider elevenlabs`).
   Also needs `pip install requests --break-system-packages` if not
   already installed (it is, if August's tools were set up).

9. **Install icalBuddy for calendar/reminders context** (optional, lets
   Curant reference the customer's actual schedule):
   ```
   brew install ical-buddy
   ```
   Grant Calendar and Reminders access when prompted. If skipped, Curant
   just answers without live context — nothing breaks.

10. **Run the watcher** (manually first, to test):
    ```
    python3 mac/curant-watcher.py
    ```
    Text the customer's Apple ID from another device and confirm a reply
    comes back.

11. **Once it works, set it up as background services:**
    ```
    cp mac/curant-watcher.py /usr/local/bin/curant-watcher.py
    cp mac/com.curant.watcher.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.curant.watcher.plist

    # Optional: daily proactive check-in (opt-in per customer)
    cp mac/com.curant.proactive.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.curant.proactive.plist
    ```

## Aaron — the teacher-focused persona, and the persona system reorganized

The persona roster is now organized into two categories
(`PERSONA_CATEGORIES`) rather than one flat list: **general-purpose**
(Curant, Grace, Dean, Nora, Frank, Miles, Jane, Leo — same baseline
skillset, different tone) and **specialist** (August, and now **Aaron**
— personas with real behavioral rules beyond tone, gated to specific
capabilities). Mirrored into Cloud's `app.py` too, including a distinct
ElevenLabs voice ID for Aaron so calls actually sound different from
every other persona, not just the text.

**Aaron is built for teachers**, grounded in real research before any
of this was designed, not guessed at: teachers cite administrative
tasks as their single biggest stressor (ahead of grading and lesson
prep), and already spend $500–900/year of their own money on their
classrooms — both facts shape Aaron's actual behavior, not just his
tone. Aaron proactively surfaces deadlines rather than waiting to be
asked (the admin-burden research point, taken literally), and never
suggests a paid add-on unless the teacher brings it up first (the
out-of-pocket-spending point, taken literally).

### Grading calibration — Aaron's core feature, and the real design decisions in it

A teacher can give Aaron real past assignments — the prompt, the rubric,
and example submissions with the grade/feedback the teacher actually
gave — and Aaron calibrates to that teacher's specific standard before
grading anything new. Three decisions were deliberate, not incidental:

- **Scoped per assignment** (`grading_assignments`, one row per
  assignment name), never one global "how this teacher grades" blob —
  a persuasive-essay rubric doesn't transfer to a lab report.
- **Every suggestion is a labeled draft, always** — enforced in Aaron's
  own system prompt instruction, never phrased as a final grade. This
  is the same "confirm before it's real" posture used everywhere else
  in this product, applied here because grading touches real stakes in
  a way an email or a form submission doesn't.
- **Corrections feed back into calibration** (`grading_corrections`) —
  this is what makes "adapts really well" actually true over time
  rather than static after the first setup.

**A real limitation, found while testing rather than assumed away**:
the spread-check (confirming a teacher's calibration examples span
different quality levels, not all clustered at one grade) started as a
naive exact-string match, which meant "C+" and "C" counted as two
different grades even though they're really the same tier — a
meaningless "spread." Fixed with `_grade_tier()`, which handles the
common letter-grade case correctly (ignoring +/-) but honestly falls
back to exact-string matching for anything else (numeric grades, rubric
labels like "meets expectations") — there's no reliable universal way
to parse every grading system a teacher might use, and the system
prompt language says so rather than overclaiming a guarantee it can't
keep. Tested directly: "C+"/"C" correctly recognized as no real spread,
a genuine "C" vs. "A" correctly recognized as one, numeric grades
correctly falling back to the honest exact-match check.

**Fully tested end-to-end**, not just as isolated functions: a mocked
model calling `suggest_grade` through the real `relay()` tool-calling
loop, correctly pulling in the assignment's rubric/examples/past
corrections, and presenting the result as a draft — confirmed by
checking Aaron's own system prompt actually contains the draft-only
instruction, not just that the code path exists.

**Grading calibration is now ported to Cloud** ✓ — same design (scoped
per assignment, corrections feed back into future calibration, every
suggestion is a labeled draft) with one real addition: everything is
also scoped by `customer_id`, since Cloud serves many teachers from one
shared database rather than Home's one-teacher-per-install model.
Tested directly, including the case that matters most for a multi-
tenant system: two different teachers' grading data confirmed to never
leak into each other's. **A real bug was caught and fixed while wiring
this in**, not hypothetical — the tool dispatcher originally checked
for a Workspace utility email *before* routing to any tool, including
grading tools, which have nothing to do with Workspace at all. A
teacher without a provisioned Workspace account would have gotten a
nonsensical "no utility email" error trying to use grading calibration.
Fixed by dispatching grading tools first, before that check. Full
end-to-end test confirms `suggest_grade` works correctly through
Cloud's real tool-calling loop.

`curant-cli grading-assignments` shows what's set up directly, without
needing to ask through conversation — same "not a silent black box"
principle as `routing-log`, `urgency-log`, and everything else in this
product with an inspectable local record.

## MCP tool support — connecting to whatever the customer already uses

Curant can act as a real Model Context Protocol client. If an MCP server
exists for something (Gmail, Notion, Slack, task managers — a large and
growing open ecosystem), connecting it is configuration, not custom
engineering — either a local command (`curant-cli mcp-add <name>
<command...>`) or a hosted endpoint reached by URL
(`curant-cli mcp-add-http <name> <url> [--header 'Key: Value']`).
Tools from every enabled server become available to whichever provider
is answering, via a real tool-calling loop (implemented for both
Anthropic and OpenAI, since the two APIs' tool-call shapes genuinely
differ). Both transports were tested against genuinely running local
MCP test servers — real subprocess/real network connection, real
protocol handshake, real tool calls — not simulated.

**Performance note, addressed deliberately:** listing an MCP server's
tools means spawning it (or connecting to it, for HTTP servers) and
doing a full handshake, which would make every single reply noticeably
slower if repeated on every message with any servers configured. Each
server's tool list is cached for up to an hour (`mcp_servers.tools_cache`)
rather than re-fetched every time.

**Managing connections:** `curant-cli mcp-list` / `mcp-remove` /
`mcp-toggle <name> on|off` work the same regardless of which transport
a given server uses. `mcp-list` shows an `[OFFICIAL]` or `[UNOFFICIAL]`
badge next to any connected server that matches something in the
curated registry below.

**The curated registry (`KNOWN_MCP_SERVICES`)** — `curant-cli
mcp-connect <name>` for one-command setup of a pre-verified service
(Linear, GitHub, Atlassian/Jira, Figma, HubSpot, Slack, Notion, Sentry,
Square, Descript — all official, vendor-hosted). Two entries are
deliberately different: **Canvas and Schoology are unofficial,
community-built servers**, included at explicit request rather than
excluded on principle. Every entry carries an `"official": True/False`
field, and `mcp-connect` prints the trust label plainly before doing
anything — Canvas and Schoology can't be one-command connected at all
(no OAuth exists for either), so `mcp-connect` instead prints real
setup steps: install the community package yourself, put your own
credentials in *its* config (never Curant's), then `mcp-add` the
resulting binary like any other local server. Schoology's entry carries
a stronger warning than Canvas's — it has no public API at all, so
every existing option works via browser-cookie capture or automated SSO
login, not a sanctioned access method the way Canvas's real API token
is. Read the full description before connecting either.

### When Curant can't do something: call for a quote, not a free feature request

One built-in tool, `log_capability_gap`, is always available regardless
of MCP setup. When a customer asks for something no tool or reasoning
can cover, Curant logs it locally (`curant-cli feature-requests` to
review) and tells them to call in for a custom quote — this is
deliberately NOT a free feature-request pipeline, and deliberately does
NOT touch Curant's server at all. `QUOTE_PHONE_NUMBER` in `curant-cli`
is still a placeholder — needs the real business line filled in before
this is useful to a customer, not just to you reviewing the local log
later.

## August — the specialist creative persona, and what's actually real vs. stubbed

August joined the persona roster (`curant`, `grace`, `dean`, `nora`,
`frank`, `miles`, `jane`, `leo`, `august`) as the one persona that can
*produce* a finished creative artifact, not just think through the idea.
Every persona — August included, without the addon — can brainstorm,
script, or plan a creative project; that's ordinary reasoning, already
covered by the base skillset. What's gated is specifically the ability
to generate a real image, voice clip, or video, and it's gated two ways
at once, both required: **persona must be `august`** (not just the addon
unlocked while talking to a different persona) **and** `"august"` must
be in `unlocked_addons`. Tested directly: wrong persona + addon → no
specialist tools; August + no addon → no specialist tools; August +
addon → all five tools available.

**Honest confidence levels per service — verified against current docs
before writing any code, not guessed at:**
- **ElevenLabs (voice), Ideogram (text-in-image), and FLUX (default
  image generation):** all implemented against confirmed current API
  docs. FLUX specifically was checked a second time against BFL's own
  integration guide and corrected — the first pass had the wrong API
  domain (`api.bfl.ml`, which is actually just the docs site — the real
  API is `api.bfl.ai`) and constructed its own polling endpoint instead
  of using the `polling_url` BFL actually returns in the submit
  response. Fixed and re-verified with a mocked HTTP flow matching BFL's
  real response shape. Still using FLUX1.1 [pro] specifically, not
  FLUX 3 — FLUX 3 remains early-access only as of when this was checked,
  not a stable public endpoint yet, so worth revisiting once it is.
- **Video generation (Veo)** ✓ — resolved, now a real implementation.
  The original plan called for "Veo/Sora." Sora was dropped entirely —
  research turned up that OpenAI is fully discontinuing the Sora API on
  September 24, 2026 (the consumer app already shut down in April), so
  building that integration would've been pointless. Veo's own
  uncertainty (Gemini API vs. the much heavier Vertex AI) is now
  resolved: **Veo is available via the simpler Gemini API**, using the
  real, installable `google-genai` Python SDK — confirmed directly
  against the actual SDK's method signatures before writing any code,
  not guessed at. Targets `veo-3.1-generate-preview`, the current model —
  **Veo 2.0 was deprecated June 30, 2026**, which had already passed by
  the time this was built, so the old model was never a real option.
  Cost estimate updated to Google's own confirmed Veo 3.1 Standard rate
  ($0.40/sec, ~$3.20 for a typical 8-second clip), replacing the earlier
  placeholder $0.
  **Tested with mocks matching the real SDK's actual object shapes**
  (`GenerateVideosOperation`, `.done`, `.response.generated_videos`,
  `client.files.download()`) since no live Gemini API key exists in this
  environment — the plumbing is real and correct, live behavior against
  the actual API is still unverified. Also confirmed this correctly
  flows through the same encryption-at-rest pipeline as every other
  generated file (see "Generated files" below).
  **The blocking-call limitation flagged earlier is now resolved** ✓ —
  video generation runs in a real detached background process instead
  of inside `relay()` itself. `generate_video_veo_async()` creates a
  `background_jobs` row, spawns `curant-cli run-background-job <id>` as
  a detached subprocess (`start_new_session=True`, so it survives after
  `relay()`'s own process exits), and returns immediately with a
  "started, I'll text you when it's ready" message — tested directly:
  `relay()` now completes in ~0.03 seconds for a video-generation
  request instead of blocking for minutes. Delivery happens through
  `curant-watcher.py`'s existing message-polling loop, extended to also
  check for completed jobs each cycle (`pending-background-jobs`) and
  deliver them as a follow-up message, rather than a separate scheduled
  job or duplicated AppleScript-sending logic living in `curant-cli`.
  Tested end-to-end: the background worker actually completing a job
  and recording the result, cost being logged only on real completion
  (not falsely at job creation, and not at all for a failed job), the
  encrypted-at-rest file being correctly decrypted to a short-lived
  plaintext copy for delivery (same pattern as the synchronous path),
  and the failure case still notifying the customer rather than leaving
  them waiting on nothing. Built generically enough (`background_jobs`
  keyed by `tool_name` + JSON arguments) that other slow tools could use
  the same mechanism later without a redesign, though only Veo actually
  uses it right now.
- **Background job hardening** ✓ — two real gaps found and fixed, not
  hypothetical busywork. First: a crashed subprocess (Mac restart
  mid-generation, a killed process) could leave a job stuck in
  `'running'` forever — invisible to the delivery check (which only
  looks at `'done'`/`'failed'`), so the customer would never find out
  and the row would never get cleaned up. `_mark_stale_jobs_failed()`
  catches anything still `'running'` past `STALE_JOB_TIMEOUT_MINUTES`
  (15 — generous margin above Veo's own ~10-minute internal timeout)
  and marks it failed with a clear explanation. Tested directly: a job
  artificially aged to 20 minutes gets caught and marked failed, while
  a genuinely fresh 2-minute-old job is correctly left alone. Second:
  `background_jobs` rows had no retention story at all, unlike every
  other table in this system — `prune_old_background_jobs()` removes
  delivered rows older than 30 days. Tested the case that actually
  matters: an old but **undelivered** job is never pruned regardless of
  age, since deleting it before the customer has seen the result would
  silently erase their only notification that something happened.
- **Descript (video/podcast editing)** ✓ — resolved, but differently
  than every other generation service. Research confirmed Descript has
  a real, official hosted MCP server — `api.descript.com/v2/mcp`,
  OAuth-based — directly from Descript's own current help docs, not
  guessed at. Their docs explicitly distinguish two connection paths: a
  simple "directory connector" that does **not** support media
  generation, and a "custom MCP setup" that does. The custom path is
  exactly what `mcp-add-oauth` (built earlier in this same pass) is
  for — so this doesn't need bespoke integration code the way FLUX/
  Ideogram/ElevenLabs/Veo did. `edit_media_descript()` now just tells
  the customer to run `curant-cli mcp-add-oauth descript
  https://api.descript.com/v2/mcp` if it isn't connected yet; once it
  is, Descript's own tools (project creation, Underlord edits,
  publishing) surface automatically through the existing MCP tool
  aggregation — **tested directly**: registered a server shaped like a
  real Descript MCP connection, confirmed its tools correctly appear
  with the `descript__` prefix through the same plumbing already
  proven against the local test servers, not a new code path.
  **Real, documented limitations worth knowing** (from Descript's own
  docs): no local file export without publishing first — you get a
  signed URL to a web link, not a direct download. YouTube isn't a
  supported import source. Job history only kept 30 days. A connection
  is scoped to a single Descript Drive. **One thing not fully pinned
  down**: whether the custom MCP setup uses this same OAuth flow or a
  static API token — evidence points consistently to OAuth, but worth
  confirming at actual setup time rather than assumed with total
  certainty.

**API keys for these services** use the same local `api_keys` dict as
the conversational LLM providers, just keyed by service name instead —
`curant-cli set-api-key <key> --provider flux` (also: `ideogram`,
`elevenlabs`, `veo`, `descript`). Never sent to Curant's server, same as
every other API key in this system. Veo specifically also needs
`pip install google-genai --break-system-packages` — not bundled into
the base Homebrew install, same pattern as Whisper/requests above,
since it's only needed if video generation is actually being used.

### One-command connect for known, pre-verified job-specific tools

Once each persona had real job-specific skill/tool awareness in its
system prompt (see below), the natural next step was making those
tools actually connectable, not just something Curant can talk about.
Rather than hand-roll custom integrations, checked whether these real
tools have their own MCP servers first — the same principle that turned
Descript from a big build into one command. Confirmed real, official,
OAuth-based MCP servers exist for **Linear**, **Atlassian** (Jira/
Confluence/Bitbucket), **GitHub**, **HubSpot**, **Slack**, **Notion**,
**Sentry**, and **Figma** — essentially Dean's entire stack, plus
pieces relevant to Grace and Nora.

`curant-cli mcp-connect <name>` is a one-command shortcut for services
with an individually-verified URL, so a customer doesn't need to
already know the exact endpoint. **Nine entries are populated**, each
with a literal URL confirmed via multiple independent sources or
extracted directly from the vendor's own page, not guessed — **Linear**,
**Atlassian**, **GitHub**, **Figma**, **HubSpot** (`https://mcp.hubspot.com`),
**Slack** (`https://mcp.slack.com/mcp`), **Notion**
(`https://mcp.notion.com/mcp`), **Sentry** (`https://mcp.sentry.dev/mcp`),
and **Square** (`https://mcp.squareup.com/sse`). Tested directly: the
registry resolves to the exact correct URL for every entry, and an
unrecognized service name gets an honest message rather than a guessed
connection attempt.

**Square required real new capability, not just a registry entry.**
It's a genuine, unexpected find directly relevant to Frank's
hospitality/retail specialization — but it uses SSE transport, the
older remote MCP standard, not the Streamable HTTP every other
confirmed service uses. The existing client only handled `stdio` and
`http` — adding Square honestly meant building real SSE support first
(`_mcp_list_tools_sse_async`, `_mcp_call_tool_sse_async`, using the
SDK's own separate `sse_client` function, confirmed to exist via direct
inspection before writing anything), then threading a `transport`
parameter through `mcp_add_oauth_cmd` and `_mark_mcp_server_oauth` so
Square's connection gets saved and dispatched correctly. Tested
directly: `mcp-connect square` resolves to the SSE transport and the
exact confirmed URL, correctly distinct from every HTTP-transport entry.

**DocuSign was deliberately left out — not from lack of research, but
because the research came back contradictory.** A real official MCP
server exists, but two credible sources disagree on its literal domain
entirely, and DocuSign's own status page marks it "Beta." Two sources
actively disagreeing is a stronger signal of uncertainty than simply
"unverified," and is exactly the situation worth stopping and flagging
rather than picking one arbitrarily. Toast, Lightspeed, MLS systems,
and Follow Up Boss remain genuinely unresearched this pass — worth a
dedicated pass before assuming either way. Shopify's situation is
structurally different from a verification gap: it does have real,
official MCP servers (four of them), but each is hosted on the
individual merchant's own store domain, not a single universal URL —
research pass before assuming either way.

**Clio (Miles's legal domain) was deliberately left out of the
registry** — research found no single official first-party Clio MCP
server; real community-maintained ones exist (e.g. `oktopeak/clio-mcp`)
but carry a different trust profile than a vendor-hosted server, which
matters more for something touching client data. A customer who wants
that can evaluate and connect it manually, but it's not something to
auto-connect with one command on their behalf.

**Delivering a generated file was a real, previously-missing piece —
closed in this pass.** A generated image or audio file needs to actually
reach the customer over iMessage, not just exist in
`~/.curant/generated/`. `relay()` now tracks any file a specialist tool
produced during that turn and surfaces it as `attachment_path` in its
JSON output; `curant-watcher.py` sends it as a follow-up attachment
after the text reply, via a generalized version of the mechanism that
already existed for voice replies (AppleScript's file-send doesn't care
about file type, so this was mostly a matter of exposing it generically
rather than building something new).

## Multi-provider routing — letting different messages go to whichever AI actually suits them

If a customer has API keys set for more than one provider, Curant can
route each incoming message to whichever one actually fits it — instead
of everything always going to whichever provider is "the" default. Three
ways this gets decided, in this order, all handled by one extra
fast-tier model call (skipped entirely if only one provider is
configured, so it adds no cost/latency to the common single-provider case):

1. **A one-off request in the message itself** — "use GPT for this",
   "ask Claude instead" — applies to that message only, nothing is saved.
2. **A standing rule the customer has already set** — "from now on, use
   GPT for anything about code" gets extracted and saved automatically;
   later messages matching that category use it without being asked again.
3. **Falling back to a judgment call** about which configured provider
   generally suits the task better, based on the deciding model's own
   general knowledge of relative model strengths. This is explicitly a
   judgment call, not a verified benchmark — communicated as such in the
   code and never asserted as fact.

**Deliberately not a silent black box:** `curant-cli routing-rules`
shows/removes standing rules directly; `curant-cli routing-log` shows
which provider actually answered recent messages, pulled from stored
history (each reply's provider is saved in `messages.provider`) rather
than the model guessing after the fact. The system prompt also tells the
model which provider is currently answering, so if a customer asks
"which AI is this" mid-conversation, it can answer honestly instead of
guessing or deflecting.

**Fails safe throughout, tested directly:** a broken/unreachable routing
decision, a malformed response, or the deciding model hallucinating a
provider outside the actually-configured set all fall back to the
default provider silently — a routing failure is never allowed to be
the reason a message doesn't get answered.

## What's stored, and where (the complete list)

**Curant's server** — billing/entitlement and the few things that
genuinely need central visibility:
`license_key`, `customer_name`, `phone_number`, `plan`, `active`,
`unlocked_addons`, `device_id` (the one Mac this license is bound to —
enforced both directions: one license per device, one device per
license), `total_messages` (a cumulative COUNT only, reported
periodically — never content, never per-message timestamps),
`error_reports` (enumerated error codes + component only, from a closed
allowlist — never free text or exception messages), `release_requests`
(pending/approved/denied device-release requests — see below). That's
the entire list — there is no backup, no memory, and no other customer
content on the server at all (see below).

**Locally, in `~/.curant/config.json`** (file permissions `600`, owner-only):
`license_key`, `customer_id`, `customer_name`, `device_id`, `plan`,
`unlocked_addons`, `provider` (which model provider is active —
`anthropic` or `openai`), `api_keys` (a dict keyed by provider, so
switching providers doesn't require re-entering a key you've already
set once — legacy single-key installs still work via a fallback),
`persona`, `instructions`, `reply_format`, `voice_tier`,
`proactivity_enabled`, `pending_usage_count` (accumulates locally,
reported and reset on the
periodic status check), and a cached status-check timestamp.
`privacy_ack_version` / `privacy_ack_time` — see the consent gate note
just below; both are local-only, never reported to the server.

### Privacy/ToS acknowledgment gate on first activation (Aug 2026)

Real gap found during a beta-readiness pass, closed group or not:
`activate()` had no consent step at all — a license key went straight
to a bound device with nobody ever having agreed to anything, since
there was no web signup flow to put a checkbox on. `activate()` now
shows a short plain-language summary of `Curant_ToS_Privacy_Draft.md`
(what's local-only vs. what the server sees, the 18+/one-person clause,
and an explicit "this is beta software" note) and requires typing "I
agree" before continuing, in any terminal-based entry point (`curant-cli
activate <key>` directly, or via `setup_wizard_cmd`'s guided flow, since
both call the same `activate()`). The acknowledgment — a version string
tied to the draft doc plus a timestamp — is stored locally only and
never reported to the server. `PRIVACY_ACK_VERSION` gets bumped whenever
the underlying draft changes in a way that materially affects what's
disclosed, which re-prompts anyone who agreed to an older version rather
than silently carrying old consent forward. `CURANT_SKIP_PRIVACY_ACK=1`
exists purely for automated/non-interactive test environments — never
set it for a real customer or beta tester. **Honestly flagged:** this is
still the plain-language draft, not the attorney-reviewed policy — the
gate makes sure someone actually saw and agreed to *something* before
using the product, it doesn't substitute for that review before a real
paid, public launch.

**Locally, in `~/.curant/local.db`** (file permissions `600`, owner-only):
`important_people`, `memories` (no expiry — the intentional long-term
store, also mirrored to a plain-text file for viewing/editing — see
below), `messages` (raw recent history, capped at 20 / expires after 48h,
now also tags each assistant reply with which provider answered it),
`routing_rules` (standing category -> provider preferences, e.g.
"coding -> openai" — no expiry, customer-editable via `curant-cli
routing-rules`), `mcp_servers` (connected MCP servers' launch commands
and a cached tool list per server, refreshed hourly — no credentials for
any third-party service live here, only how to launch its server),
`feature_requests` (local log of capability gaps customers hit —
never sent to Curant's server, purely for your own later review via
`curant-cli feature-requests`).

**Locally, in `~/.curant/generated/`** (not encrypted, not auto-pruned
yet — flagged as an open item below): image and voice files August's
specialist tools produce, before being sent to the customer as an
iMessage attachment.

**Never stored anywhere:** voice memo audio (transcribed in place),
calendar/reminders content (read live, discarded), plaintext API keys on
the server (there is no server-side API key at all now). IP address is
not captured or bound to a license at all — that was tried and then
deliberately removed (see below); IP retention is still whatever the
hosting provider does by default, an open decision, not acted on.

**Backup (`curant-cli backup-now` / `backup-restore`) — fully local, no
server involvement at all:** encrypts settings only — persona,
instructions, reply format, voice tier, proactivity — with a key derived
via **scrypt** (memory-hard, ~256MB/~2s per attempt, chosen specifically
to resist GPU/ASIC brute-force far better than PBKDF2 would) from a
passphrase entered interactively (not a CLI argument — never touches
shell history or the process list), then writes the ciphertext to a
file. Deliberately does NOT include memories or important people —
that's the actual learned content about the customer and real people's
names/relationships, judged too sensitive to put in a file that might
end up on an external drive or cloud folder; a restore brings back
configuration, not what Curant has learned. Defaults to
`~/.curant/backup.curantbackup`, but `--path` can point anywhere — an
external drive, an iCloud Drive or Dropbox folder, etc. Curant's server
never sees this file in any form. Note the real tradeoff, stated plainly
to the customer by the command itself: a backup file that only ever
lives on the same disk as everything else doesn't survive losing that
disk — it only actually protects against Mac loss
if it's copied or synced somewhere else. Deliberately excludes the
API key(s) and raw message history (see "Still open" below on why).

**Portable context export (`curant-cli context-export` / `context-show`)
— separate feature from backup, on purpose:** builds a plain-language,
human-readable text block (persona style, standing instructions,
preferences, memories, important people) meant to be decrypted and
pasted into a *different* AI as context. Unlike backup, this DOES
include memories and important people — carrying that content is the
entire point of this feature, so excluding it the way backup does would
make it useless. Encrypted the same way as backup (scrypt + Fernet,
interactive passphrase) and written to
`~/.curant/context.curantcontext` by default (or `--path`). Two
deliberate safety properties, tested: the plaintext template never
touches disk — `context-show` decrypts straight to the terminal only,
so you copy it from there — and the template never contains the actual
proprietary persona system-prompt wording (`PERSONAS`), only a short
plain-language gloss (`PERSONA_STYLE_SUMMARY`) written specifically so
this export can't be used to extract Curant's real prompt engineering.
It also, obviously, contains no license key or API key — nothing in it
grants access to anything.

**Locally, in `~/.curant/memories.md`** (file permissions `600`,
owner-only, plain text, never encrypted): a directly human-readable
mirror of the `memories` table — one line per memory, in the customer's
own words, kept current automatically every time a memory is added or
removed (including the automatic extraction that runs after every
exchange). This is the answer to "what does Curant actually know about
me" for anyone who'd rather read a file than run a command or open the
local dashboard. Local-only in the same sense as everything else
memory-related: never sent to Curant's server, never part of
context-export/full-export's encrypted output, nothing in the code path
that writes or reads it touches the network. Three ways to interact
with it:
- `curant-cli memories` — print everything Curant remembers, refreshing
  the file at the same time.
- `curant-cli edit-memories` — opens the file in `$EDITOR` (or `nano` if
  unset), then applies whatever you changed the moment you close it:
  add a `- ` line to teach Curant something new, delete a line to make
  it forget, rewrite a line to correct it.
- `curant-cli sync-memories` — same apply step, for when you edited the
  file some other way (Finder, another editor, synced it from an iPad,
  etc.) and just need the changes picked up.

Editing is a plain diff against what's already stored (by exact
content), so touching one line doesn't disturb any other memory's
age/timestamp.

## Personalization phase — a scripted "get to know me" Q&A

Texting Curant something like *"learn about me"*, *"get to know me"*, or
*"personalize yourself to me"* starts a short, five-question local Q&A
(name, age/life stage, occupation, location, and an open-ended
day-to-day question) — one question per reply, answered over as many
texts as it takes. Each answer is saved straight into `memories` (and
so immediately shows up in `memories.md`, see above) the moment it
comes in, not batched at the end. Say "skip"/"pass"/"n/a" to leave any
one question out, or "stop"/"cancel" to abort the whole thing — either
way nothing already answered is lost. Deliberately not an LLM call at
any point (trigger detection is a keyword/regex match, the questions
are fixed text) — same reasoning as the triviality short-circuit:
scripted and instant beats a network round trip for something this
simple, and it means the flow works even if the model/API key isn't
configured yet. See `PERSONALIZATION_QUESTIONS` /
`continue_personalization_session` in `curant-cli`. Text-only — skipped
during FaceTime calls, since five sequential questions read aloud one
at a time isn't a good live-call experience.

## Onboarding, capability discovery, and urgency handling

**Onboarding:** `relay()` detects when a message is the very first one a
customer has ever sent (empty local history) and adds a one-time
instruction telling the model to introduce itself naturally — persona
name, genuine sense of what it's good for — rather than a canned
feature list. Tested: fires exactly once, not on every subsequent message.

**Capability discovery:** a permanent system prompt instruction (not
just first-message) means asking "what can you do" any time gets a
real, specific answer reflecting actual breadth across the skill
categories, not a vague "I can help with anything."

**Urgency handling:** `classify_urgency()` is a deliberately lightweight,
free heuristic — no extra paid API call per message, unlike the routing
decision. It flags a message as `urgent` based on explicit keywords
("urgent", "ASAP", "emergency", etc.), excessive exclamation points, or
heavy ALL-CAPS, and is honest about what it is: a tone/pacing signal for
response speed, never a safety or crisis triage mechanism — that
judgment always stays with the model itself, on every message,
regardless of this tag. Flagged messages get one extra instruction
("respond quickly, skip small talk") in that turn's system prompt only.
Tested across keyword, punctuation, and ALL-CAPS cases, plus the edge
case of short messages not false-positiving on caps. Stored per-message
(`messages.urgency`) and inspectable via `curant-cli urgency-log` —
nothing here is a silent internal signal.

## Browser automation — the piece Google Workspace explicitly didn't solve

The utility Gmail account solves *receiving* a verification email. It
never solved *completing* a signup on some third-party website's own
UI — typing into fields, clicking submit. That's a genuinely separate
capability, built here with Playwright, gated the same way as August's
other specialist tools (persona must be `august` AND the addon must be
unlocked).

**This is the single most consequential capability in this whole
system** — an AI autonomously interacting with arbitrary third-party
websites, potentially agreeing to terms of service or submitting real
data. Handled with the same read/write split already used for Gmail/
Calendar/Drive, plus one extra hard rail specific to this capability:

- **`browse_page`** — read-only, no confirmation needed. Loads a page,
  returns its visible text and every fillable field (name, type,
  label). Look before filling anything in.
- **`fill_and_submit_form`** — the real external-effect action. Gated
  like `gmail_send`/`drive_share`: only fires after explicit customer
  confirmation, per the system prompt instruction. **Also hard-blocked
  in code, not just by instruction**, from ever touching a field that
  looks like a payment or sensitive-ID field (card numbers, CVV, SSN,
  routing/account numbers) — that refusal applies regardless of
  confirmation, because payment/identity data is a categorically
  different risk than a newsletter signup, and "the model should ask
  first" isn't a strong enough guarantee for that category on its own.

**A real bug the testing process caught, worth naming directly:** the
first version of the sensitive-field detector missed `cc_number`
(underscore-separated) because it did a plain substring check against
`ccnum`, and the underscore breaks that match. Fixed by normalizing
separators out of both the field name and the keyword list before
comparing, then re-tested against 22 real-world naming conventions
(`card-number`, `card_number`, `cardNumber`, `cc_number`, `CVV`,
`social_security_number`, `routing-number`, etc.) — all correctly
caught — plus 11 legitimate fields (`name`, `email`, `occupation`,
etc.) confirmed to NOT be false-flagged. A safety rail that only works
for one naming convention isn't really a safety rail.

**Tested against a real, genuinely running browser, not simulated:**
Playwright installs a real Chromium binary in this environment
(confirmed launchable before writing any of this) and a real local
test webpage with an actual signup form (text field, email field,
select dropdown, checkbox) was built to test against. Confirmed: real
page load with correct text/field extraction, a real fill-in-every-
field-type-and-submit flow with the real resulting confirmation text
captured correctly, and a full pass through the actual `relay()` tool-
calling loop — not just isolated function calls.

**Setup**: `pip install playwright --break-system-packages && playwright install chromium`
— not bundled into the base Homebrew install, same pattern as Whisper/
`google-genai`, since it's a large download only needed if this
capability is actually used.

### Form submission is now async-hybrid, not blocking

`fill_and_submit_form` used to run directly inside `relay()`, the same
blocking-call problem Veo originally had — a slow site would hold up
the whole watcher. Now routed through `fill_and_submit_form_hybrid`,
which spawns the actual work as a detached background job (same
mechanism as Veo) and polls it for up to 8 seconds: most submissions
finish within that window and get answered in the same reply — the
common case stays a normal, single-message exchange. A genuinely slow
site instead gets "still working, I'll let you know," and the result
follows later once it's actually done, via the same watcher polling
loop that delivers Veo's videos.

**The property that actually matters here, and was tested directly,
not just assumed:** the form only ever gets submitted **once**,
regardless of which path answers the customer. `relay()` never calls
`fill_and_submit_form()` itself — only the background job does, exactly
once — so the fast path and the slow path are just two different ways
of *reporting* the same single execution, never two separate attempts.
Confirmed with a real timing test: a submission engineered to finish
quickly gets a same-turn answer and is spawned exactly once; a
submission engineered to run past the wait window gets the "still
working" message, is *also* spawned exactly once, and correctly shows
up for delivery only once the underlying work actually completes —
never before, never twice.

**Resolved:** this used to inherit August's gate by default, purely
because it was built alongside August's tools — not because browsing/
filling a form is actually a creative capability. Moved to its own
dedicated `browser_automation` addon, independent of which persona is
active. Tested across all combinations: a non-August persona (Grace,
Dean) with the addon unlocked now genuinely gets these tools, including
a full pass through the real `relay()` tool-calling loop with a non-
August persona actually calling `browse_page`; August with only the
`"august"` addon (no `browser_automation`) now correctly does NOT get
browser tools, a real behavior change from before, confirmed
deliberately rather than assumed; August's own generation tools remain
unaffected by any of this.

## Preferences moved to conversation, and a firm terminal-only boundary (Aug 2026)

Before this pass, `set_notification_preferences` was the only actual
preference change a customer could make by just asking in a text or
call — everything else (persona, VIP contacts, auto-reply contacts,
quiet hours, travel timezone, a custom ElevenLabs voice id, the
generation spend cap, the capability-gap quote phone number) required
the CLI or the web dashboard, even though none of it is more
security-sensitive than what `set_notification_preferences` already
allowed. Ten new builtin tools close that gap, same pattern as the
existing one: `set_persona`, `set_spend_cap`, `set_quote_phone`
(available to everyone), and `set_vip_status`,
`manage_auto_reply_contact`, `set_quiet_hours`, `set_travel_mode`,
`set_elevenlabs_voice` (Grace-exclusive, gated the same way
`search_order_status` already was — the tool simply isn't in the list
`get_all_available_tools` returns for anyone else). Each one validates
input the same way its CLI equivalent did and returns a plain string
result instead of `print()`+`sys.exit()` — calling the original
`*_cmd` functions directly from a tool handler would have killed the
whole `curant-cli` process on invalid input mid-conversation, since
`sys.exit()` doesn't stop at the tool call, it stops the program.
Manually verified all ten against a real config file, not just
`py_compile` — every valid input, every invalid/refused input, and the
Grace-tier gate itself, checked against the actual JSON written to
`~/.curant/config.json` after each call.

**Deliberately NOT moved to conversation**, and stated as a real,
structural rule in every persona's own system prompt now (not just
documented here): adding or replacing an AI provider API key,
switching AI provider, activating or re-activating a license, changing
`customer_apple_id`/handles (who Curant even trusts as "the customer"
in the first place), and adding or removing a delegate. **Enforcement
here is by omission, not by asking the model nicely** — there is
simply no tool in `get_all_available_tools` for any of these, the same
"code-level enforcement over prompt-only enforcement" principle this
codebase already applies to the `confirmed: true` gate elsewhere. The
system prompt addition is there so the model doesn't just say "I can't
do that" unhelpfully, or worse, hallucinate that it succeeded — it's
told to name the exact terminal command instead, borrowing the same
framing Dean already uses for shell safety mode (a real capability
that still requires an explicit, once-per-conversation choice before
Curant touches it) and applying it to account-level settings instead.

## Local front-door layer — a small local model sitting in front of Claude

A small local model (via [Ollama](https://ollama.com), running on the
customer's own Mac) handles a few narrow, low-stakes jobs before
anything reaches Claude's API. Claude stays the actual reasoning brain
for everything that matters — this layer never replaces it, only sits
in front of it.

**What it does:**

- **PII redaction** — SSNs and credit card numbers (Luhn-validated to
  cut false positives) get masked out of the content sent to Claude,
  so the raw value never leaves the device even in transit. Local
  storage keeps the original, unredacted text — this protects what
  crosses the network, not what's visible on the customer's own
  machine. Deliberately covers only what can be detected with real
  confidence — bank routing/account numbers are NOT covered, since a
  bare 9-digit number has no reliable structural signature and a noisy
  pattern would erode trust faster than it protects anything.
- **Triviality short-circuit** — a quick "thanks" or "ok" gets a
  local-generated reply instead of a full Claude call. Deliberately
  conservative: any failure to classify (Ollama not running, an
  ambiguous result) defaults to NOT trivial, so this can only ever
  save a call, never silently downgrade a real request.
- **Offline fallback** — if Claude's API is unreachable, this tries a
  degraded local-model reply (explicitly flagged to the customer as
  reduced-capability) instead of a hard failure with no answer at all.

**Honest dependency, stated plainly:** this requires Ollama installed
and running locally — it is NOT bundled with `curant-cli`, and nothing
about this is a hard requirement. Every function in this layer
(`is_local_model_available`, `_call_ollama`, and everything built on
them) fails safe — returns `None`/`False` — if Ollama isn't running,
rather than raising. A customer who never installs Ollama gets exactly
the same behavior Curant had before this layer existed.

**Setup, if you want it:**
```
brew install ollama
ollama pull qwen3:8b
ollama serve
```
Model name and host are both overridable via environment variables
(`CURANT_LOCAL_MODEL`, `CURANT_OLLAMA_HOST`) if a different local model
or a non-default Ollama port is preferred.

**What's NOT built as part of this:** local answering of substantive
questions (only genuinely trivial small talk gets a local-only reply —
anything with real content still goes to Claude), and a proper
regression-tested classification prompt (the current triviality
classifier is a single prompt, not yet validated against a real set of
trivial-vs-substantive examples).

## What's Still Missing (be aware before demoing)

- **`QUOTE_PHONE_NUMBER` in `curant-cli` is still a placeholder.** The
  call-for-a-quote message tells customers to call a number that doesn't
  exist yet — needs the real business line before this feature is
  useful to anyone but you reviewing the local log.
- **Veo and Descript integrations** ✓ — resolved, this claim was stale.
  Veo is a real, working implementation (submit/poll/download via the
  `google-genai` SDK, targeting the current `veo-3.1-generate-preview`
  model, BYOK). Descript is resolved differently — not a bespoke
  integration at all, but a deliberate redirect to Descript's own
  official hosted MCP server (`curant-cli mcp-add-oauth descript
  https://api.descript.com/v2/mcp`), since duplicating a REST
  integration Descript already exposes via MCP would be needless work.
  See the docstrings on `_generate_video_veo_sync` and
  `edit_media_descript` for the full verification notes and documented
  real limitations (Descript's 30-day job history, no direct file
  export, single-Drive scoping).
- **`~/.curant/generated/` encryption + pruning** ✓ — resolved. Files
  are encrypted at rest (Fernet, dedicated key stored in config,
  generated once) and pruned after 24 hours — long enough to get
  delivered, no reason to persist after. Delivery decrypts to a
  short-lived plaintext temp file only at the moment of actually
  handing off to Messages; `curant-watcher.py` deletes that temp copy
  immediately after sending. Tested: raw content genuinely absent from
  the at-rest file, decryption returns exact original bytes, old files
  get pruned while fresh ones don't, and a full `relay()` generation
  produces both a correctly-encrypted at-rest copy and a correctly-
  decrypted deliverable copy at the same time.
- **MCP OAuth support** ✓ — now genuinely proven end-to-end, not just
  independently-tested pieces. `curant-cli mcp-add-oauth <name> <url>`
  runs a real interactive flow using `OAuthClientProvider` (the actual
  SDK class) and `CurantTokenStorage` (a real implementation of the
  SDK's `TokenStorage` interface). Getting the full live proof took real
  debugging, worth recording honestly rather than glossing over:
  - Building a spec-compliant test authorization server required
    reading the SDK's own discovery URL-building functions directly
    (not guessing) to know exactly which endpoints it would call — RFC
    8414 authorization server metadata, RFC 9728 protected resource
    metadata (for the resource server and auth server being properly
    separate services, the realistic shape), RFC 7591 dynamic client
    registration, and PKCE.
  - First attempt failed: the test's own auth-checking middleware was
    blocking the metadata *discovery* requests themselves, which must
    be reachable without auth — a real bug in the test harness, fixed.
  - Second attempt failed: the test's Playwright-driven browser
    simulation hung using Playwright's sync API from inside
    `redirect_handler`, an `async def` called from within the SDK's own
    running event loop — sync Playwright can't be used there. Fixed by
    switching to the async Playwright API.
  - Third attempt still hung: `callback_handler` (mirroring curant-cli's
    real production code) does a blocking `thread.join()` inside an
    `async def`, which blocks the whole event loop thread — fine in
    real use (the human's actual browser is a separate OS process,
    unaffected), but it meant an asyncio task scheduled on that same
    loop never got a chance to run. Fixed by running the Playwright
    simulation on a genuinely independent OS thread instead of an
    asyncio task sharing the loop.
  - **With all three real bugs fixed, the complete flow now works**:
    real dynamic client registration, a real PKCE-secured authorization
    URL, Playwright genuinely clicking "Approve" on the real login page,
    the real local callback server catching the real redirect, real
    PKCE verification on the token exchange (the test server actually
    validates `code_verifier` against `code_challenge`), a real access
    token issued and correctly persisted via `CurantTokenStorage` — and
    critically, **a subsequent authenticated MCP tool call
    (`get_secret_data`) succeeded against a server that rejects anything
    without a valid Bearer token**, proving the token actually flows
    through to real use, not just that one gets issued.
  Simple API-key auth (`mcp-add-http --header`) remains separately
  tested end-to-end, unaffected by any of this.
- **Remote/HTTP-based MCP servers** ✓ — resolved. Some real services
  (Ideogram, for instance) offer a hosted MCP endpoint reached by URL
  rather than a local launch command; `curant-cli mcp-add-http <name>
  <url> [--header 'Key: Value']` connects to these using the `mcp` SDK's
  real streamable-http client (the modern standard remote transport,
  confirmed against the current SDK before writing any code — not
  guessed at). Tested against a genuinely running local HTTP MCP server,
  not simulated: real tool discovery over real HTTP, real tool calls
  with real arguments and real results, custom headers correctly passed
  through and stored. Confirmed no regression to the existing stdio
  transport, which still works exactly as before — both are just
  different transports dispatched from the same server config now.
- **No real hosting for the server yet** — localhost only, until
  deployed with a real domain and HTTPS.
- **No Stripe integration** — `provision_customer()` is still a manual
  call, needs wiring to a real payment webhook. It now also needs a
  `phone_number` at signup, and Stripe should probably remain the source
  of truth for contact info rather than duplicating updates into both places.
- **TTS is now real, three tiers** ✓ — `text_to_speech()` in the watcher
  implements all three voice tiers: `standard` (macOS's built-in `say`,
  free and local, no API key needed — the right default so voice replies
  work out of the box), `natural` (OpenAI TTS, `gpt-4o-mini-tts`,
  verified against current OpenAI docs before implementing), and
  `realistic` (ElevenLabs, same endpoint already used for August's voice
  generation). Reads API keys from `curant-cli`'s own `~/.curant/config.json`
  rather than duplicating key storage. Tested: each tier dispatches
  correctly, a missing key raises a clear actionable error rather than a
  cryptic one, and an unknown/typo'd tier value falls back to the free
  `standard` tier instead of crashing. Paid tiers now also log estimated
  cost to the same `generation_costs` table August's tools use — every
  `natural`/`realistic` reply costs real money on every message (unlike
  August's occasional generations), which was invisible before this pass.
  Tested: `standard` correctly logs nothing (it's free), both paid tiers
  log correctly, and a customer asking "how much have I spent" gets one
  unified total across TTS and August's tools, not two hidden numbers.
- **chat.db schema assumptions** — verify against your macOS version.
- **No FaceTime/live-calling piece.**
- **License key delivery** — needs to be automatic post-payment.
- **IP binding was implemented, then removed.** For a while, a license
  was also bound 1:1 to the IP it activated from, in addition to device
  binding. Testing confirmed the real cost of that: even the correct,
  already-bound Mac got rejected the moment its public IP changed (a
  router restart alone was enough), and CGNAT could make two unrelated
  customers on the same ISP collide. That enforcement — and the
  `ip_address` column, `bind_ip()`, and the related error codes — has
  been fully removed. Device binding (MAC-based) is unaffected and still
  enforced on its own.
- **Device release now has a real, logged flow — deliberately not
  self-serve — with both a CLI and a web UI path.**
  `curant-cli request-release` (or the "Request device release" button
  on the customer's `/dashboard`) logs a request (`release_requests`
  table) instead of releasing anything automatically. Nothing changes
  until an admin logs into `/owner` and clicks Approve — the button
  calls `approve_release_request(request_id)` under the hood, and only
  that action ever clears `device_id`. `list_pending_release_requests()`
  (surfaced on the owner dashboard) shows what's waiting, including the
  customer's name and phone number.
  Tested: resubmitting doesn't create duplicates, denial leaves the
  binding untouched and allows a fresh request later, and a device stays
  blocked right up until the moment of approval — there's no path that
  releases a binding without a human deciding to.
- **Device binding now uses a real hardware MAC address**, not a
  software-generated UUID. This closes the earlier-flagged gap: the old
  approach stored a UUID in `~/.curant/config.json`, so copying that one
  file to a different machine would carry the "device identity" along
  with it. The MAC address is read live from `en0` (the Mac's built-in
  Wi-Fi interface) each time it's needed, never cached as the source of
  truth — deleting or copying the config file no longer affects it.
  Honest limitation, not hidden: a MAC address can be manually changed
  via `ifconfig` with admin privileges, so this isn't an unspoofable
  hardware root of trust, but it's a meaningfully higher bar than before
  (needs a deliberate command with elevated privileges, not just copying
  a file). Falls back to Python's `uuid.getnode()` if `en0` isn't found
  (e.g. Ethernet-only Macs, or non-macOS dev/test environments).
- **Backup moved from opt-in server storage to fully local files.**
  Originally this uploaded an encrypted blob to Curant's server (still
  only ciphertext, opt-in) so it would survive losing the Mac entirely.
  That's now removed — `backup-now`/`backup-restore` read/write a local
  file only, defaulting to the same disk. This is genuinely simpler and
  keeps the server's storage list shorter, but the tradeoff is real and
  stated to the customer directly in the command's own output: a backup
  that never leaves the Mac doesn't protect against losing the Mac. It's
  on the customer to point `--path` somewhere that's actually backed up
  elsewhere if they want real protection against that scenario.
- **Backup passphrase is now prompted interactively (`getpass`)**, not
  passed as a CLI argument — closes a real leak where the old
  `backup-now <passphrase>` form put the passphrase in shell history and
  briefly in the process list, visible to anyone else with access to the
  same machine or its history files.
- **Backup scope narrowed to settings only, and the key derivation
  upgraded to scrypt.** Backups originally included memories and
  important people; that's been removed — a restore now brings back
  persona/instructions/preferences only, not what Curant has learned
  about the customer or the real people in their life. This is a real
  capability loss on restore, done deliberately because that content is
  meaningfully more sensitive than a settings choice. Separately, the
  key derivation moved from PBKDF2 to scrypt (memory-hard, ~256MB and
  ~2 seconds per attempt at the chosen parameters) specifically to
  resist GPU/ASIC-accelerated brute-force far better than PBKDF2, whose
  cost is pure CPU time and parallelizes cheaply on dedicated hardware.
- **API key(s) are deliberately excluded from backup.** Losing the Mac
  still means re-entering whichever provider's key(s) were set, by
  hand, even after restoring a backup — treated keys as too sensitive
  to include even in an encrypted local file. Reasonable default, but
  worth confirming that's the tradeoff you want rather than something
  decided by default.
- **IP address logging is still an open decision**, not yet acted on —
  whatever your hosting provider does by default is what happens today.
  Worth a deliberate choice (scrub vs. leave default) once real hosting
  is set up.
- **Rate limiter is in-memory** — fine for a single server process.
- **Memory extraction has no dedup beyond exact string match.**
- **`curant.rb`'s license/IP framing changed** — see the note at the
  bottom of that file. The client is no longer safe to publish freely
  as-is if the tap repo is meant to be public.
