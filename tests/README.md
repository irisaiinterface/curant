# Persona Regression Tests

Backs the policy in `MODEL_VERSION_POLICY.md`: "regression-test before
rolling a version change out to any real customer." Before this suite
existed, that step was a manual checklist. This is the actual
automation behind it.

## What this tests

The *soft*, model-dependent behaviors that a version change could
silently break:

- **Escalation** — does the persona still defer on high-stakes/uncertain
  decisions (legal, medical, financial) instead of confidently answering
  anyway?
- **Domain boundaries** — does Miles still refuse to give legal/financial
  advice? Does Leo still refuse clinical judgment calls?
- **Tone sanity** — coarse checks only (e.g. Miles's replies staying
  short), not deep persona-fidelity testing.

## What this deliberately does NOT test

**The confirmation gate.** `fill_and_submit_form`, `gmail_send`,
`calendar_create_event` with attendees, `calendar_delete_event`, and
`drive_share` all require `confirmed: true`, enforced in *code*
(`execute_tool_call`/`execute_cloud_tool_call` reject the call outright
if it's missing or false) — not model judgment. A model version change
cannot break this regardless of how the model behaves, so there's
nothing here worth regression-testing. This is worth stating
explicitly: it's a real, positive property of the system, not an
oversight in this test suite.

## Real limitation, stated plainly

Pass/fail is decided by keyword-heuristic matching (`signal_phrases` in
`persona_test_cases.py`), not real language understanding. A model can
genuinely comply while phrasing it in a way that matches nothing on the
list (false FAIL), or, much more rarely, use a listed phrase without
really complying (false PASS). This suite is built to catch a **gross**
regression — a persona that stopped deferring entirely, or started
giving direct legal/medical advice outright — not to certify subtle
behavioral nuance. **Read any FAIL manually before concluding something
actually broke.**

## Running it

Requires a real `ANTHROPIC_API_KEY` — this makes real, billed API
calls. There's no mocked mode; the entire point is testing actual model
behavior, not this script's own logic.

```bash
export ANTHROPIC_API_KEY=sk-ant-...

# Test both codebases against their currently-pinned model versions
python3 tests/run_persona_regression.py

# Test just one
python3 tests/run_persona_regression.py --target home
python3 tests/run_persona_regression.py --target cloud

# The actual intended use per MODEL_VERSION_POLICY.md: test a candidate
# version BEFORE putting it into PROVIDER_MODELS
python3 tests/run_persona_regression.py --model claude-opus-4-8
```

Exit code is 0 if every case passes, 1 if any case fails or a real API
error occurred — CI-friendly, but see the limitation above.

## How it avoids drifting from the real prompts

`build_system_prompt` and `PERSONAS` are imported directly from
`curant-current/curant-cli` and `curant-cloud/server/app.py` — never
duplicated or re-typed here. `curant-cli` has no `.py` extension (it's
installed as a standalone Homebrew binary), so it's loaded via
`importlib` from its file path rather than a normal import; both files
guard their entry point with `if __name__ == "__main__":`, so importing
either one doesn't trigger the CLI's argument parsing or the Flask
server's `app.run()`.

One real, harmless side effect worth knowing: importing `server/app.py`
starts a background daemon thread (`_session_cleanup`) as part of its
normal module-level startup — dies with the test process, doesn't
affect results.

## What's not built yet

- **Tone-fidelity testing beyond the coarse sanity check.** The `forbid_phrases`
  / `require_any_phrases` mechanism (Grace's no-exclamation-points rule,
  Frank's warmth markers) catches a handful of concrete, literal style
  markers — it does not verify that Frank actually *sounds* "warm and
  upbeat" versus Grace sounding "composed and formal" at a holistic level.
  That would need either a much larger rubric-based judge (a second LLM
  call scoring against a style rubric) or human review — neither exists
  here yet.
- **Cloud-specific behaviors** (Vapi voice channel prompt differences,
  Workspace tool confirmation wording) aren't covered — this suite only
  tests the shared persona/escalation/boundary layer, not
  channel-specific prompt variations.
- **Every case still needs a real run against a live key.** All 10
  personas now have at least one dedicated case (16 cases total as of
  this pass), but see "Running it" above — none of this has actually
  executed against `ANTHROPIC_API_KEY` yet, only compiled and
  import-verified.
