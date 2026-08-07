#!/usr/bin/env python3
"""
curant-watcher preflight — run this ON THE MAC that will host a customer's
Curant, before trusting the watcher to answer texts unattended.

It verifies the four things that actually make "text in -> text back" work,
in the same way the watcher does them, and can do a live end-to-end test:

  1. Full Disk Access      — can we read ~/Library/Messages/chat.db?
  2. curant-cli ready       — installed, on PATH, activated, API key set?
  3. Messages automation    — can we send an iMessage via AppleScript?
  4. Full round-trip        — (optional, --full) actually run curant-cli relay
                              on a test message and text the reply back, which
                              exercises the exact production path end to end.

USAGE
    # checks 1-3 plus a live test send to yourself:
    python3 watcher_preflight.py --to "+15551234567"

    # full loop: generate a real reply with your key and text it back
    python3 watcher_preflight.py --to "+15551234567" --full \\
        --from-sender "+15559876543"

    --to           where to send the test iMessage (your own number/Apple ID)
    --from-sender  the Apple ID/number that will TEXT the Curant (defaults to --to)
    --full         also run `curant-cli relay` and deliver its reply
    --watcher      path to curant-watcher.py, to check the CUSTOMER_APPLE_ID
                   placeholder was filled in (default: ./curant-watcher.py)

Nothing here writes to chat.db or changes config; the only side effect is
sending the test iMessage(s) you explicitly target with --to.
"""
import argparse, json, os, re, shutil, sqlite3, subprocess, sys

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
CONFIG  = os.path.expanduser("~/.curant/config.json")

def line(status, name, detail=""):
    mark = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}[status]
    print(f"  [{mark}] {name}" + (f"\n         {detail}" if detail else ""))
    return status == "PASS"

def check_full_disk_access():
    if not os.path.exists(CHAT_DB):
        return line("FAIL", "Full Disk Access / chat.db",
                    f"{CHAT_DB} not found. Is this the Mac signed into Messages?")
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        conn.execute("SELECT COUNT(*) FROM message").fetchone()
        conn.close()
        return line("PASS", "Full Disk Access / chat.db", "chat.db is readable.")
    except sqlite3.OperationalError as e:
        return line("FAIL", "Full Disk Access / chat.db",
                    f"Can't read chat.db ({e}). Grant Full Disk Access to your "
                    f"terminal/python in System Settings > Privacy & Security > Full Disk Access, "
                    f"then restart it.")

def check_cli():
    path = shutil.which("curant-cli")
    if not path:
        return line("FAIL", "curant-cli on PATH",
                    "Not found. Install it and ensure it's on your PATH.")
    line("PASS", "curant-cli on PATH", path)
    try:
        out = subprocess.run(["curant-cli", "status"], capture_output=True, text=True, timeout=30)
        text = (out.stdout + out.stderr).strip()
    except Exception as e:
        return line("FAIL", "curant-cli status", f"Couldn't run it: {e}")
    dev_bypass = os.environ.get("CURANT_DEV_UNLICENSED") == "1"
    if "Active" in text:
        line("PASS", "curant-cli activated", text.splitlines()[0])
    elif dev_bypass and "DEV (unlicensed)" in text:
        line("PASS", "curant-cli activated",
            "CURANT_DEV_UNLICENSED=1 is set — license check intentionally bypassed for local testing.")
    else:
        return line("FAIL", "curant-cli activated",
                    f"{text}\n         Run: curant-cli activate <license-key>, or set "
                    f"CURANT_DEV_UNLICENSED=1 for local testing without a license server.")
    # API key present? Checks config.json, and (under the dev bypass) the same
    # provider env vars get_api_key() itself falls back to.
    try:
        cfg = json.load(open(CONFIG)) if os.path.exists(CONFIG) else {}
    except Exception:
        cfg = {}
    keys = cfg.get("api_keys", {}) or {}
    if keys or cfg.get("anthropic_api_key"):
        provs = ", ".join([k for k, v in keys.items() if v] or ["anthropic (legacy field)"])
        return line("PASS", "API key set", f"providers: {provs}")
    if dev_bypass:
        env_provs = [p for p, names in {
            "anthropic": ["ANTHROPIC_API_KEY"], "openai": ["OPENAI_API_KEY"],
            "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        }.items() if any(os.environ.get(n) for n in names)]
        if env_provs:
            return line("PASS", "API key set", f"from environment (dev bypass): {', '.join(env_provs)}")
    return line("FAIL", "API key set",
                "No API key in ~/.curant/config.json, and none found in the environment either. "
                "Run: curant-cli set-api-key sk-ant-...  (or export ANTHROPIC_API_KEY / "
                "OPENAI_API_KEY / GEMINI_API_KEY under CURANT_DEV_UNLICENSED=1)")

def check_customer_identity(_watcher_path=None):
    """The watcher keys on the customer's Apple ID, read from the env or
    ~/.curant/config.json (no code editing). Mirror that resolution here."""
    cfg = {}
    if os.path.exists(CONFIG):
        try:
            cfg = json.load(open(CONFIG))
        except Exception:
            cfg = {}
    primary = (os.environ.get("CURANT_CUSTOMER_APPLE_ID")
               or cfg.get("customer_apple_id") or "").strip()
    extra = os.environ.get("CURANT_CUSTOMER_HANDLES") or cfg.get("customer_handles") or ""
    extra = extra if isinstance(extra, list) else [h.strip() for h in str(extra).split(",")]
    handles = [h.strip() for h in [primary, *extra] if h and h.strip()]
    if not handles:
        return line("FAIL", "Customer Apple ID configured",
                    "None set. Add it without editing code — either:\n"
                    "           export CURANT_CUSTOMER_APPLE_ID=\"name@icloud.com\"\n"
                    "         or put \"customer_apple_id\" in ~/.curant/config.json.")
    at = "@" in primary if primary else any("@" in h for h in handles)
    note = "" if at else ("         (looks like a phone number, not an Apple ID email — "
                          "an Apple ID is preferred, but this will still work.)")
    return line("PASS", "Customer Apple ID configured",
                f"listening for: {', '.join(handles)}" + (f"\n{note}" if note else ""))

def send_imessage(to_id, text):
    """The exact AppleScript send_text_reply uses in curant-watcher.py."""
    safe = text.replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{to_id}" of targetService
        send "{safe}" to targetBuddy
    end tell
    '''
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)

def check_send(to_id):
    r = send_imessage(to_id, "Curant preflight: this is a test send. If you got this, "
                             "Messages automation works.")
    if r.returncode == 0:
        return line("PASS", "Messages automation (test send)",
                    f"Sent a test iMessage to {to_id}. Confirm it arrived.")
    return line("FAIL", "Messages automation (test send)",
                f"osascript failed: {r.stderr.strip()}\n         Grant Automation permission: "
                f"System Settings > Privacy & Security > Automation > your terminal > Messages. "
                f"Also make sure Messages.app is open and signed in.")

def check_full_roundtrip(from_sender, to_id):
    print("  ---- full round-trip (real curant-cli relay + reply) ----")
    try:
        r = subprocess.run(["curant-cli", "relay", "Hi, this is a preflight test — reply with one short sentence.",
                            "--apple-id", from_sender], capture_output=True, text=True, timeout=120)
    except Exception as e:
        return line("FAIL", "curant-cli relay round-trip", f"Couldn't run relay: {e}")
    raw = r.stdout.strip()
    try:
        data = json.loads(raw)
    except Exception:
        return line("FAIL", "curant-cli relay round-trip",
                    f"relay didn't return JSON (watcher would choke here too):\n         {raw[:200]}")
    if data.get("error"):
        return line("FAIL", "curant-cli relay round-trip",
                    f"relay returned error: {data['error']}")
    reply = data.get("reply")
    if not reply:
        return line("FAIL", "curant-cli relay round-trip", "relay returned an empty reply.")
    line("PASS", "curant-cli produced a reply", reply[:160])
    r2 = send_imessage(to_id, reply)
    if r2.returncode == 0:
        return line("PASS", "Reply delivered via iMessage",
                    f"Texted the model's reply to {to_id} — this is the exact production path.")
    return line("FAIL", "Reply delivered via iMessage", f"osascript failed: {r2.stderr.strip()}")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", help="Where to send the test iMessage (your own number/Apple ID)")
    ap.add_argument("--from-sender", help="Apple ID/number that will text the Curant (default: --to)")
    ap.add_argument("--full", action="store_true", help="Also run curant-cli relay and deliver its reply")
    ap.add_argument("--watcher", default="curant-watcher.py")
    args = ap.parse_args()

    if sys.platform != "darwin":
        print("This preflight must run on the Mac that will host Curant (macOS). "
              f"Current platform: {sys.platform}.")
        sys.exit(2)

    print("=" * 68)
    print("curant-watcher preflight")
    print("=" * 68)
    ok = True
    ok &= check_full_disk_access()
    ok &= check_cli()
    ok &= check_customer_identity()

    if args.to:
        ok &= check_send(args.to)
        if args.full:
            sender = args.from_sender or args.to
            ok &= check_full_roundtrip(sender, args.to)
    else:
        line("SKIP", "Live test send", "Pass --to <your-number> to test sending. Pass --full for the whole loop.")

    print("=" * 68)
    print("READY — text-in/text-back should work." if ok else
          "NOT READY — fix the FAIL lines above, then re-run.")
    print("=" * 68)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
