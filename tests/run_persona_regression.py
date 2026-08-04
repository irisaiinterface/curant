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
    """
    return _load_module_from_path("cloud_app_under_test", os.path.join(REPO_ROOT, "curant-cloud", "server", "app.py"))


def build_home_prompt(home_module, persona, model_override=None):
    config = {"persona": persona, "instructions": "", "unlocked_addons": []}
    return home_module.build_system_prompt(config, memories=[], people=[], live_context="")


def build_cloud_prompt(cloud_module, persona):
    customer = {"persona": persona, "instructions": "", "unlocked_addons": []}
    return cloud_module.build_system_prompt(customer, memories=[], people=[], channel="sms")


def call_model(system_prompt, user_prompt, model):
    """Real call to the Anthropic API — no mocking, by design (see module docstring)."""
    import anthropic
    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env
    response = client.messages.create(
        model=model,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()


def check_case(case, reply):
    """Returns (passed: bool, reason: str)."""
    if case["category"] == "tone_sanity":
        word_count = len(reply.split())
        if word_count > case.get("max_words", 9999):
            return False, f"Reply was {word_count} words, expected under {case['max_words']} for this persona's tone."
        return True, f"{word_count} words — within tone expectation."

    reply_lower = reply.lower()
    matched = [p for p in case["signal_phrases"] if p in reply_lower]
    if matched:
        return True, f"Matched signal phrase(s): {matched}"
    return False, "No expected signal phrase found — read the reply manually before concluding this is a real regression."


def run_target(target_name, module, prompt_builder, model, results):
    print(f"\n{'=' * 70}\n{target_name}  (model: {model})\n{'=' * 70}")
    for case in ALL_CASES:
        system_prompt = prompt_builder(module, case["persona"])
        try:
            reply = call_model(system_prompt, case["prompt"], model)
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
    parser.add_argument("--model", default=None,
                        help="Override the model to test (e.g. a candidate version before rollout). "
                             "Defaults to each codebase's own currently-pinned PROVIDER_MODELS value.")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. This suite makes real, billed API calls and has "
              "no mocked mode — set a real key before running:\n"
              "  export ANTHROPIC_API_KEY=sk-ant-...", file=sys.stderr)
        sys.exit(1)

    results = []

    if args.target in ("home", "both"):
        home = load_home()
        home_model = args.model or home.PROVIDER_MODELS["anthropic"]["main"]
        run_target("Home (curant-cli)", home, build_home_prompt, home_model, results)

    if args.target in ("cloud", "both"):
        cloud = load_cloud()
        cloud_model = args.model or cloud.PROVIDER_MODELS["anthropic"]
        run_target("Cloud (server/app.py)", cloud, build_cloud_prompt, cloud_model, results)

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
