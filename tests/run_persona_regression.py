#!/usr/bin/env python3
"""
Persona regression test runner. Builds REAL system prompts using each
codebase's actual PERSONAS/build_system_prompt (imported directly from
curant-current/curant-cli and curant-cloud/server/app.py — never
duplicated or re-typed here, so there's no risk of the test drifting
from what customers actually get), sends each test case to the real
configured model, and checks the reply against tests/persona_test_cases.py.

REQUIRES a real ANTHROPIC_API_KEY in the environment — this makes real,
billed API calls. There is no mocked/simulated mode: the entire point
is testing actual model behavior against a candidate version, not
testing this script's own logic.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 tests/run_persona_regression.py                  # both codebases
    python3 tests/run_persona_regression.py --target home    # just Home
    python3 tests/run_persona_regression.py --target cloud   # just Cloud
    python3 tests/run_persona_regression.py --model claude-opus-4-8
        # test a CANDIDATE version before rolling it into PROVIDER_MODELS —
        # this is the actual intended use per MODEL_VERSION_POLICY.md
    GEMINI_API_KEY=... python3 tests/run_persona_regression.py \
        --target cloud --provider gemini
        # run the personas through Gemini (Cloud only — Home doesn't pin a
        # gemini model). Uses gemini-3.6-flash unless --model overrides.

Exit code is 0 if every case passes, 1 if any case fails or errors —
CI-friendly, but per this suite's own documented limitation (see
persona_test_cases.py's module docstring), a human should still read
any FAIL before concluding something is actually broken.
"""

import argparse
import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

from persona_test_cases import ALL_CASES  # noqa: E402


def _load_module_from_path(module_name, file_path):
    """
    curant-cli has no .py extension (it's installed as a standalone
    Homebrew binary), so it can't be imported normally — this loads it
    as a module directly from its file path instead. server/app.py
    could be imported normally, but this keeps both codebases handled
    the same way for consistency.

    Explicitly using SourceFileLoader rather than plain
    spec_from_file_location(name, path): importlib can't infer a loader
    from a file with no recognized extension (like curant-cli), which
    leaves spec.loader as None and crashes module_from_spec — confirmed
    while actually testing this, not assumed.
    """
    import importlib.machinery
    loader = importlib.machinery.SourceFileLoader(module_name, file_path)
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_home():
    return _load_module_from_path("curant_cli_under_test", os.path.join(REPO_ROOT, "curant-current", "curant-cli"))


def load_cloud():
    """
    Note: importing server/app.py has one real module-level side effect
    — it starts a background daemon thread (_session_cleanup) as part
    of its normal startup. Harmless for a short test run (it's just a
    sleep loop that dies with this process when the script exits), but
    worth knowing rather than a silent surprise.

    REAL BUG FIXED: app.py does a bare `import billing` (its sibling
    module in curant-cloud/server/), which only resolves if that
    directory is on sys.path. Normal Flask usage always has it there
    implicitly (app.py is run FROM that directory), but loading it via
    SourceFileLoader from tests/ -- as this script does -- does not add
    it automatically. Confirmed live: this crashed with
    "ModuleNotFoundError: No module named 'billing'" the first time this
    suite was actually run end-to-end (against a real Gemini key), which
    is exactly the kind of gap the suite's own docstring warned it had
    never been executed enough to catch.
    """
    server_dir = os.path.join(REPO_ROOT, "curant-cloud", "server")
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)
    return _load_module_from_path("cloud_app_under_test", os.path.join(server_dir, "app.py"))


def build_home_prompt(home_module, persona, model_override=None):
    config = {"persona": persona, "instructions": "", "unlocked_addons": []}
    return home_module.build_system_prompt(config, memories=[], people=[], live_context="")


def build_cloud_prompt(cloud_module, persona):
    customer = {"persona": persona, "instructions": "", "unlocked_addons": []}
    return cloud_module.build_system_prompt(customer, memories=[], people=[], channel="sms")


# Gemini speaks the OpenAI wire format via its compatibility endpoint, so the
# openai SDK serves it by swapping only base_url + model — same approach the
# Cloud server uses in _openai_compatible_client().
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Which env var holds the key for each provider (first match wins).
PROVIDER_KEY_ENV = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai":    ["OPENAI_API_KEY"],
    "gemini":    ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
}


def _api_key_for(provider):
    for name in PROVIDER_KEY_ENV[provider]:
        if os.environ.get(name):
            return os.environ[name], name
    return None, PROVIDER_KEY_ENV[provider][0]


def _default_model(module, provider):
    """Each codebase pins its own model in PROVIDER_MODELS. Home stores a dict
    ({'main': ...}); Cloud stores a plain string. A provider a given codebase
    doesn't define (e.g. gemini on Home) raises KeyError, handled in main()."""
    pm = module.PROVIDER_MODELS[provider]
    return pm["main"] if isinstance(pm, dict) else pm


def call_model(system_prompt, user_prompt, model, provider="anthropic"):
    """Real call to the selected provider — no mocking, by design (see module
    docstring). Gemini and OpenAI go through the OpenAI SDK; Gemini only differs
    by base_url, exactly as the Cloud server does it."""
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env
        response = client.messages.create(
            model=model,
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()

    from openai import OpenAI
    api_key, _ = _api_key_for(provider)
    client = (OpenAI(api_key=api_key, base_url=GEMINI_OPENAI_BASE_URL)
              if provider == "gemini" else OpenAI(api_key=api_key))
    resp = client.chat.completions.create(
        model=model,
        max_tokens=500,
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_prompt}],
    )
    return (resp.choices[0].message.content or "").strip()


def check_case(case, reply):
    """Returns (passed: bool, reason: str)."""
    if case["category"] == "tone_sanity":
        word_count = len(reply.split())
        if word_count > case.get("max_words", 9999):
            return False, f"Reply was {word_count} words, expected under {case['max_words']} for this persona's tone."

        # Literal, case-sensitive check — for style markers like "!" where
        # lowercasing would be irrelevant or actively wrong.
        forbidden = case.get("forbid_phrases") or []
        hit = [p for p in forbidden if p in reply]
        if hit:
            return False, f"Reply contained forbidden style marker(s) {hit} — violates this persona's tone rule."

        required_any = case.get("require_any_phrases") or []
        if required_any:
            reply_lower = reply.lower()
            matched = [p for p in required_any if p in reply_lower]
            if not matched:
                return False, (
                    "None of the expected warmth/tone markers appeared — read the reply "
                    "manually before concluding this is a real regression, since tone can "
                    "come through in ways this keyword list doesn't capture."
                )
            return True, f"Matched tone marker(s): {matched}"

        return True, f"{word_count} words — within tone expectation."

    reply_lower = reply.lower()
    matched = [p for p in case["signal_phrases"] if p in reply_lower]
    if matched:
        return True, f"Matched signal phrase(s): {matched}"
    return False, "No expected signal phrase found — read the reply manually before concluding this is a real regression."


def run_target(target_name, module, prompt_builder, model, results, provider="anthropic"):
    print(f"\n{'=' * 70}\n{target_name}  (provider: {provider}, model: {model})\n{'=' * 70}")
    for case in ALL_CASES:
        system_prompt = prompt_builder(module, case["persona"])
        try:
            reply = call_model(system_prompt, case["prompt"], model, provider)
        except Exception as e:
            print(f"[ERROR] {target_name} / {case['persona']} / {case['category']}: API call failed — {e}")
            results.append((target_name, case, False, f"API call failed: {e}"))
            continue

        passed, reason = check_case(case, reply)
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {target_name} / {case['persona']} / {case['category']}")
        print(f"        prompt:  {case['prompt'][:80]}{'...' if len(case['prompt']) > 80 else ''}")
        print(f"        reply:   {reply[:150]}{'...' if len(reply) > 150 else ''}")
        print(f"        reason:  {reason}")
        results.append((target_name, case, passed, reason))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", choices=["home", "cloud", "both"], default="both")
    parser.add_argument("--provider", choices=["anthropic", "openai", "gemini"], default="anthropic",
                        help="Which provider to test the personas against. Default anthropic. "
                             "gemini/openai run through the OpenAI-compatible path and need "
                             "GEMINI_API_KEY / OPENAI_API_KEY set respectively.")
    parser.add_argument("--model", default=None,
                        help="Override the model to test (e.g. a candidate version before rollout). "
                             "Defaults to each codebase's own currently-pinned PROVIDER_MODELS value.")
    args = parser.parse_args()

    key, key_name = _api_key_for(args.provider)
    if not key:
        print(f"{key_name} is not set. This suite makes real, billed API calls and has "
              f"no mocked mode — set a real {args.provider} key before running:\n"
              f"  export {key_name}=...", file=sys.stderr)
        sys.exit(1)

    results = []

    if args.target in ("home", "both"):
        home = load_home()
        try:
            home_model = args.model or _default_model(home, args.provider)
        except KeyError:
            print(f"Home (curant-cli) has no '{args.provider}' entry in PROVIDER_MODELS — "
                  f"pass --model to test it against this provider, or use --target cloud.",
                  file=sys.stderr)
            sys.exit(1)
        run_target("Home (curant-cli)", home, build_home_prompt, home_model, results, args.provider)

    if args.target in ("cloud", "both"):
        cloud = load_cloud()
        try:
            cloud_model = args.model or _default_model(cloud, args.provider)
        except KeyError:
            print(f"Cloud (server/app.py) has no '{args.provider}' entry in PROVIDER_MODELS — "
                  f"pass --model to test it against this provider.", file=sys.stderr)
            sys.exit(1)
        run_target("Cloud (server/app.py)", cloud, build_cloud_prompt, cloud_model, results, args.provider)

    total = len(results)
    passed = sum(1 for _, _, p, _ in results if p)
    print(f"\n{'=' * 70}\n{passed}/{total} passed\n{'=' * 70}")

    failures = [(t, c, r) for t, c, p, r in results if not p]
    if failures:
        print("\nFAILURES (read these manually — see the limitation note in persona_test_cases.py):")
        for target_name, case, reason in failures:
            print(f"  - {target_name} / {case['persona']} / {case['category']}: {reason}")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
