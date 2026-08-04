# Curant — Full Delivery

Everything built to date, in one place.

## Folders

- **`curant-current/`** — Curant Home. Fully local, runs on the customer's
  own Mac (or Windows/Linux via Docker), reachable by iMessage. Start
  with `curant-current/README.md` — it documents every feature, every
  real bug found and fixed along the way, and every honest limitation,
  in the order things were actually built.

- **`curant-cloud/`** — Curant Cloud. Hosted, no hardware required,
  reachable by SMS/voice via Telnyx + Vapi. Start with
  `curant-cloud/README.md`, same documentation standard as Home's.

- **`reference-documents/`** — Business and design documents produced
  along the way:
  - `Curant_Summary.docx` — product/business summary for mentor review
  - `Curant_Persona_Reference.docx` — consolidated feature table across
    all ten personas
  - `Curant_Persona_Profiles.docx` — one detailed profile per persona
  - `Curant_Persona_Psychology_Redesign.docx` — the Big Five / 16Personalities
    research behind each persona's design
  - `Curant_Job_Specific_Skills_Tools.docx` — job-specific skills and
    verified real tools mapped to each persona
  - `CostCalculator.jsx` — interactive Cloud economics calculator (open
    as a React artifact)
  - `Kickstarter_Campaign_Copy.md` — campaign title, pitch, use-of-funds
    breakdown, stretch goals, and FAQ for a Cloud-launch funding raise —
    several sections marked as placeholders pending real decisions
    (reward tiers, risk-section voice, Privacy Policy FAQ answer)

## Quick orientation

Both codebases are real, tested, working Python — not scaffolding.
Every feature documented in the two READMEs was verified: either
against real infrastructure (a locally-run browser, a real local MCP
test server, a real spec-compliant OAuth flow built from scratch to
prove the handshake actually completes) or with mocks shaped to match
the real SDK/API objects being used, with the boundary between the two
stated honestly wherever full live verification wasn't possible.

What's still open is almost entirely external at this point — a real
Google Workspace domain, A2P 10DLC registration, real hosting, and a
Stripe account. Both READMEs list these explicitly near the end.
