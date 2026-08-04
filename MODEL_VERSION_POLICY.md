# Model Version Pinning Policy

## The risk this addresses

Every persona's behavior — tone, judgment calls, escalation thresholds,
tool-use patterns — is shaped by the underlying Claude (or GPT) model
version. If that version changes silently, every persona changes
behavior simultaneously, with no regression test in between and no
warning to any customer. This is a real, documented failure mode in
the AI agent industry, not a hypothetical: providers frequently
deprecate models and use aliases that change production behavior
without notice, and this codebase intentionally does not build on that
pattern.

## The policy

1. **Pin explicitly, everywhere.** Every model string used to actually
   generate a reply resolves through exactly one named constant per
   codebase — `PROVIDER_MODELS` in both `curant-current/curant-cli` and
   `curant-cloud/server/app.py`. No model name should ever be
   hardcoded inline at a call site. (This was cleaned up as part of
   this policy — see the code comment on `PROVIDER_MODELS` in each
   file, and the git history for the specific literal strings that
   were centralized.)

2. **Never auto-upgrade.** A version change is a deliberate, tracked
   decision — not something that happens because a provider's "latest"
   alias moved underneath us. Home already pins explicit version
   strings (`claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, not
   `claude-latest` or similar). Cloud does the same
   (`claude-sonnet-4-6`). Keep it that way.

3. **Regression-test before rolling a version change out to any real
   customer.** `tests/run_persona_regression.py` is the automation for
   this — run it against a candidate version (`--model claude-opus-4-8`
   or similar) before putting that version into `PROVIDER_MODELS`. It
   tests the genuinely model-dependent behaviors (escalation, Miles/Leo's
   domain boundaries) directly against real API calls using each
   codebase's actual, imported `build_system_prompt`. It deliberately
   does NOT test tool confirmation — that's enforced in code
   (`execute_tool_call`/`execute_cloud_tool_call` reject a call missing
   `confirmed: true`), not model judgment, so it can't regress from a
   model change at all. See `tests/README.md` for the real limitation
   (keyword-heuristic pass/fail, not language understanding — read any
   FAIL manually) and what's not covered yet (most personas beyond
   Miles/Leo/Curant, tone-fidelity beyond coarse sanity checks). Even
   with the suite passing, still do:
   - A staged rollout, not a flip for every customer simultaneously

4. **Document every version change here**, with the date, the reason,
   and what was verified before it went out. This file should
   accumulate a real history, not stay static.

## Current pinned versions

| Codebase | Provider | Model string | Constant |
|---|---|---|---|
| Home (`curant-cli`) | Anthropic | `claude-sonnet-4-6` (main), `claude-haiku-4-5-20251001` (fast) | `PROVIDER_MODELS["anthropic"]` |
| Home (`curant-cli`) | OpenAI | `gpt-4o` (main), `gpt-4o-mini` (fast) | `PROVIDER_MODELS["openai"]` |
| Cloud (`server/app.py`) | Anthropic | `claude-sonnet-4-6` | `PROVIDER_MODELS["anthropic"]` |
| Cloud (`server/app.py`) | OpenAI | `gpt-4o` | `PROVIDER_MODELS["openai"]` |

**Resolved:** Home's main Anthropic model was previously pinned to
`claude-sonnet-5`, different from Cloud's `claude-sonnet-4-6` — flagged
as a deliberate, unresolved product decision in the prior version of
this document. Decided in Cloud's favor: Home's "main" tier now matches
Cloud exactly, so persona behavior on the primary conversational path
is consistent across both tiers. **Still not harmonized, and not part
of this decision:** Home's two-tier main/fast structure itself — Cloud
has no "fast" tier equivalent, and Home's `fast` model
(`claude-haiku-4-5-20251001`) is unchanged. Whether Cloud should adopt
a similar fast-tier split (e.g. for cheap operations like memory
extraction) is a separate architectural question, not addressed here.

## Change log

- **Initial policy adoption** — centralized 5 literal `"claude-sonnet-4-6"`
  strings scattered across Cloud's Vapi integration into references to
  the existing `PROVIDER_MODELS` constant. This also fixed a real bug
  in the process: the Vapi response/model-block fields were hardcoding
  the Claude model name regardless of which provider a given customer
  actually had configured (`api_provider`) — a customer using OpenAI
  would have gotten a response falsely labeled as Claude-generated.
  Home's `curant-cli` was already clean (single `PROVIDER_MODELS`
  source, no stray literals found) and needed no code change, only
  this policy document.

- **Regression-test suite built** (`tests/`) — turned "manual
  verification" from the original policy text into real automation.
  Imports each codebase's actual `build_system_prompt`/`PERSONAS`
  directly (no duplicated prompt text to drift), makes real API calls
  against a target/candidate model, checks escalation and Miles/Leo
  domain-boundary compliance via keyword heuristics. Deliberately does
  not test tool confirmation, since that's code-enforced and can't
  regress from a model change. Full limitations documented honestly in
  `tests/README.md`.

- **Home/Cloud version harmonization decided** — resolved the
  previously-flagged inconsistency in Cloud's favor. Home's `main`
  Anthropic model changed from `claude-sonnet-5` to `claude-sonnet-4-6`,
  matching Cloud exactly. Home's `fast` tier (`claude-haiku-4-5-20251001`)
  is unchanged — no Cloud equivalent to harmonize against. **Honest
  gap:** this change was NOT run through `tests/run_persona_regression.py`
  before landing — no live `ANTHROPIC_API_KEY` was available in the
  environment this was built in. Per this policy's own rule 3, that
  regression pass should happen before this version is trusted in front
  of real customers, even though the suite technically exists now.
