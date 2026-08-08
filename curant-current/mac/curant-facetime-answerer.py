#!/usr/bin/env python3
"""
curant-facetime-answerer — EXPERIMENTAL. Auto-answers incoming FaceTime
calls and lets the caller talk to your Curant in real time: it clicks
"Accept" via UI automation, records the caller's voice, transcribes it,
asks curant-cli for a reply (fast model tier — a live caller is
waiting), and speaks the reply back with macOS's built-in `say`.

READ THIS BEFORE RUNNING IT.

Unlike curant-watcher.py (SQLite + AppleScript, both documented, both
verified end to end on a real Mac), this script rests on ground that is
NOT solid:

  - FaceTime has no public API and no AppleScript dictionary. Answering
    a call here means clicking a button via macOS's Accessibility APIs
    (System Events), driven by GUESSING at FaceTime's current window/
    button layout. Apple can change that layout in any macOS update and
    silently break this. The one community project that tried to solve
    this (vrunt/facetime-auto-answer) gave up on the "real" approach and
    relied on a preference key its own README says "haven't worked for
    at least 3 years and have never been officially supported by Apple."
    This script instead does the UI click for real, which is more
    likely to work but has never been run against a live FaceTime call
    — I have no macOS desktop to test it on.

  - Piping audio into and out of a FaceTime call requires a virtual
    audio driver (BlackHole) and manual device routing that only you
    can set up on your actual Mac — see SETUP_FACETIME_CALLS.md next to
    this file. Skipping that setup means this script will "answer" the
    call but neither hear nor be heard.

  - This is a "you test it live, tell me what breaks" feature, not a
    "verified working" one. Expect to iterate.

WHAT'S ACTUALLY SOLID (reused from the rest of Curant, unchanged):
  - The reply itself comes from `curant-cli relay --tier fast`, the same
    local relay every text reply goes through — persona, memory, tools,
    and all.
  - Speaking uses macOS's built-in `say`, the same free "standard" TTS
    tier curant-watcher.py already uses for voice-memo replies.
  - Transcription (STT) uses OpenAI's Whisper API — the one new external
    dependency this feature adds. Requires an OpenAI key even if your
    main provider is Anthropic or Gemini:
        curant-cli set-api-key <key> --provider openai

REQUIREMENTS
  - Full setup in SETUP_FACETIME_CALLS.md (BlackHole, per-call FaceTime
    audio device selection) done FIRST.
  - Accessibility permission granted to Terminal/python (System
    Settings > Privacy & Security > Accessibility).
  - `brew install ffmpeg switchaudio-osx`
  - An OpenAI API key set (for Whisper transcription).
  - FaceTime.app open and signed in.

USAGE
    python3 curant-facetime-answerer.py [--apple-id name@icloud.com]
                                         [--dry-run]

    --apple-id   Who calls will be treated as coming from, for
                 curant-cli's memory/persona context. Defaults to the
                 configured customer_apple_id (same config key the
                 watcher uses).
    --dry-run    Detect and log incoming calls, print what it WOULD
                 click/say, but never actually click Accept or speak.
                 Use this first to sanity-check detection before
                 letting it answer anything for real.

ACCESS CONTROL: honors the same CURANT_ACCESS_MODE the watcher uses.
In "approved" mode (default), calls are only answered if the incoming
caller ID (best-effort, read from the call window's text — FaceTime
does not expose this cleanly, so this can fail closed and refuse to
answer rather than risk answering an unrecognized caller) matches a
configured handle. In "open" mode, any call is answered.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

CONFIG_PATH = os.path.expanduser("~/.curant/config.json")

CALL_POLL_INTERVAL_SECONDS = 2
TURN_RECORD_SECONDS = 5          # length of each caller-audio recording chunk
MAX_CALL_TURNS = 60              # hard safety cap so a stuck call can't run forever

# BlackHole device names set up per SETUP_FACETIME_CALLS.md.
TTS_OUTPUT_DEVICE = "BlackHole 2ch"     # fed to FaceTime as its Microphone
CALLER_AUDIO_DEVICE = "BlackHole 16ch"  # fed FROM FaceTime's Speaker/Output


def _load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _read_customer_handles(cfg):
    """Same resolution as curant-watcher.py's _read_customer_handles, kept
    independent here rather than importing the watcher (it isn't a real
    importable module — it's a standalone script)."""
    primary = (os.environ.get("CURANT_CUSTOMER_APPLE_ID") or cfg.get("customer_apple_id") or "").strip()
    extra = os.environ.get("CURANT_CUSTOMER_HANDLES") or cfg.get("customer_handles") or ""
    extra = extra if isinstance(extra, list) else [h.strip() for h in str(extra).split(",")]
    ordered, seen = [], set()
    for h in [primary, *extra]:
        h = (h or "").strip()
        if h and h not in seen:
            seen.add(h)
            ordered.append(h)
    primary = primary or (ordered[0] if ordered else "")
    return primary, ordered


def _read_access_mode(cfg):
    mode = (os.environ.get("CURANT_ACCESS_MODE") or cfg.get("access_mode") or "approved").strip().lower()
    if mode not in ("approved", "open"):
        print(f"Unrecognized CURANT_ACCESS_MODE '{mode}' — falling back to 'approved'.", file=sys.stderr)
        mode = "approved"
    return mode


def _run_osascript(script, timeout=15):
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────
# Step 1: detect + accept an incoming call (UI automation — the fragile part)
# ─────────────────────────────────────────────────────────────────────────

DETECT_AND_DESCRIBE_SCRIPT = '''
tell application "System Events"
    if not (exists process "FaceTime") then return "no_facetime_process"
    tell process "FaceTime"
        set winNames to {}
        repeat with w in windows
            try
                set end of winNames to (name of w as string)
            end try
        end repeat
        set AppleScript's text item delimiters to "||"
        set result to winNames as string
        set AppleScript's text item delimiters to ""
        return result
    end tell
end tell
'''

ACCEPT_CALL_SCRIPT = '''
tell application "System Events"
    if not (exists process "FaceTime") then return "no_facetime_process"
    tell process "FaceTime"
        set frontmost to true
        repeat with w in windows
            try
                if exists (button "Accept" of w) then
                    click (button "Accept" of w)
                    return "accepted:" & (name of w as string)
                end if
            end try
            -- Some macOS versions expose the accept control differently
            -- (e.g. inside a group, or labeled just as an icon button
            -- with no visible text name) — this UI shape is a guess and
            -- may need adjusting on your actual Mac. Run with
            -- --dry-run and inspect the printed window/button names if
            -- this never matches.
        end repeat
    end tell
    return "no_incoming_call"
end tell
'''


def poll_for_incoming_call(dry_run):
    """Returns a description string of the incoming call window (best
    effort — often just contains window titles), or None if no call is
    ringing. Never claims certainty about caller identity."""
    r = _run_osascript(DETECT_AND_DESCRIBE_SCRIPT)
    if r.returncode != 0:
        return None
    windows = r.stdout.strip()
    if not windows or windows == "no_facetime_process":
        return None
    # Heuristic: FaceTime's incoming-call window usually isn't the main
    # window and its title often includes the caller's name/number.
    # This is unverified — inspect with --dry-run on your Mac and adjust
    # if it doesn't look right.
    return windows


def accept_call():
    r = _run_osascript(ACCEPT_CALL_SCRIPT, timeout=10)
    out = (r.stdout or "").strip()
    return out.startswith("accepted"), out


def caller_is_approved(window_desc, cfg):
    mode = _read_access_mode(cfg)
    if mode == "open":
        return True, "open access mode"
    _, handles = _read_customer_handles(cfg)
    if not handles:
        return False, "approved mode but no customer handles configured"
    for h in handles:
        if h and h.lower() in (window_desc or "").lower():
            return True, f"matched configured handle: {h}"
    return False, (
        "could not confirm caller matches a configured handle from window "
        f"text ({window_desc!r}) — refusing to answer rather than risk "
        "answering an unrecognized caller. If this is a false negative, "
        "the window-title heuristic likely needs adjusting for your macOS "
        "version (see poll_for_incoming_call)."
    )


# ─────────────────────────────────────────────────────────────────────────
# Step 2: audio in/out, once on the call
# ─────────────────────────────────────────────────────────────────────────

def set_system_output_device(device_name):
    """Requires `brew install switchaudio-osx`. Sets the TTS-injection
    device as the default system output for the duration of the call —
    see SETUP_FACETIME_CALLS.md for why this is the mechanism (afplay
    has no per-call device argument on macOS)."""
    try:
        subprocess.run(["SwitchAudioSource", "-t", "output", "-s", device_name],
                        check=True, capture_output=True, timeout=10)
        return True
    except Exception as e:
        print(f"Could not switch system output to '{device_name}': {e}. "
              f"Is switchaudio-osx installed (brew install switchaudio-osx) "
              f"and is that device name exact?", file=sys.stderr)
        return False


def speak(text):
    """Same free, local 'standard' tier as curant-watcher.py's
    _tts_macos_say — generates speech and plays it, which (per the
    SETUP_FACETIME_CALLS.md routing) goes out through BlackHole 2ch into
    FaceTime's selected Microphone."""
    fd, aiff_path = tempfile.mkstemp(suffix=".aiff")
    os.close(fd)
    try:
        subprocess.run(["say", "-o", aiff_path, text], check=True, timeout=30)
        subprocess.run(["afplay", aiff_path], check=True, timeout=60)
    finally:
        if os.path.exists(aiff_path):
            os.remove(aiff_path)


def record_caller_audio(seconds):
    """Records from CALLER_AUDIO_DEVICE (FaceTime's Speaker/Output,
    per the routing setup) using ffmpeg's avfoundation input. The exact
    device index isn't stable across Macs, so this looks it up by name
    each time rather than hardcoding an index."""
    device_index = _find_avfoundation_audio_device_index(CALLER_AUDIO_DEVICE)
    if device_index is None:
        raise RuntimeError(
            f"Could not find an audio input device named '{CALLER_AUDIO_DEVICE}'. "
            f"Is BlackHole 16ch installed? See SETUP_FACETIME_CALLS.md."
        )
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "avfoundation", "-i", f":{device_index}",
         "-t", str(seconds), "-ar", "16000", "-ac", "1", wav_path],
        check=True, capture_output=True, timeout=seconds + 15,
    )
    return wav_path


_AVFOUNDATION_DEVICE_CACHE = None


def _find_avfoundation_audio_device_index(device_name):
    global _AVFOUNDATION_DEVICE_CACHE
    if _AVFOUNDATION_DEVICE_CACHE is None:
        r = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=15,
        )
        _AVFOUNDATION_DEVICE_CACHE = r.stderr  # ffmpeg prints the device list to stderr
    in_audio_section = False
    for line in _AVFOUNDATION_DEVICE_CACHE.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio_section = True
            continue
        if in_audio_section and device_name in line:
            # Lines look like: "[0] BlackHole 16ch"
            try:
                return line.split("[")[2].split("]")[0]
            except Exception:
                return None
    return None


def transcribe(wav_path, cfg):
    """Whisper API — the one new external dependency this feature adds.
    Needs an OpenAI key regardless of which provider you use for actual
    replies (documented in the module docstring and setup guide)."""
    import requests
    api_key = (cfg.get("api_keys", {}) or {}).get("openai") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No OpenAI API key configured — needed for call transcription "
            "even if you use a different provider for replies. Run: "
            "curant-cli set-api-key <key> --provider openai"
        )
    with open(wav_path, "rb") as f:
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (os.path.basename(wav_path), f, "audio/wav")},
            data={"model": "whisper-1"},
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


def get_reply(text, apple_id):
    """Routes through curant-cli's normal relay, same persona/memory/tools
    as text — just tier='fast' since a live caller is waiting on this,
    not reading a text at their own pace."""
    r = subprocess.run(
        ["curant-cli", "relay", text, "--apple-id", apple_id, "--tier", "fast"],
        capture_output=True, text=True, timeout=45,
    )
    raw = r.stdout.strip()
    try:
        data = json.loads(raw)
    except Exception:
        raise RuntimeError(f"curant-cli relay didn't return JSON: {raw[:200]}")
    if data.get("error"):
        raise RuntimeError(f"relay error: {data['error']}")
    return data.get("reply") or ""


def hang_up():
    """Best-effort — same UI-click fragility as accepting. If this
    fails, the call is left open; log it loudly rather than silently
    leaving a customer connected to a Curant that's stopped responding."""
    script = '''
    tell application "System Events"
        tell process "FaceTime"
            repeat with w in windows
                try
                    if exists (button "Decline" of w) then
                        click (button "Decline" of w)
                        return "hung_up"
                    end if
                    if exists (button "End" of w) then
                        click (button "End" of w)
                        return "hung_up"
                    end if
                end try
            end repeat
        end tell
    end tell
    return "no_end_button_found"
    '''
    r = _run_osascript(script)
    return (r.stdout or "").strip()


# ─────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────

def handle_call(window_desc, apple_id, dry_run):
    print(f"Incoming call detected: {window_desc!r}")
    cfg = _load_config()
    approved, reason = caller_is_approved(window_desc, cfg)
    print(f"  access check: {'PASS' if approved else 'REFUSED'} — {reason}")
    if not approved:
        return

    if dry_run:
        print("  [dry-run] would click Accept now. Stopping here.")
        return

    ok, detail = accept_call()
    if not ok:
        print(f"  Failed to accept call: {detail}", file=sys.stderr)
        return
    print(f"  Accepted: {detail}")

    if not set_system_output_device(TTS_OUTPUT_DEVICE):
        print("  Continuing anyway, but TTS likely won't reach the caller "
              "until system output is fixed — see SETUP_FACETIME_CALLS.md.",
              file=sys.stderr)

    speak("Hi, this is Curant. I'm listening.")

    for turn in range(MAX_CALL_TURNS):
        try:
            wav_path = record_caller_audio(TURN_RECORD_SECONDS)
        except Exception as e:
            print(f"  Recording failed: {e}", file=sys.stderr)
            break
        try:
            text = transcribe(wav_path, cfg)
        finally:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        if not text:
            continue  # likely silence in this window — just listen again
        print(f"  Caller said: {text}")
        try:
            reply = get_reply(text, apple_id)
        except Exception as e:
            print(f"  Reply failed: {e}", file=sys.stderr)
            reply = "Sorry, I ran into a problem there — could you say that again?"
        if reply:
            print(f"  Curant says: {reply}")
            speak(reply)
        if any(word in text.lower() for word in ("bye", "goodbye", "hang up", "that's all")):
            break

    print("  Ending call.")
    print(f"  hang_up(): {hang_up()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apple-id", default=None,
                     help="Treat calls as coming from this Apple ID for curant-cli context "
                          "(defaults to the configured customer_apple_id)")
    ap.add_argument("--dry-run", action="store_true",
                     help="Detect calls and log what would happen, but never click Accept or speak")
    args = ap.parse_args()

    if sys.platform != "darwin":
        print("This must run on the Mac hosting FaceTime.", file=sys.stderr)
        sys.exit(2)

    cfg = _load_config()
    apple_id = args.apple_id or _read_customer_handles(cfg)[0]
    if not apple_id:
        print("No apple-id available (pass --apple-id or set customer_apple_id in "
              "~/.curant/config.json). Refusing to start.", file=sys.stderr)
        sys.exit(1)

    print("curant-facetime-answerer starting (EXPERIMENTAL — see module docstring).")
    print(f"  mode: {'DRY RUN (no answering, no speaking)' if args.dry_run else 'LIVE'}")
    print(f"  apple_id for replies: {apple_id}")
    print(f"  access mode: {_read_access_mode(cfg)}")

    while True:
        try:
            window_desc = poll_for_incoming_call(args.dry_run)
            if window_desc:
                handle_call(window_desc, apple_id, args.dry_run)
        except Exception as e:
            print(f"Unexpected error in poll loop (continuing): {e}", file=sys.stderr)
        time.sleep(CALL_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
