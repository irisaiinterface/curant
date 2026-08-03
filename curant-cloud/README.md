# Curant Cloud

A hosted AI Secretary reachable by plain SMS and voice call — no app to install,
no Mac Mini required. Customers get a local phone number and just text it.

This is separate from Curant Home (Mac Mini, iMessage, fully local). The same
Curant brain runs both — the only difference is the channel and where memory/data
lives.

## How it works

```
Customer texts a number
    → Telnyx receives it
    → Telnyx sends a webhook to this server
    → Server runs the Curant brain (LLM call, memory, persona)
    → Server replies via Telnyx SMS
```

Voice calls follow the same path via Vapi (prototype) → Telnyx Voice AI (production).

## Who this is for

- **Customers who don't want to buy a Mac Mini** but want a personal AI Secretary
- **Non-Apple users** (iMessage only works on Apple devices — this uses plain SMS)
- **Customers who don't care about full local privacy** and prefer zero setup
- Anyone who wants to try Curant without any hardware

## Self-hosting (Mac, Windows, Linux) — for privacy-conscious customers

If a customer wants the Cloud features (SMS, no hardware) but doesn't want to rely
on your hosted version, they can run this themselves. Requires:
- Docker Desktop (Mac/Windows) or Docker Engine (Linux) — that's the only dependency
- A Telnyx account (~$1/mo for a phone number)
- An Anthropic or OpenAI API key

```bash
# 1. Copy env template and fill in your values
cp .env.example .env
# Edit .env — at minimum: CLOUD_SECRET_KEY, CLOUD_ENCRYPTION_KEY,
#             CLOUD_ADMIN_PASSWORD, TELNYX_API_KEY, TELNYX_WEBHOOK_SECRET

# 2. Start
cd docker
docker compose up -d

# 3. Point Telnyx at your server
# Set your Telnyx messaging webhook to: http://your-ip:5051/webhooks/sms
# (Use ngrok or Cloudflare Tunnel for a public URL during local testing)

# 4. Sign up at http://localhost:5051/cloud/signup
```

### Windows-specific note
Docker Desktop for Windows uses WSL2 internally — there's nothing Windows-specific
in this code, it runs identically. Install Docker Desktop from docker.com, enable
WSL2 backend, and follow the same steps above.

### Linux-specific note
Install Docker Engine (not Docker Desktop) via your distro's package manager
or https://docs.docker.com/engine/install/. `docker compose` is included.
No other system dependencies.

## Hosted deployment (you run this on a VPS for customers)

Any Linux VPS works. Tested on Ubuntu 24.04.

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh

# Clone/copy the curant-cloud directory to the VPS
# Fill in .env

# Run
cd docker
docker compose up -d

# Put nginx in front for HTTPS (strongly recommended before real customers)
# Point your domain at the VPS, get a Let's Encrypt cert, proxy to port 5051
# Then set CLOUD_HTTPS=true in .env and restart
```

## Phone numbers

You provision numbers via Telnyx. Each customer gets a number matched to their
requested area code, folded into their subscription price — no separate charge.

On cancellation, the number is released **immediately** and the routing entry is
deleted at the same moment — a reassigned number can never route to a stale account.

## API key storage — customer's choice

Customers choose at signup:

**Option A — We store it (simpler)**
Key is encrypted server-side with Fernet (scrypt-derived key from `CLOUD_ENCRYPTION_KEY`).
Curant answers every message immediately, including proactive check-ins.

**Option B — You hold it (more private)**
Key is encrypted in the browser using Web Crypto (PBKDF2 + AES-GCM, 310,000 iterations).
Server stores only the ciphertext and can never decrypt it. Customer taps a link once
per session to unlock. Proactive check-ins require an active session.

Both options use HTTPS in transit. Neither sends the plaintext key to our servers
in Option B — ever.

## Google Workspace utility email — for account signups, not customer correspondence

Alongside the phone number (for talking to the customer), each customer
can also get a dedicated Google Workspace account — an inbox Curant uses
for itself, specifically for account-creation flows: receiving a
verification email or confirmation code when Curant signs the customer
up for something online.

**Scope, stated plainly:** this solves *receiving* a verification email.
It does **not** by itself solve *filling out a signup form* on some
arbitrary third-party website — that's the separate browser automation
capability (see below), which now exists on Cloud too. Having the inbox
is half of "make accounts on websites"; browser automation is the other
half — together they cover the full flow.

**Why Workspace and not just a plain Gmail account:** creating raw
consumer Gmail accounts programmatically at scale is exactly the
automated-account-creation pattern Google's abuse detection aggressively
flags and bans. Workspace accounts, created through proper admin
provisioning under a domain you administer, are the sanctioned way to
have many email identities under real business control.

**Getting an actual Workspace domain to provision under — the realistic
path, checked directly rather than assumed:** Google's Reseller Program
has two paths. Applying directly to Google requires **100+ already-
provisioned seats, a credit check, and a signed contract** — not
available to a new product with zero customers yet, a real chicken-and-
egg problem. The practical route: become a customer of an **authorized
distributor** (Vendasta, Ingram Micro, or a smaller reseller) who
already holds Google's authorization — real quotes seen as low as
$2.50–3/user/month at volume vs. $7–8.40/user/month buying direct from
Google. Apply to Google directly once past the 100-seat threshold
yourself, for better wholesale terms at that point.

**Cost impact — this changes the numbers from earlier in this doc.**
Phone number (~$1–2/mo) + Vapi voice (variable) + now a Workspace seat
(~$3–8/mo depending on direct vs. distributor pricing) raises the fixed
cost base from the original ~$2–3/mo estimate to more like **$5–10/mo
per customer** before any voice usage. Feed this into whatever Cloud
price point gets set.

**What's actually implemented, verified against real Google docs before
writing any code (same discipline as the FLUX/Vapi work):**
- `provision_workspace_account()` — creates a real Workspace user via
  the official Admin SDK Directory API (`users.insert`), confirmed
  against Google's current developer docs.
- `deprovision_workspace_account()` — deletes it, called on
  cancellation with the same immediacy principle as releasing the
  Telnyx number.
- `check_signup_inbox()` — reads recent messages via the Gmail API,
  delegated to that specific customer's account. Fails safe (empty
  list) on any error — a broken inbox check degrades gracefully rather
  than crashing the conversation.
- Both provisioning and deprovisioning are wired into signup/cancellation
  as **non-blocking, best-effort steps** — tested directly: a customer's
  signup still succeeds and they still get a working phone number even
  when Workspace isn't configured at all. This isn't a corner cut, it's
  deliberate — the phone number is what actually matters for the
  product to function; the utility email is an enhancement.

**What's real and now fully wired up:** Curant has **complete control
over the whole Workspace suite assigned to it** — not just Gmail,
everything the utility account includes at no extra per-action cost
beyond the seat itself: **Calendar** (create/list/delete events, attach
a Meet link), **Drive** (upload/list/delete/share files), **Docs**
(create/read/append), **Sheets** (create/read/write ranges), **Tasks**
(create/list/complete/delete), and **Contacts** (create/list). 25 tools
total, all verified against current Google API docs before writing any
code, all tested — both individually (each dispatches to the right
function) and end-to-end (a mocked model actually calling
`calendar_list_events` through the real tool-calling loop, getting real
data back, and answering correctly).

**The confirmation rule, extended consistently rather than only
covering Gmail:** three actions across this whole suite have a genuine
external effect on another person, and all three require explicit
customer confirmation first, same standing rule as everywhere else in
this product: **sending an email** (`gmail_send`), **creating or
deleting a calendar event with attendees** (they get a real invite or
cancellation notice), and **sharing a Drive file** (they get real
access and a notification email). Everything else — reading, searching,
creating your own docs/sheets/tasks/contacts, listing, labeling,
trashing — is the customer's own private data with no effect on anyone
else, and runs freely.

**Browser automation** (`browse_page`, `fill_and_submit_form`) — filling
out a signup form or clicking through a flow on some arbitrary
third-party website's own UI, not just a Curant-controlled API. Ported
from Home, with the same hard rails: `fill_and_submit_form` requires
`confirmed: true`, enforced in code, and refuses to touch anything that
looks like a payment or sensitive-ID field regardless of confirmation.
Gated on its own `browser_automation` addon, independent of persona,
same as Home.

**Architectural note specific to Cloud:** Home has a persistent local
watcher process that can poll a background job indefinitely. Cloud is a
stateless webhook handler, so there's no equivalent process to poll —
instead, a slow form submission runs in a background thread started
exactly once, and if it's not done within a short synchronous window,
the customer gets a "still working" reply now and a follow-up SMS once
it actually finishes, rather than the result being silently dropped.

## Voice — Vapi prototype

Set `VAPI_API_KEY` in .env and configure Vapi to point incoming calls at:
`https://yourdomain.com/webhooks/vapi`

Vapi handles the call (transcription, TTS), and calls this server for the Curant
brain's response. This is the prototype path — production voice will migrate to
Telnyx's native Voice AI Agents (lower latency, one vendor for both SMS and voice).

**Verified against Vapi's current docs, not just built on assumption:**
`assistant-request` is confirmed as the real webhook message type, and Vapi enforces
a **hard 7.5-second end-to-end response deadline** on it (fixed, not configurable —
the telephony layer caps at 15s, Vapi reserves half of that for call setup). Tested
this server's actual response time: ~3ms, since it only does local DB reads, no
network or LLM call — comfortably within budget. If this ever gets slower (e.g. a
future version calls out over the network before responding), that budget needs to
be actively protected, not assumed.

**Real bug caught and fixed during this verification pass:** the system prompt sent
to voice calls was reusing the SMS prompt verbatim, including the literal instruction
"You are communicating via SMS" — during an actual phone call. Fixed by making
`build_system_prompt()` channel-aware (`channel="voice"` vs `"sms"`), tested that
voice calls now get spoken-audio-appropriate instructions and SMS is unaffected.

**Not independently re-verified this pass:** the exact JSON response shape
(`{"assistant": {...}}`) and `"end-of-call-report"` as the literal type string for
the post-call event. Both match Vapi's documented naming conventions from prior
research, but — same standard applied to the FLUX/Veo work — worth confirming
against a real test call before trusting this in front of a customer.

**The voice/key-choice gap flagged previously is now resolved** ✓ —
SMS respected each customer's Option A/B key choice, but Vapi calls
never did: every call used whatever key was configured in Vapi's own
dashboard, shared across every customer. Confirmed via research this is
a genuine structural constraint — Vapi's "bring your own key" system is
account-level dashboard configuration, not something passable per-call
— not a bug fixable with a small patch. The real fix: Vapi supports a
**Custom LLM provider mode**, letting us host the actual LLM-calling
endpoint instead of Vapi calling Anthropic directly. Since Curant's own
server controls the URL Vapi is told to call, the customer's ID gets
baked directly into that URL (`/vapi-llm/<customer_id>`) at
`assistant-request` time — solving the routing problem without any
extra metadata mechanism. `CLOUD_PUBLIC_URL` needs to be set to this
server's real public URL for Vapi to reach it.

**Tested directly, not just structurally plausible:** a customer's own
stored key was confirmed to be the exact key actually used for the
call (mocked the LLM call and asserted on which key arrived, not just
that *a* key arrived), the OpenAI-shaped request Vapi's Custom LLM
provider sends gets correctly split into Anthropic's separate
system-prompt/messages shape, and the response comes back correctly
formatted as an OpenAI chat completion. Both honest failure modes were
tested too: an Option B customer with no active unlocked session gets
a clear 401 with an explanatory message (same real limitation the SMS
unlock flow already has, not silently pretended away), and an unknown
customer ID gets a clean 404 rather than a crash. An unrecognized
caller (no customer match at all) still falls back to Vapi's own
account-level key, so *some* response is possible rather than nothing.

**Real token-by-token streaming is now built** ✓ — resolved, not left as
an open limitation. Research on Vapi's own support forum surfaced real
production friction even with seemingly-correct non-streaming
responses (Vapi's docs say both formats work, but multiple real support
threads show the assistant staying silent with non-streaming JSON that
looked correct), and Vapi's own official example repos ship SSE as the
primary recommended pattern — worth taking seriously rather than
trusting the docs alone. `call_llm_streaming()` yields real text
chunks: Anthropic via `client.messages.stream()`, with the exact
event-filtering logic (`chunk.type == "content_block_delta"` and
`chunk.delta.type == "text_delta"`) copied directly from the SDK's own
internal implementation (`MessageStream.__stream_text__`), not
guessed — the public `text_stream` convenience property isn't exposed
as a plain attribute in the installed SDK version, so the underlying
mechanism was read directly rather than assumed. OpenAI via
`stream=True` and `chunk.choices[0].delta.content`, long-stable API
surface.

The endpoint honors Vapi's actual `stream` request field (confirmed via
research this is what Vapi really sends), returning real SSE when
requested and falling back to the original complete-response JSON
otherwise — nothing regressed. **Tested thoroughly**: correct
`text/event-stream` content type, the correct customer-specific key
still used in streaming mode (not just non-streaming), each SSE chunk
using the real `delta` shape confirmed from an actual solved case on
Vapi's support forum (not the `message` shape used in non-streaming
completions), chunks arriving in the right order with the right
content, the final chunk correctly signaling `finish_reason: "stop"`
with an empty delta, proper `[DONE]` stream termination, the
non-streaming fallback still working both when `stream` is absent and
when explicitly `false`, and — the case that matters most for
reliability — a failure mid-generation still delivers whatever content
was already produced rather than silently dropping it, while clearly
communicating that something went wrong.

**Persona voices were generic before this pass — mapped per persona below.**
Every call used to sound identical (one hardcoded ElevenLabs voice ID
regardless of which persona the customer had set).

## What's stored on the Cloud server

Per customer, in `curant_cloud.db`:
- `customers`: name, email, phone number, plan, persona, instructions, key mode,
  encrypted API key (if Option A) or browser ciphertext (if Option B),
  workspace_email/workspace_user_id (the utility email for account signups,
  if Workspace provisioning is configured — null otherwise, never blocks signup)
- `memories`: what Curant has learned about the customer (same 20/48h retention as Home)
- `messages`: recent conversation history, now tagged with urgency ('urgent'/'normal')
- `routing_rules`: standing provider-per-task preferences
- `important_people`: named relationships
- `phone_routing`: phone number → customer mapping (active flag zeroed immediately on cancel)

**Unlike Curant Home**, the Cloud server holds all of a customer's data — not their
device. This makes you a data controller. The privacy policy needs to cover:
- Encryption at rest (the DB file itself — not yet implemented, flagged below)
- Isolation between customers (each row is keyed by customer_id)
- Retention and deletion on cancel (implemented: active=0, routing archived)

## Onboarding, capability discovery, and urgency handling

Same mechanisms as Curant Home, mirrored here: `generate_reply()` detects
a customer's first-ever message (empty history) and adds a one-time
onboarding instruction; a permanent instruction means "what can you do"
always gets a real answer; `classify_urgency()` is the same free,
keyword/punctuation-based heuristic (not a paid API call, not a safety
triage mechanism — purely response tone/pacing). All tested end-to-end:
onboarding fires once and only once, urgency is correctly scoped per-
message and persisted (`messages.urgency`), and the welcome SMS sent at
signup now actually names real capabilities instead of a generic
"text me anything" greeting.

## What's still missing before production

- **Rate limiting** ✓ — resolved. Was in-memory (a dict), which only ever
  rate-limited within a single process — meaningless once this runs
  behind multiple gunicorn workers, since each worker would silently
  get its own separate count and the real limit would be multiplied by
  however many workers exist. Now backed by the same shared SQLite
  database every worker already connects to. Tested with actual
  separate OS processes (not threads) making real rate-limit checks
  against a shared limit — confirmed the limit holds across processes,
  not just within one.
- **A2P 10DLC registration** — required for US business SMS via Telnyx. Without it,
  messages may be filtered as spam. Register at telnyx.com before going live.
  This has a multi-week approval delay — start it now.
- **Stripe integration** — billing is manual right now. `provision_phone_number()`
  is the right place to hook in a Stripe payment check.
- **CLOUD_HTTPS** — set to "true" and put a real TLS terminator (nginx + Let's Encrypt)
  in front before any real customers use this. Cookie security flags depend on it.
- **Vapi → Telnyx Voice AI migration** — no trigger point defined yet. Run Vapi
  until the call experience is validated, then migrate.
- **Pricing** — agreed Cloud should run higher than Home to cover hosting costs.
  No number set yet.
- **Privacy policy** — a draft exists at `PRIVACY_POLICY.md` covering Cloud's
  hosted data model accurately. Needs lawyer review before publishing, especially
  for GDPR/CCPA compliance language, retention period confirmation, and
  jurisdiction-specific requirements.
- **DB encryption at rest** ✓ — implemented with SQLCipher (AES-256). Set
  `CLOUD_DB_KEY` in your `.env` before storing any real customer data.
  See `.env.example` for the generate command.
- **Browser automation job tracking** — the in-memory `_browser_jobs` dict
  (and `_session_keys` for Option B unlock, same underlying pattern) only
  works correctly on a single process. Fine for now; if this ever runs
  behind multiple gunicorn workers, the same fix already applied to rate
  limiting (move to the shared SQLite database, or Redis) needs to happen
  here too — a form submission started on one worker won't be visible to
  a poll that lands on a different worker.
