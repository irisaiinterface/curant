# Curant

**An AI personal secretary, reachable by phone call or text — not another app to open.**

Curant is staffed, not installed. Every customer gets their own AI secretary — a consistent persona with memory, judgment, and real tool access — reachable the way you'd reach an actual assistant: you call, or you text. There's no chat window to remember to open, no app icon competing for attention. Claude (Anthropic) is the reasoning core behind every persona, chosen deliberately for consistency of judgment across every interaction rather than swapped between cheaper models.

This document is the top-level orientation — what the product is, what it currently does and doesn't do, and how the repo is organized. `curant-current/README.md` and `curant-cloud/README.md` go much deeper on their respective codebases.

---

## The Product

Curant ships in two tiers, aimed at different customers:

| | **Curant Home** | **Curant Cloud** |
|---|---|---|
| **Where it runs** | The customer's own Mac (or Windows/Linux via Docker) | Hosted — no hardware required |
| **Reachable by** | iMessage, FaceTime Audio, voice memos | Plain SMS and voice call (Telnyx) |
| **Data locality** | Never leaves the customer's device | Held on Curant's servers, encrypted at rest |
| **AI cost model** | BYOK — customer's own Anthropic/OpenAI key, called directly from their machine | BYOK — customer's own key, either server-encrypted or held entirely in their browser (their choice) |
| **Setup** | Homebrew install + guided onboarding | Sign up, pay, text the number you're given |

Both tiers run the same ten personas, the same core skillset, and the same behavioral guardrails — the difference is entirely in deployment and data locality, not capability.

### The ten personas

Curant (generalist), Grace (executive ops), Dean (software dev), Nora (HR/coaching), Frank (retail/hospitality), Miles (legal/finance), Jane (real estate), Leo (healthcare admin), August (creative/generation specialist), Aaron (education/teaching). Each is grounded in Big Five psychology research (Conscientiousness + Emotional Stability as a universal baseline — the two traits research actually ties to good service performance) with an MBTI-style type layered on for tone differentiation, explicitly documented as a design language, not a clinical claim. Full research and rationale: `reference-documents/Curant_Persona_Psychology_Redesign.docx`.

### Why this is a different category, not a better chatbot

Most AI products today assume you'll open an app and type. Curant doesn't compete on chat-interface polish — it's positioned against a completely different alternative: hiring a human assistant. Phone-native access, persona-as-hire identity (the same "person" every time, not a model that resets), and literal data-locality options (not just a privacy policy promise) are the actual differentiators, not model quality or feature count. Full positioning analysis: `reference-documents/Curant_Summary.docx`.

---

## Business Model

- **Base:** $29/mo. **Executive:** $149/mo (all-inclusive — every persona, every add-on). Add-ons priced individually so customers only pay for what they use.
- **AI inference is a $0 cost to the business at every tier.** Every AI call — the core persona reasoning, and August's image/voice/video generation — is bring-your-own-key. This is the single fact that makes Cloud's margins what they are: **~76% on Base, ~85% on Executive**, since the business's actual per-customer cost is just infrastructure (Telnyx number, SMS/voice usage, Workspace seat, Stripe processing), not compute.
- **Billing is real and live**, not a manual process: Stripe Checkout for subscriptions, a Stripe-hosted Customer Portal for self-service plan changes/cancellation, webhook-driven subscription state (the webhook, not the browser redirect, is the source of truth — closes an obvious "skip payment by visiting the success URL" gap).
- Full unit economics, itemized: `reference-documents/CostCalculator.jsx` (interactive) and `reference-documents/Curant_Summary.docx`.

---

## Fundraising Materials

Two separate documents, for two separate instruments — kept deliberately distinct rather than conflated, since they're regulated differently and promise backers/investors different things:

| | **Kickstarter** | **Equity Investors** |
|---|---|---|
| **Document** | `reference-documents/Kickstarter_Campaign_Copy.md` | `reference-documents/Investor_Pitch_Materials.md` |
| **What it is** | Reward-based, non-dilutive campaign copy — pledge, get a discount/perk in return | Pitch deck content — market sizing, unit economics, the case for an equity stake |
| **Funds** | Cloud's specific launch-readiness costs (legal/compliance, not inference or dev) | Not yet defined — no equity ask has actually been finalized (see below) |
| **Status** | Drafted, not yet published — reward tiers finalized, visuals/video and the founder's own risk-section voice still needed | Content drafted; explicitly missing a formed legal entity/cap table confirmation, a real founder bio, and actual round terms (a lawyer's job, not a draft) |

**Not to be mixed in messaging:** the Kickstarter campaign specifically avoids "invest" language (a wording choice, not a mechanic problem — see the note in that doc) since Kickstarter is reward-based crowdfunding, a different regulatory regime than equity. If both are ever promoted together, keep the asks — and the audiences — visibly separate.

---

## What Currently Works

Everything below is real, tested code — not scaffolding or planning documents. The verification discipline behind this claim: every feature was checked either against real infrastructure (a locally-run browser, a real OAuth handshake, a real Gmail/Calendar/Drive integration, a real Telnyx/Vapi call flow) or against mocks shaped to match the real SDK objects, with that boundary stated honestly wherever full live verification wasn't possible — never assumed from memory or documentation alone.

**Both tiers:**
- All ten personas, with real job-specific tool knowledge per domain (verified against current vendor documentation, not assumed — e.g. Clio for legal, Epic/athenahealth for healthcare admin, MLS/Follow Up Boss for real estate)
- Persistent memory with real near-duplicate detection (not just exact-string matching)
- **MCP tool connections** — a curated registry of pre-verified official servers (Linear, GitHub, Atlassian/Jira+Confluence+Bitbucket, Figma, HubSpot, Postman, Slack, Notion, Sentry, Square, Descript), plus TestRail as a Home-only community stdio server (no official TestRail MCP server exists as of Aug 2026). **Note on Bitbucket:** its tools on the shared Atlassian server currently authenticate via API token only — OAuth is on Atlassian's own roadmap but not live yet, unlike Jira/Confluence. Home connects via a CLI command with a local OAuth flow; Cloud connects via the dashboard with a real, standards-based OAuth flow (metadata discovery + Dynamic Client Registration + PKCE) split across two web requests, since there's no "customer's own machine" to run a local callback server on. **Honestly flagged:** Cloud's OAuth flow hasn't been tested against a real provider yet — no live app is registered with any vendor and no browser exists in the build environment to complete an interactive login; confirm against a real account before trusting it with a real customer.
- **All third-party OAuth tokens are now encrypted at rest** (Aug 2026 fix) — MCP connector tokens and the new Microsoft Graph tokens (see below) were being stored in plaintext, the same class of gap already fixed once before for customer Anthropic API keys. Same Fernet encryption now applied consistently across all three.
- Browser automation — fills and submits real web forms, with `confirmed: true` enforced **in code**, not just requested by prompt, and a hard, code-level block on anything that looks like a payment or sensitive-ID field, regardless of confirmation
- August's generation tools — FLUX, Ideogram, ElevenLabs, Veo — all real, working integrations, gated by a per-customer monthly spend cap that's checked *before* any paid call fires, not just logged after
- Aaron's grading calibration system (rubric + example-based, spread-checking, correction feedback loop)
- Every consequential action (sending an email, sharing a file, deleting a calendar event with attendees) requires explicit prior confirmation — enforced structurally, so a future model version change literally cannot regress this behavior
- An explicit escalation instruction: every persona is told to defer to the customer on genuinely high-stakes or uncertain decisions rather than answer confidently regardless — Miles and Leo additionally have hard-stated domain boundaries (no legal/financial or clinical judgment, ever)
- A regression test suite (`tests/`) that imports each codebase's real prompt-building logic directly and checks it against a candidate model version before rollout — not yet run against a live key, see "What's Not Done Yet"

**Home specifically:**
- Fully local — Claude is called directly from the customer's Mac; nothing about a conversation touches Curant's servers except a minimal license/billing check
- A local front-door layer (small model via Ollama, optional, fails safe if not installed): PII redaction before anything reaches Claude's API, a triviality short-circuit that skips a full API call for simple acknowledgments, and a degraded offline fallback if connectivity drops
- Local encrypted backup and a portable, model-agnostic context export format (CPMF) — a customer's accumulated context is explicitly designed to be *their* asset, not a vendor lock-in mechanic
- **Dean's shell access (Aug 2026)** — real shell command execution, deliberately scoped to Home only and Dean only (Cloud's multi-tenant architecture already treats arbitrary command execution as unsafe, the same reasoning that keeps stdio MCP servers off Cloud). Three safety modes, always the customer's own explicit choice and never assumed by Dean: confirm-every-time (default), a pre-approved read-only allowlist, or fully autonomous.
  **Corrected after a security audit the same day it was written:** an earlier version of this bullet described the working directory as a sandbox. It is not one, and saying so was wrong. In `confirm` and `autonomous` mode a command runs through a real shell and can reach any path on the machine — what limits blast radius there is the customer approving the exact command, or having explicitly accepted no guardrails. Only `allowlist` mode is genuinely constrained, and only after the audit: the original prefix matcher was bypassable (`git log --oneline; <anything>` matched and ran with no confirmation), file-reading commands were on the allowlist despite arguments being able to name any absolute path, and the code carried a comment asserting a protection that testing disproved. It now rejects shell metacharacters, rejects absolute paths and traversal in arguments, executes without shell interpretation at all, and no longer allowlists `cat`/`head`/`tail`. Re-verified against the full original exploit set. **Real OS-level containment (`sandbox-exec` or a container) remains a genuine open gap** — see "What's Not Done Yet".

**Cloud specifically:**
- Dual-mode API key storage — server-encrypted (simple) or held entirely in the customer's browser via Web Crypto (the server never sees the plaintext key), switchable from the dashboard at any time, not just at signup
- Full Google Workspace integration (Gmail including attachment reading with real PDF text extraction, Calendar, Drive, Docs, Sheets, Tasks, Contacts) via a company-provisioned utility account per customer
- Voice calls via Vapi, with per-customer key routing fixed (each customer's own configured key is used, not a shared account-level key)
- **Voice spend cap now has a real hard stop, not just a warning (Aug 2026).** Previously flagged as a genuine limitation — a live call was already connecting by the time the cap could be checked, and no verified Vapi mechanism existed to end one mid-call. Confirmed against Vapi's current docs (Live Call Control: `POST {type: end-call}` to a call's `monitor.controlUrl`) and implemented as a background poller that ends any call the moment its customer crosses their monthly cap while still talking. **Honestly flagged:** never exercised against a real live phone call — the exact field names (`monitor.controlUrl`, live `cost`) are confirmed from Vapi's docs, not from a test call through this specific Telnyx-routed setup.
- **Outlook and Teams, via Microsoft Graph (Aug 2026), Cloud only.** Deliberately built as "Option B" rather than Microsoft's own official Work IQ Mail/Calendar and Teams Graph MCP servers, which require a customer M365 Copilot license plus their own IT admin registering a per-tenant Entra app — real friction most individual customers can't clear. Instead: one single Curant-owned Azure AD app (multi-tenant + personal accounts), a standard OAuth2+PKCE consent per customer, and seven Graph-backed tools (search/send email, list/create calendar events, list Teams channels, read/post channel messages). **Requires a one-time manual step outside this codebase** — registering that Azure app and setting `MS_GRAPH_CLIENT_ID`/`MS_GRAPH_CLIENT_SECRET` — and, like the MCP OAuth flow above, has never been run against a real Microsoft account.
- A working customer dashboard — persona/instructions, generation API keys, spend caps, billing status, all real and DB-backed (a separate demo/mockup file that predated this was deleted after confirming it was never functional)
- An owner dashboard flagging customers who need attention (billing issues, over voice budget, pending device-release requests)

---

## What's Not Done Yet

Split honestly into two categories — what's buildable but simply hasn't been prioritized yet, and what's genuinely blocked on something outside a code editor.

### Buildable, not yet built or decided
- **Cloud's MCP OAuth flow is real but unverified against a live provider.** The metadata discovery + Dynamic Client Registration + PKCE flow is implemented and code-reviewed, but no live app is registered with Jira/Linear/GitHub/Postman/etc. and no browser exists in the build environment to complete an actual interactive login — needs a real end-to-end test before a real customer relies on it.
- **The Microsoft Graph Azure AD app hasn't been registered yet.** A 10-minute manual step (multi-tenant + personal-account app, `MS_GRAPH_CLIENT_ID`/`MS_GRAPH_CLIENT_SECRET`) — the Outlook/Teams code is written and compiles but is fully inert without it.
- **Voice-cap hard-stop and Dean's shell access are both real, working code that has never touched real infrastructure.** The voice-cap enforcer's mechanism is confirmed against Vapi's own docs but not against a real call through this specific Telnyx setup; shell access is verified functionally and adversarially in an isolated test environment, but not in an actual customer conversation.
- **Dean's shell access has no OS-level containment.** Post-audit it is meaningfully constrained in `allowlist` mode, but `confirm` and `autonomous` mode rely entirely on customer approval, not on the OS preventing anything. A real `sandbox-exec` profile or container would make the guarantee structural rather than procedural. Known and deliberate, not overlooked.
- **A full security audit (6 Aug 2026) found 14 issues, 3 critical — 2 of which are now fixed.** The remaining critical is that Cloud's customer login requires only an email address, with no password column in the schema at all; several webhook and OAuth endpoints also lack authentication. These are launch blockers for Cloud and are documented separately in the audit report rather than summarized away here.
- **Add-on pricing isn't finalized.** The two real gated add-ons (browser automation, August) have founder-placeholder prices ($10/mo, $15/mo) wired into Stripe, not a finished business decision.
- **Portal-cancellation cleanup behavior is configured but defaults to the conservative option.** Cancelling in-app immediately releases the phone number and deprovisions Workspace; cancelling via Stripe's portal only disables the account unless a specific env var is deliberately flipped.
- **The regression test suite has never actually been run against a live model.** It's real, working code (verified via import-only dry runs), but needs a real `ANTHROPIC_API_KEY` to execute for real — that hasn't happened yet.
- **Home and Cloud diverge slightly in model-tier structure** — Home has a two-tier main/fast split, Cloud doesn't. Both are pinned to the same primary model version as of this pass; the structural difference is unresolved.
- **Regression test coverage is currently limited** to Miles, Leo, and the generalist Curant persona — the other seven personas don't have dedicated test cases yet.
- **Multi-worker deployment requires a manual infrastructure step** (sticky sessions at the reverse proxy) that hasn't been configured — irrelevant at today's single-process deployment scale, but documented so it isn't a surprise later.

### Genuinely external, not solvable by more code
- **A2P 10DLC registration** — required for US SMS compliance via Telnyx, not started. Approval timelines aren't controllable, making this the most time-sensitive open item overall.
- **No real Stripe account/live keys yet** — the billing integration is built and code-verified, but has never processed a real payment.
- **No real TLS/hosting** — `CLOUD_HTTPS` isn't enabled anywhere yet; this needs a real deployment target, not a config flag.
- **Cloud's Privacy Policy is a draft, not a published policy** — needs actual attorney review before any real customer's data should flow through it. Hosting conversations and memory makes Curant a data controller, a materially higher legal bar than Home's local-only model.
- **A pending trademark issue** referenced in the Kickstarter draft needs resolution before public scaling.
- **A real Google Workspace domain** hasn't been acquired — the provisioning code is real and tested against Google's actual Admin SDK, but has nothing to provision against yet.
- **No customers, no revenue, no usage data yet.** This is a pre-launch product. Every cost/margin figure above is a projection built from real, sourced unit costs (Telnyx's own pricing, Vapi's published rates, Workspace distributor pricing) — not observed at scale.

---

## Repo Structure

- **`curant-current/`** — Curant Home. Start with `curant-current/README.md` — documents every feature, every real bug found and fixed along the way, and every honest limitation, in the order things were actually built.
- **`curant-cloud/`** — Curant Cloud. Start with `curant-cloud/README.md`, same documentation standard.
- **`tests/`** — the persona regression suite (see "What's Not Done Yet" above for its current status).
- **`MODEL_VERSION_POLICY.md`** — the actual policy governing model version pinning and rollout, with a running change log.
- **`reference-documents/`** — business and design documents:
  - `Curant_Summary.docx` — product/business summary
  - `Curant_Persona_Reference.docx` — consolidated feature table across all ten personas
  - `Curant_Persona_Profiles.docx` — one detailed profile per persona
  - `Curant_Persona_Psychology_Redesign.docx` — the Big Five / MBTI-style research behind each persona's design
  - `Curant_Job_Specific_Skills_Tools.docx` — job-specific skills and verified real tools mapped to each persona
  - `CostCalculator.jsx` — interactive Cloud economics calculator (open as a React artifact)
  - `Kickstarter_Campaign_Copy.md` — draft campaign copy, several sections explicitly marked as placeholders pending real decisions
  - `Investor_Pitch_Materials.md` — pitch deck content (not slide design), sourced market sizing, unit economics, and illustrative financial projections for an equity raise — explicitly separate from the Kickstarter, which is reward-based, and honest about what's still needed (legal entity confirmation, team bios, an actual equity ask)
