# Curant — Investor Pitch Materials

*Content for a pitch deck / data room — not slide design, the actual argument and data behind each section. Cross-referenced against the real codebase and business documents in this repo throughout; nothing here is invented or rounded up without saying so.*

---

## 1. Executive Summary

Curant is a personal AI secretary reachable by phone call or text — not another app to open. It competes against hiring a human assistant, not against ChatGPT. Two tiers: **Curant Home** (fully local, runs on the customer's own Mac, built and working today) and **Curant Cloud** (hosted, no hardware required — the tier this raise is aimed at bringing to launch readiness).

The core economic fact shaping everything below: **every AI inference cost is bring-your-own-key**, so Curant's own cost structure is pure infrastructure, not compute — yielding ~76-85% gross margin per customer even before any scale efficiencies.

This is a **pre-revenue, pre-launch** company. Every figure in this document is either a real, sourced unit cost, or an explicitly-labeled projection built from assumptions — never presented as observed traction.

---

## 2. The Problem

Every AI assistant on the market today assumes the same thing: open an app, find the window, type. That's a real friction point for the people Curant targets — solo operators and small business owners who are already juggling every function of their business themselves, and for whom "one more app to check" is a genuine cost, not a minor inconvenience.

The alternative that actually exists today — hiring a human assistant — is expensive, hard to find, and not viable until someone is already successful enough to afford it. Curant is positioned in the gap between "an AI tool I have to remember to use" and "a human employee I can't yet afford."

---

## 3. The Solution

An AI secretary, reachable by call or text, with a consistent persona across every interaction — not a chatbot that resets. Ten personas, each grounded in Big Five psychology research (not just tone flavor — Conscientiousness and Emotional Stability are the two traits research actually ties to service-role performance) and specialized for a real domain (legal/finance, healthcare admin, real estate, education, executive ops, and more), each with verified tool knowledge for real, currently-used software in that domain.

Full technical and product detail: root `README.md`, `curant-current/README.md`, `curant-cloud/README.md`.

---

## 4. Market Opportunity

**Top-down figures are noisy and inconsistent** across market research firms, because "personal AI assistant" gets scoped very differently between reports — some include smart speakers and enterprise customer-service chatbots, which aren't Curant's market at all:

<cite index="46-1">The personal AI assistant market is estimated to grow from $3.4 billion in 2025 to $4.84 billion in 2026 at a 42.2% CAGR, reaching $19.63 billion by 2030</cite>, per one research firm. A different firm scopes the same category at <cite index="51-1">$4.26 billion in 2026, reaching $78.01 billion by 2035 at a 38.15% CAGR</cite>. Neither number is "the" TAM for Curant specifically — both are directional evidence of a genuinely fast-growing category, not a defensible ceiling for this product.

**A bottom-up estimate is more honest for a product this specifically positioned.** Curant's stated competitive frame is "hire Curant instead of a human assistant" — the buyer is a solo operator or small business owner handling their own admin. <cite index="63-1">The US Census Bureau counted roughly 29.8 million non-employer establishments (solopreneurs) in 2023 — 81.9% of all small businesses, generating about $1.7 trillion in revenue, 6.4% of US GDP</cite>. That's the most defensible SAM anchor available — a real, government-sourced count of exactly the buyer segment Curant is positioned against, not a vendor market-research estimate.

| | Figure | Basis |
|---|---|---|
| **SAM** (US solopreneurs) | ~29.8M | US Census Bureau, non-employer establishments, most recent available count |
| **SOM at 0.1% penetration** | ~29,800 customers | Illustrative — not a projection, see §7 |
| **SOM at 1% penetration** | ~298,000 customers | Illustrative — not a projection, see §7 |

**Explicitly not claimed:** an international TAM, an enterprise/team-tier TAM, or any penetration-rate assumption presented as likely rather than illustrative. A real go-to-market plan with actual channel/CAC data would sharpen this considerably — that data doesn't exist yet (see §8).

---

## 5. Business Model & Unit Economics

- **Base:** $29/mo. **Executive:** $149/mo (all-inclusive). Add-ons priced individually (currently: Browser Automation $10/mo, August creative generation $15/mo — both founder-placeholder prices, not finalized).
- **The core economic fact:** every AI inference call — the persona reasoning itself, and August's image/voice/video generation — is bring-your-own-key. The business's own cost is pure infrastructure (Telnyx number + SMS/voice usage, Google Workspace seat, Stripe processing), never compute.

| Plan | Price | Est. cost/customer/mo | **Margin $** | **Margin %** |
|---|---|---|---|---|
| Base | $29.00 | $6.94 | $22.06 | **76%** |
| Executive | $149.00 | $23.02 | $125.98 | **85%** |

*Cost assumptions: ~150 SMS/mo (Base) or ~400 SMS/mo (Executive), 40%/80% voice adoption respectively at Vapi's $0.20/min blended rate, 30%/100% Workspace adoption at $3/mo distributor pricing, Stripe's 2.9%+$0.30. Full breakdown: `CostCalculator.jsx` (interactive) and `Curant_Summary.docx`.*

**Billing is real, not aspirational** — Stripe Checkout, a hosted Customer Portal for self-service, webhook-driven subscription state as the actual source of truth (not the browser success-redirect, which would be a real security gap if trusted alone). Built and code-verified; has not yet processed a live payment (no production Stripe keys yet).

---

## 6. Competitive Landscape

Curant is not positioned against ChatGPT, Lindy, or other AI-assistant products on capability or model quality — it's positioned against **hiring a human assistant**, using phone-native access as the credibility mechanism (dialing a number is the same action as reaching a human hire; opening an app is not).

| Axis | Typical AI assistant product | Curant |
|---|---|---|
| Interface | Chat window / app | Phone call or text |
| Identity | Resets each session | Same persona, persistent memory |
| Trust model | General-purpose | Domain-scoped, stated boundaries (e.g. Miles never gives legal advice, only handles the paperwork around it) |
| Data | Sent to vendor's cloud | Customer's choice — fully local (Home) or hosted with browser-held encryption option (Cloud) |
| Action-taking | Often autonomous by default | Every consequential action requires explicit confirmation — enforced in code, not just prompted |

Full positioning work, including the "three tiers of noncustomers" analysis (who currently isn't served by any AI assistant product, and why each group specifically would convert to Curant): `Curant_Summary.docx`.

---

## 7. Traction & Execution Proof

**Honest framing: there is no revenue, no paying customers, and no usage data yet.** This is a pre-launch product. Any investor materials claiming otherwise would be wrong.

What exists instead, as a substitute signal at this stage:

- **A genuinely complete, working product**, not a prototype — both tiers have real, tested integrations (Google Workspace, Telnyx, Vapi, Stripe, Playwright browser automation, FLUX/Ideogram/ElevenLabs/Veo generation), each verified against real infrastructure or real vendor documentation rather than assumed. This is unusual completeness for a pre-seed stage company — most of the technical risk in "can this actually be built" is already retired.
- **Real, code-enforced safety properties**, not just described ones — the confirmation gate on consequential actions and the spend caps on metered generation are structural, not prompt-based, meaning they can't silently regress as the underlying model changes.
- **A documented verification discipline** running through the entire build — every commit in this repo's history either confirms something against real infrastructure or flags explicitly where that wasn't yet possible. That discipline is itself a signal about execution quality, separate from the product's current market traction.

**What this section deliberately does not claim:** waitlist signups, LOIs, beta users, or any social-proof metric that doesn't currently exist. If any of those become real, this section should be updated with them specifically, not vague enthusiasm.

---

## 8. Financial Projections (Illustrative — Not a Verified Forecast)

**Every number below is a placeholder assumption**, not observed data or a committed plan. No customer acquisition channel, CAC, or churn rate has been tested — those numbers do not exist yet and are not modeled here. This section exists to show the shape of the unit economics at scale, not to represent diligence-grade projections.

| | Year 1 | Year 2 | Year 3 |
|---|---|---|---|
| Customers (end of year) | 200 | 1,500 | 6,000 |
| Blended ARPU/mo* | $45 | $45 | $45 |
| Annual revenue | $108,000 | $810,000 | $3,240,000 |
| Annual cost (infra only) | $24,000 | $180,000 | $720,000 |
| **Gross margin** | **$84,000 (78%)** | **$630,000 (78%)** | **$2,520,000 (78%)** |

*\*Blended ARPU assumes a mix of Base/Executive/add-ons; not derived from any real customer distribution, since none exists yet.*

**What's missing from this model, stated plainly:** customer acquisition cost, marketing/sales spend, churn/retention assumptions, payroll beyond the current solo-founder structure, and any funding-round dilution modeling. A real financial model for an actual raise needs all of these — this table only demonstrates that the per-customer unit economics are sound at scale, which is a narrower and much safer claim.

---

## 9. The Ask

**Not yet finalized as an equity ask.** Currently in motion: a Kickstarter campaign (`Kickstarter_Campaign_Copy.md`) for $4,500, explicitly structured as reward-based (non-dilutive) funding for Cloud's specific launch-readiness costs — legal/compliance, not inference or development. That campaign and any equity raise are separate instruments and shouldn't be conflated in the same pitch without being explicit about which is being discussed.

**For an actual equity raise, still needed before this section can be filled in for real:**
- A formed legal entity (LLC/C-corp) — not confirmed as existing yet in anything reviewed for this document
- A cap table
- Actual round terms (SAFE vs. priced round, valuation/cap, amount) — these need a lawyer, not a document draft
- A specific use-of-funds breakdown distinct from the Kickstarter's (equity capital typically funds team/growth, not just compliance runway)

---

## 10. Risks

- **Pre-revenue.** No validated demand signal yet beyond product completeness.
- **Solo-founder execution risk** — all current development, product, and business work is done by one person.
- **Regulatory dependencies** — A2P 10DLC registration (SMS compliance) has an external approval timeline outside the company's control; the Cloud Privacy Policy needs real attorney review before real customer data should flow through it, since hosting conversations/memory makes Curant a data controller.
- **Margin assumptions are cost-side verified, demand-side unverified.** The infrastructure cost figures are real and sourced (Telnyx, Vapi, Workspace's own published pricing); the customer-count and ARPU assumptions in §8 are not.
- **BYOK dependency.** The entire margin structure depends on customers bringing their own AI provider key. If that model ever needed to change (e.g., a customer segment that wants a fully-managed, non-BYOK price), the unit economics in §5 would need to be rebuilt from scratch.

---

## 11. Team

*Not filled in — needs the founder's actual background, not a generated bio. A solo-founder narrative is a real, common pattern for pre-seed AI products, but investors will want the specific "why this founder, why now" case made directly, not inferred from commit history.*

---

## Open items before this is presentation-ready

1. **Legal entity and cap table status** — unconfirmed, needs a real answer before any equity conversation can proceed
2. **Team section** — needs the founder's real bio/background
3. **The equity ask itself** — amount, instrument, valuation/cap — needs a lawyer and a real decision, not a draft
4. **Visual design** — this document is content only, not slide layout/design
5. **A real demo** — a recorded video or live walkthrough would meaningfully strengthen §7, given there's no traction data to lean on otherwise
