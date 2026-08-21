#!/opt/homebrew/opt/python@3.12/bin/python3.12
"""
curant-facetime-answerer — EXPERIMENTAL. Auto-answers incoming FaceTime
AUDIO calls and lets the caller talk to your Curant in real time: it
clicks "Accept" via UI automation, records the caller's voice,
transcribes it, asks curant-cli for a reply (fast model tier — a live
caller is waiting), and speaks the reply back with macOS's built-in
`say`.

DELIBERATELY TARGETS FACETIME AUDIO CALLS, NOT VIDEO. Confirmed against
Apple's own FaceTime User Guide and a real live test call: audio-only
calls get a different, more limited menu bar item ("Audio" — just Mic
Mode/Voice Isolation) than video calls ("Video" — full camera/
microphone/output device picker). Video calls' Video menu could
override system defaults per-call; audio calls have no such picker, so
this script instead points macOS's SYSTEM default input/output devices
at BlackHole automatically (see set_system_input_device/
set_system_output_device and handle_call()) — there's no manual
per-call FaceTime menu step anymore, which is actually simpler than the
video-call approach this used to assume. If a caller instead places a
FaceTime VIDEO call, detection/accept should still work (the banner
looks the same), but the audio routing here has NOT been verified for
that case and may need the old manual Video-menu approach instead.

READ THIS BEFORE RUNNING IT.

Unlike curant-watcher.py (SQLite + AppleScript, both documented, both
verified end to end on a real Mac), this rests on ground that live
testing proved is NOT solid — here's exactly what was tried and what
was actually found, on the real Mac this runs on, not guessed:

  - FaceTime.app never runs for an incoming call — confirmed via `ps
    aux`: it's handled entirely by background daemons
    (FTConversationService, facetimemessagestored, identityservicesd,
    callservicesd) plus a system call-banner UI. `open -a FaceTime`
    while a call rings opens FaceTime's Recents window, not an
    answerable in-call screen — a dead end, ruled out by testing.

  - The banner IS reliably detectable — a window named "Notification
    Center" appears in NotificationCenter's window list only while it's
    up (confirmed with and without a live call). But its internal
    controls are NOT reachable via Accessibility scripting at all:
    `entire contents` of that window bottoms out five levels of nested,
    unlabeled AXGroups deep with zero buttons or text exposed. This
    looks like Apple's modern Live-Activity-style rendering, which
    doesn't populate the standard Accessibility tree the way older
    AppKit notification banners did — not a wrong-guess-fixable problem,
    a genuine platform restriction.

  - So acceptance here is VISUAL instead: screenshot the screen, find
    the green accept button by color+position, synthesize a click at
    that point (see accept_call()). This is real automation, but
    meaningfully more fragile than Accessibility-based clicking would
    have been — it breaks if the banner appears in a different position,
    a second notification stacks above it, the display resolution
    changes, or a macOS visual update changes the button's look. A
    hardcoded coordinate fallback exists for when visual detection
    itself misses (CURANT_FACETIME_ACCEPT_XY).

  - REGRESSION: caller-ID verification for calls is NOT implemented.
    The banner's caller-ID text is visible as rendered pixels but not
    exposed to Accessibility scripting either, so there's currently no
    way to check an incoming caller against your configured handles
    before answering. "approved" mode (the safe default) therefore
    refuses to auto-answer ANY call right now — see caller_is_approved().
    Only "open" mode (explicit, already used the same way by the text
    watcher) will actually answer calls, and it answers ALL of them.

  - Piping audio into and out of the call still requires the BlackHole
    virtual-audio setup in SETUP_FACETIME_CALLS.md. Skipping it means
    this script "answers" the call but neither hears nor is heard.

  - This is a "you test it live, tell me what breaks" feature, not a
    "verified working" one. Expect to keep iterating.

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
  - Call detection (is a call ringing at all) is a cheap, confirmed-
    reliable window-list check — see poll_for_incoming_call().

REQUIREMENTS
  - Full setup in SETUP_FACETIME_CALLS.md (BlackHole + switchaudio-osx)
    done FIRST. No manual per-call FaceTime device selection needed —
    this script switches the system default input/output automatically
    (see the audio-routing note above); switchaudio-osx is what makes
    that possible.
  - Accessibility permission granted to Terminal/python (System
    Settings > Privacy & Security > Accessibility) — needed for both
    detection (System Events) and synthesized clicks (cliclick).
  - Screen Recording permission granted to Terminal/python (System
    Settings > Privacy & Security > Screen Recording) — needed for
    accept_call()'s screenshot; without it, screenshots come back black
    instead of erroring, which looks like a detection bug but isn't.
  - `brew install ffmpeg switchaudio-osx cliclick`
  - `pip3 install pillow --break-system-packages` (screenshot analysis)
  - `brew install tesseract` and `pip3 install pytesseract --break-system-packages`
    (OCR caller-ID reading for "approved" mode — see caller_is_approved()).
    Without these, "approved" mode fails closed on every call rather than
    answering unverified — "open" mode still works with no OCR at all.
  - An OpenAI API key set (for Whisper transcription).
  - FaceTime.app signed in (does not need to be open — see above).

USAGE
    python3 curant-facetime-answerer.py [--apple-id name@icloud.com]
                                         [--dry-run]

    --apple-id   Who calls will be treated as coming from, for
                 curant-cli's memory/persona context. Defaults to the
                 configured customer_apple_id (same config key the
                 watcher uses).
    --dry-run    Detect incoming calls and log what it WOULD do, but
                 never actually click Accept or speak. Use this first
                 to sanity-check detection before letting it answer
                 anything for real.

ACCESS CONTROL: uses its OWN access-mode setting, CURANT_CALL_ACCESS_MODE
(config key "call_access_mode") -- deliberately separate from
curant-watcher.py's CURANT_TEXT_ACCESS_MODE, after a real incident where
sharing one switch between calls and texts meant opening call access for
testing silently opened text access to everyone too (see
_read_call_access_mode()'s docstring). "approved" mode now verifies callers
via screenshot + OCR against configured handles (see caller_is_approved()
and _ocr_caller_matches_customer()) — this is a real check, but the crop
region it reads from is UNVERIFIED against a real live call (see
CURANT_FACETIME_CALLERID_REGION if it needs tuning); "open" mode still
answers every call with no verification at all, same as before.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import math
import re
import shutil  # module-level now -- a later LOCAL 'import shutil' inside
                     # handle_call() would make every 'shutil' reference in that whole function
                     # resolve as local (Python scopes by static analysis of the function body),
                     # breaking the EARLIER shutil.copy2 use added for saving failing
                     # transcription clips with an UnboundLocalError.

def _ts():
    """Wall-clock timestamp (local time, millisecond precision) prefixed
    onto the key lifecycle prints below. Added after live debugging where
    figuring out exactly how many seconds elapsed between 'Accepted' and
    a call dropping required manually counting terminal scrollback --
    this makes that a direct read instead of a guess."""
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


CONFIG_PATH = os.path.expanduser("~/.curant/config.json")

CALL_POLL_INTERVAL_SECONDS = 2
TURN_RECORD_SECONDS = 1.0
# LOWERED 3.0 -> 1.0 (2026-08-21 latency pass). This is safe ONLY
# because _UtteranceBuffer now exists, and it changes what this
# constant even means.
#
# BEFORE endpointing, this was a TURN boundary: whatever landed in one
# window was transcribed alone, so a short window sliced words in half
# and produced empty transcripts (that's why it went 5 -> 3 -> 2 -> and
# back to 3 over three separate sessions -- each move traded latency
# against accuracy, and no value could win both).
#
# NOW it is only the ENDPOINTING GRANULARITY. Consecutive speech
# segments are concatenated into one utterance before transcription
# (see _UtteranceBuffer), so a word split across a boundary is
# rejoined before Gemini ever sees it -- segment length no longer
# affects transcription quality at all. What it DOES affect is how
# fast a pause is noticed, and that was the single largest remaining
# source of dead air in the whole call:
#
#   at 3.0s: caller stops -> up to 3s to close the in-flight segment
#            -> another 3s for a silent segment to confirm the pause
#            => ~4.5-6.0s of silence before transcription even starts
#   at 1.0s: ~0.5s to close + 1.0s to confirm
#            => ~1.5-2.0s
#
# ~3-4 seconds saved on every single turn, with no accuracy tradeoff,
# which is why this is a different decision from the earlier attempts
# rather than a repeat of one. Cost is more (smaller) files and more
# per-segment RMS checks -- both cheap, in-memory, no extra API calls.
RECORDING_FAILURE_RETRY_SECONDS = 1  # brief pause before re-checking call state after a failed recording

# Real bug fixed live: "sometimes the call cuts, and it still thinks
# it's the old call." Root cause -- handle_call()'s inner loop has NO
# hangup detection at all (deliberate, see the loop's own comments:
# every AppleScript-based connectivity check was removed earlier
# because polling FaceTime's process/window state too often was
# suspected of contributing to calls dropping in the first place). So
# when a call genuinely ends, this function never returns -- it just
# keeps recording/checking silent segments forever, which ALSO means
# main()'s outer poll loop never gets control back to notice a real
# NEW incoming call, because the process is stuck inside handle_call()
# for the dead one.
#
# Fix uses a signal that's already being computed every turn anyway --
# no new AppleScript calls, no new risk of the disconnect bug this was
# built around. Real live data from today's debugging: every turn of a
# genuinely CONNECTED call (even a quiet one, nobody talking) measured
# RMS in the ~7-18 range -- real, nonzero room-tone noise floor. Every
# turn of a call that was NOT actually real (the notification-banner
# false-positive bug, fixed separately today) measured EXACT RMS=0.0 on
# every channel, every single time. That's a strong, already-observed
# distinguishing signal: true near-zero RMS sustained over several
# consecutive turns means there's no real audio source at all anymore
# -- either the call ended, or was never a real call to begin with.
TRUE_SILENCE_RMS_THRESHOLD = 1.0  # at/below this = no real audio source, not just a quiet room
# TIME-BASED, not a raw segment count. It used to be a hardcoded "6
# turns", which silently meant ~18s at 3s segments -- and would have
# meant ~6s once segments dropped to 1s, i.e. hanging up on anyone who
# paused to think for six seconds. Expressing the real intent (how
# long of TRUE digital silence proves there's no call anymore) in
# seconds and deriving the count keeps this correct no matter what
# TURN_RECORD_SECONDS becomes later.
HANGUP_TRUE_SILENCE_SECONDS = 18.0
HANGUP_CONSECUTIVE_TRUE_SILENT_TURNS = max(
    3, int(math.ceil(HANGUP_TRUE_SILENCE_SECONDS / TURN_RECORD_SECONDS)))

# NOTE: there is deliberately no MAX_CALL_TURNS anymore. Per explicit
# direction, Curant must never be the one to end a call — only the human
# can, by hanging up on their own end. handle_call()'s loop is unbounded
# and exits only when _facetime_is_frontmost() reports the call has
# already ended; see handle_call() for the full reasoning.

# BlackHole device names set up per SETUP_FACETIME_CALLS.md.
TTS_OUTPUT_DEVICE = "BlackHole 2ch"     # fed to FaceTime as its Microphone
CALLER_AUDIO_DEVICE = "BlackHole 16ch"  # what this script CAPTURES caller audio from (always this)

# What gets set as the SYSTEM's default output device -- separate from
# CALLER_AUDIO_DEVICE above. Real bug found live: setting bare
# "BlackHole 16ch" as the system output produced genuine, total digital
# silence (RMS=0.0 across ALL 16 channels, confirmed live) on every
# single captured turn, even during a call confirmed connected and live
# (screenshot showed FaceTime's own audio waveform moving) with
# BlackHole 16ch confirmed as the selected output device the whole time
# (System Settings > Sound, also screenshotted). FaceTime's call audio
# apparently doesn't render into a BARE virtual loopback device the way
# ordinary app audio does. The standard fix is a macOS Multi-Output
# Device (Audio MIDI Setup -> + -> Create Multi-Output Device, checking
# both BlackHole 16ch and your real speakers/headphones) -- pointing
# system output at THAT combined device instead of bare BlackHole 16ch,
# while still capturing from BlackHole 16ch specifically (which mirrors
# whatever audio the combined device receives). Set this env var to
# whatever you named that Multi-Output Device; defaults to
# CALLER_AUDIO_DEVICE (the old, confirmed-broken-for-FaceTime behavior)
# if unset, purely for backward compatibility.
SYSTEM_OUTPUT_DEVICE = os.environ.get("CURANT_FACETIME_SYSTEM_OUTPUT_DEVICE", CALLER_AUDIO_DEVICE)


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


def _read_call_access_mode(cfg):
    """DELIBERATELY a separate key (CURANT_CALL_ACCESS_MODE / config
    "call_access_mode") from curant-watcher.py's text access mode
    (CURANT_TEXT_ACCESS_MODE / "text_access_mode") -- these used to share
    one CURANT_ACCESS_MODE / "access_mode" key, and a real incident
    confirmed why that was unsafe: setting it to "open" here (needed
    because 'approved' mode can't verify caller ID at all yet -- see
    caller_is_approved()) also silently opened the TEXT watcher to
    answering literally anyone who texted the customer's real number,
    with no separate signal that had happened. Two independent keys
    means testing/enabling one feature's open access can never again
    silently do the same to the other."""
    mode = (os.environ.get("CURANT_CALL_ACCESS_MODE") or cfg.get("call_access_mode") or "approved").strip().lower()
    if mode not in ("approved", "open"):
        print(f"Unrecognized CURANT_CALL_ACCESS_MODE '{mode}' — falling back to 'approved'.", file=sys.stderr)
        mode = "approved"
    return mode


def _run_osascript(script, timeout=15):
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────
# Step 1: detect + accept an incoming call
#
# Live-tested on the real Mac this was built for (not guessed):
#   - FaceTime.app never actually runs for an incoming call — confirmed via
#     `ps aux`, it's handled entirely by background daemons
#     (FTConversationService, facetimemessagestored, identityservicesd,
#     callservicesd) plus a system call-banner UI.
#   - `open -a FaceTime` while a call rings opens FaceTime's normal Recents
#     window, NOT an answerable in-call screen — dead end, don't use it.
#   - DETECTION works: `System Events` can enumerate NotificationCenter's
#     windows cheaply, and a window literally named "Notification Center"
#     appears in that list only while the call banner is on screen (checked
#     both with and without a live call — confirmed the difference).
#   - ACCEPTING via Accessibility does NOT work: `entire contents` of that
#     window bottoms out five levels of nested, unlabeled AXGroups deep with
#     zero buttons, static text, or any other leaf UI element exposed. This
#     is Apple's modern Live-Activity-style call banner, and its internal
#     controls are not exposed to third-party Accessibility scripting at
#     all — not a wrong-guess-fixable problem.
#
# So acceptance here is VISUAL instead: screenshot the screen, look for the
# banner's green accept-call button by color+position, and synthesize a
# click at that point. A hardcoded coordinate fallback exists for when
# visual detection misses — see CURANT_FACETIME_ACCEPT_XY below.
# ─────────────────────────────────────────────────────────────────────────

def poll_for_incoming_call(dry_run):
    """Cheap, confirmed-reliable detection: does NotificationCenter's
    window list currently include a window named "Notification Center"?
    Caveat, genuinely unverified beyond FaceTime: this may also go true
    for OTHER kinds of notifications (Slack, Calendar, etc.), not just
    calls — only tested against a real FaceTime call so far. The
    corroborating checks below narrow this back down to a real FaceTime
    call specifically.

    REVERTED: this briefly had a guard here that skipped detection
    entirely whenever _call_is_still_connected() (FaceTime.app process
    existence) reported "connected," meant to stop a stale banner from
    being mistaken for a second incoming call. Live testing (no call
    placed at all) found that guard was actually unsafe to keep: this
    Mac's FaceTime.app can sit open in the background for unrelated
    reasons (confirmed via `ps aux` — it had been running for over two
    hours with no call in progress), and FTConversationService can also
    stay resident well beyond any single call. "FaceTime process
    exists" is NOT a reliable signal that a call is connected — it also
    made this function permanently return None (never detect a real
    incoming call) for as long as FaceTime.app happened to be open, and
    spammed the log every 2 seconds with _call_is_still_connected()'s
    diagnostic print. The actual phantom-second-click bug this was
    meant to guard against is fixed at its real source instead — see
    _click_and_verify()'s docstring — so this extra guard was removed
    rather than papered over with a better process check.

    ADDED (real bug, confirmed live): the two checks below this
    docstring used to be the ONLY gate, and they weren't enough --
    after extended call testing left FTConversationService resident in
    the background (per the note above, it "can stay resident well
    beyond any single call"), sending a plain text message to this Mac
    produced a Messages notification banner, which ALSO satisfies the
    "Notification Center window" check, and with the stale daemon still
    running, _facetime_call_daemon_active() ALSO returned True -- the
    combination false-positived as "incoming call," blindly clicked the
    cached Accept coordinate (nothing real there), played a greeting
    into a call that didn't exist, and every subsequent turn read
    RMS=0.0 forever because there was never any real caller audio to
    capture. This is almost certainly what several "silent call" test
    runs actually were, not a real audio-routing regression.

    Fix: require a THIRD, genuinely call-specific signal before
    treating this as a real incoming call -- the same visual
    template-match already used to actually click Accept
    (_find_accept_button_visually) must ALSO find the real green Accept
    button on screen right now. A Messages banner (or any other
    notification) will never match that template, regardless of
    whatever FaceTime-adjacent processes happen to still be resident.

    REMOVED (real bug, confirmed live on a newer macOS release than
    this was originally built against): the second gate used to also
    require _facetime_call_daemon_active() (pgrep -f
    "FTConversationService"). On this Mac, that process -- and every
    other FaceTime-notification-adjacent process (FaceTimeNotification
    Extension, FaceTimeNotificationViewBridgeService,
    FaceTimeNotificationService, facetimemessagestored) -- is resident
    in the background AT ALL TIMES, confirmed by comparing `ps aux`
    output taken during a real ringing call against a quiet baseline
    with no call at all: identical process list both times. So on this
    macOS version the daemon check is permanently true regardless of
    call state, which made it a no-op at best and, since it was a
    required AND condition, a permanent hard block on ever detecting a
    real call at worst -- every single poll returned None even with the
    Accept button genuinely visible on screen. Dropped rather than
    replaced with a guess at a "new correct" process name, since
    everything observed resident here is equally unreliable as a
    call-specific signal on this OS version. The visual Accept-button
    match below is left as the sole discriminating signal beyond the
    window check -- it's the one that's actually been confirmed to only
    go true when a real call banner is on screen (see its own
    docstring), so the false-positive protection this whole function
    exists for is still intact without the daemon check."""
    r = _run_osascript(
        'tell application "System Events" to tell process "NotificationCenter" to get name of every window'
    )
    if r.returncode != 0:
        return None
    names = [n.strip() for n in (r.stdout or "").split(",")]
    if "Notification Center" not in names:
        return None
    if not _accept_button_visible_now():
        return None  # a banner is up, but no real Accept button on screen -- not a call
    return "FaceTime call banner active (NotificationCenter window + visible Accept button)"


def _accept_button_visible_now():
    """Third corroborating signal for poll_for_incoming_call() -- takes a
    fresh screenshot and runs the SAME template match accept_call() uses
    to actually click Accept, just to confirm the real button is
    genuinely on screen right now before this is trusted as a call at
    all. See poll_for_incoming_call()'s docstring for the false-positive
    (text notification mistaken for a call) this exists to prevent.
    Fails safe: any error here (screenshot/template load failure) is
    treated as "not visible" -- i.e. NOT a call -- rather than risking a
    false accept on an error."""
    try:
        screenshot_path = _capture_screenshot()
        try:
            return _find_accept_button_visually(screenshot_path) is not None
        finally:
            if os.path.exists(screenshot_path):
                os.remove(screenshot_path)
    except Exception as e:
        print(f"  [{_ts()}] Could not check for a visible Accept button ({e}) -- "
              f"treating as no call detected, not risking a false accept.", file=sys.stderr)
        return False


def _facetime_call_daemon_active():
    """Corroborating signal alongside the window check — confirmed via
    `ps aux` that com.apple.FaceTime.FTConversationService only showed up
    while a call was actively ringing. Reduces false-positives from
    unrelated notifications triggering poll_for_incoming_call()."""
    try:
        r = subprocess.run(["pgrep", "-f", "FTConversationService"],
                            capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip())
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────
# Caller-ID verification via OCR -- closes the REGRESSION noted in
# caller_is_approved()'s docstring below: Accessibility scripting cannot
# read any text out of the call banner at all (confirmed by testing --
# same finding that forced accept_call() over to visual template
# matching), so there was previously no way to identify who's calling at
# all, and 'approved' mode refused every single call as a result.
#
# A different approach was considered and deliberately NOT taken: reading
# the caller's info out of macOS's own notification store directly (the
# usernoted database some versions of macOS keep under
# ~/Library/Group Containers/group.com.apple.usernoted/) instead of a
# screenshot. Rejected because that format is private, undocumented, has
# changed across macOS versions before, and -- critically -- there's no
# live Mac available in the environment building this to actually verify
# it works or find the right schema/query; shipping a guess at an
# undocumented private database format is worse than being honest that
# it's unverified. A screenshot + OCR is comparatively simple, uses only
# public APIs (screencapture, already used elsewhere in this file, plus
# an OCR library), and is the same approach this file's own prior
# docstring already flagged as the real fix -- see caller_is_approved().
#
# REAL CAVEAT, stated plainly per this file's own house rule of not
# hiding what's untested: the exact screen region the caller's name/
# number renders in has NOT been confirmed against a live incoming call
# (no live Mac available while building this) -- it starts from the same
# region already confirmed to contain the Accept button
# (_SEARCH_REGION_FRACTION), on the reasoning that the caller-ID text
# lives in the same banner, but this needs a real call to verify. If it
# doesn't work first try, CURANT_FACETIME_CALLERID_REGION lets you
# override the crop without touching code -- see below.
CALLERID_OCR_REGION_ENV = "CURANT_FACETIME_CALLERID_REGION"  # e.g. "0.55,1.0,0.0,0.30"


def _callerid_search_region_fraction():
    """Override via CURANT_FACETIME_CALLERID_REGION="x0,x1,y0,y1" (same
    fraction-of-screen-size format as _SEARCH_REGION_FRACTION) if the
    default (reusing the Accept button's own confirmed search region)
    turns out not to actually contain the caller's name/number on a
    real call."""
    override = os.environ.get(CALLERID_OCR_REGION_ENV)
    if override:
        try:
            parts = tuple(float(x) for x in override.split(","))
            if len(parts) == 4:
                return parts
        except Exception:
            print(f"  {CALLERID_OCR_REGION_ENV} isn't valid 'x0,x1,y0,y1' — ignoring, using the default region.",
                  file=sys.stderr)
    return _SEARCH_REGION_FRACTION  # same region already confirmed to contain the Accept button/banner


# Direct call-end detection via OCR, added per explicit request to replace
# the previous ~18-27s silence-timeout-only detection with something much
# faster (target: ~2s). macOS shows a system HUD banner reading "<name>
# left" when the other party disconnects (visible top-of-screen, same
# general area as the incoming-call banner/caller-ID) -- this reuses the
# EXACT SAME technique already proven reliable for caller-ID matching
# (screencapture + pytesseract), deliberately NOT AppleScript/Accessibility.
#
# WHY NOT AppleScript, stated plainly: an earlier, different attempt at
# hangup detection (_call_is_still_connected(), see its own docstring)
# used `osascript`/System Events queries AGAINST THE FACETIME PROCESS
# ITSELF, repeatedly, during a live call -- and that specific pattern was
# the prime suspect behind a real, confirmed call-dropping regression
# (FaceTime's wantsCallDisconnectionOnInvalidation=YES behavior reacting
# to rapid process churn touching it). This check is mechanistically
# different: screencapture + tesseract never touch FaceTime's own process
# or Accessibility tree at all, only raw screen pixels -- reducing but,
# without a live call to confirm against, NOT proving that risk is zero.
# Runs on its own throttled background thread (CALL_END_POLL_SECONDS),
# not inline in the per-turn audio loop, specifically so it can't add
# latency to the actual conversation even if a check takes a moment.
#
# UNVERIFIED, stated per this file's house rule: the default region below
# is a first guess (a wide top strip) since there's no live Mac available
# while writing this to confirm exactly where the "<name> left" banner
# renders. Override with CURANT_FACETIME_CALLEND_REGION="x0,x1,y0,y1"
# (same format as CURANT_FACETIME_CALLERID_REGION) if the default doesn't
# actually catch it on a real call -- the raw OCR text is printed on every
# check specifically so a bad region shows up immediately in the logs
# instead of silently never firing.
CALL_END_OCR_REGION_ENV = "CURANT_FACETIME_CALLEND_REGION"
CALL_END_POLL_SECONDS = 2.0  # how often the background thread checks -- target end-to-end detection latency


def _call_end_search_region_fraction():
    override = os.environ.get(CALL_END_OCR_REGION_ENV)
    if override:
        try:
            parts = tuple(float(x) for x in override.split(","))
            if len(parts) == 4:
                return parts
        except Exception:
            print(f"  {CALL_END_OCR_REGION_ENV} isn't valid 'x0,x1,y0,y1' — ignoring, using the default region.",
                  file=sys.stderr)
    return (0.0, 1.0, 0.0, 0.12)  # wide top strip, first guess -- see comment above


def _raw_call_end_region_text():
    """Just the OCR read of the call-end region, no 'left' matching --
    factored out so both the baseline capture and the live check use
    the exact same code path. Returns (text_or_None, err_or_None)."""
    screenshot_path = _capture_screenshot()
    try:
        return _ocr_text_from_screenshot(screenshot_path, _call_end_search_region_fraction())
    finally:
        if os.path.exists(screenshot_path):
            os.remove(screenshot_path)


def _call_end_banner_detected(baseline_text=None):
    """Fails safe: any error here (screenshot/OCR failure) returns False
    -- i.e. NOT ended -- same fail-safe direction as _accept_button_
    visible_now(), since a false "ended" here would mean silently
    abandoning a caller who's still on the line.

    BASELINE-EXCLUSION, added after a real, confirmed-live false
    positive: the call-end region is a wide top strip of the whole
    screen (see CALL_END_OCR_REGION_ENV's comment -- deliberately wide
    since the exact banner position was never confirmed against a real
    Mac before writing this). On a dev machine, that strip can easily
    include a Terminal window with OLD "<number> left" text sitting in
    its own scrollback from an earlier successful call -- confirmed
    live: this matched stale Terminal text and hung up a real,
    still-active call mid-conversation, nowhere near an actual FaceTime
    hangup banner.

    Fix: baseline_text is a snapshot of this exact region taken once,
    right after a call is accepted (see handle_call()), before any real
    hangup could possibly have happened yet. A later "left" match only
    counts if the region's OCR text has actually CHANGED since that
    baseline -- static stale text (like Terminal scrollback that never
    scrolls during a silent call) stays byte-identical to the baseline
    and is correctly ignored forever, while a real banner appearing
    changes what's on screen and is still caught immediately. If no
    baseline was captured (baseline_text=None), falls back to the old,
    less careful behavior -- any "left" match counts -- rather than
    silently never detecting real hangups."""
    try:
        text, err = _raw_call_end_region_text()
        if err:
            print(f"  [{_ts()}] [call-end OCR] {err} -- treating as not ended.", file=sys.stderr)
            return False
        if not text or not re.search(r"\bleft\b", text, re.IGNORECASE):
            return False
        if baseline_text is not None and text == baseline_text:
            # Same "left" was already sitting on screen before this call
            # even started (e.g. stale Terminal scrollback) -- nothing
            # has actually changed, so this is not a real hangup event.
            return False
        print(f"  [{_ts()}] [call-end OCR] matched \"left\" in: {text!r}"
              f"{' (baseline had no match)' if baseline_text is not None else ''}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"  [{_ts()}] [call-end OCR] check failed ({e}) -- treating as not ended.", file=sys.stderr)
        return False


def _watch_for_call_end(stop_event, baseline_text=None):
    """Runs on its own daemon thread for the lifetime of one call (see
    handle_call()). Polls _call_end_banner_detected() every
    CALL_END_POLL_SECONDS and sets stop_event the moment it fires --
    handle_call()'s main loop and _wait_for_next_turn_segment() both
    check stop_event to react within about one poll tick, rather than
    waiting for the old silence-timeout to build up over many turns.

    baseline_text is threaded straight through to _call_end_banner_
    detected() on every check -- see that function's docstring for why
    this is needed (stale on-screen "left" text producing a false
    hangup)."""
    while not stop_event.is_set():
        if _call_end_banner_detected(baseline_text=baseline_text):
            stop_event.set()
            return
        stop_event.wait(CALL_END_POLL_SECONDS)


def _ocr_text_from_screenshot(screenshot_path, region_fraction):
    """
    Crops the given region out of a full screenshot and runs OCR on it
    via pytesseract (Tesseract under the hood) -- lazy-imported, same
    pattern as curant-watcher.py's whisper/pypdf handling, so this file
    doesn't hard-require the OCR stack just to run unrelated commands.
    Returns (text_or_None, error_or_None) -- error is always a short,
    actionable string (missing dependency, or the real exception),
    never a raw traceback.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return None, ("OCR dependencies aren't installed — run: brew install tesseract && "
                       "pip3 install pytesseract --break-system-packages")
    try:
        img = Image.open(screenshot_path).convert("RGB")
        width, height = img.size
        xf0, xf1, yf0, yf1 = region_fraction
        x0, x1 = int(width * xf0), int(width * xf1)
        y0, y1 = int(height * yf0), int(height * yf1)
        region = img.crop((x0, y0, x1, y1))
        text = pytesseract.image_to_string(region)
        return text.strip(), None
    except Exception as e:
        return None, f"OCR failed: {e}"


def _normalize_digits(s):
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _ocr_caller_matches_customer(ocr_text, customer_handles):
    """
    Matches OCR'd banner text against configured handles via SUFFIX
    matching on digits only, not exact equality -- deliberate: a
    caller-ID display doesn't always show a country code, and OCR can
    drop a leading '+', but the actual subscriber number (the last
    7-10 digits) is what's diagnostic regardless of formatting.

    Compares per LINE rather than across the whole OCR blob, and that
    matters: a formatted number like "+1 (614) 717-8753" tokenizes into
    separate short digit runs ("1", "614", "717", "8753") if you
    naively regex for contiguous digits anywhere in the text, and none
    of those fragments is long enough to match on its own -- caught by
    the synthetic-image smoke test that motivated this fix. Stripping
    digits per LINE (not per contiguous run) fixes that, while still
    avoiding concatenating digits across DIFFERENT lines, which would
    let two unrelated short numbers stitch together into a false match.

    Returns (matched: bool, matched_handle_or_None).
    """
    if not ocr_text:
        return False, None
    handle_digit_pairs = [(h, _normalize_digits(h)) for h in customer_handles if _normalize_digits(h)]
    handle_digit_pairs = [(h, d) for h, d in handle_digit_pairs if len(d) >= 7]
    if not handle_digit_pairs:
        return False, None
    for line in ocr_text.splitlines():
        line_digits = _normalize_digits(line)
        if len(line_digits) < 7:
            continue
        for original_handle, handle_digits in handle_digit_pairs:
            if line_digits.endswith(handle_digits[-7:]) or handle_digits.endswith(line_digits[-7:]):
                return True, original_handle
    return False, None


# Set once you've found the real coordinates by hovering your mouse over
# the actual Accept button during a live call and running `cliclick p`
# (prints current mouse position in point coordinates) — used only if
# visual detection below fails to locate the button itself.
ACCEPT_BUTTON_FALLBACK_XY_ENV = "CURANT_FACETIME_ACCEPT_XY"  # e.g. "1900,140"

# Real reference image of the actual Accept button, cropped from a live
# screenshot on the Mac this runs on (not a mockup) — see accept_call()'s
# docstring for how matching against it works. If FaceTime's button design
# ever changes, replace this file with a fresh crop.
ACCEPT_BUTTON_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "assets", "facetime_accept_button.png")

# Downsample factor for template matching — big enough to make the sliding-
# window search fast on a full-res screenshot, small enough that the
# button's shape/color pattern still survives clearly. Verified: on a
# 122x108 real button crop, a true match scores ~74 (normalized SSD) vs.
# ~6600 for no match at all — roughly 90x separation, so this threshold has
# a lot of margin, not a hair-trigger cutoff.
_TEMPLATE_MATCH_DOWNSAMPLE = 4
_TEMPLATE_MATCH_SSD_THRESHOLD = 800.0  # normalized SSD (per pixel-channel); see above
_SEARCH_REGION_FRACTION = (0.55, 1.0, 0.0, 0.30)  # (x0, x1, y0, y1) as fraction of screen size

_display_scale_cache = None
_template_cache = None


def _capture_screenshot():
    """Requires Screen Recording permission granted to whatever runs this
    (System Settings > Privacy & Security > Screen Recording) — without
    it, screencapture silently produces a black or permission-prompt
    image instead of erroring, so a black-looking result usually means
    this permission, not a code bug."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    subprocess.run(["screencapture", "-x", path], check=True, timeout=10)
    return path


def _display_scale_factor(screenshot_width):
    """screencapture saves at actual pixel resolution (2x on Retina
    displays), but click coordinates (cliclick, CGEvent) are in logical
    'point' space — this converts between them. Cached after first call
    since it won't change mid-run.

    CHANGED after live testing: the original system_profiler text-parse
    approach was a likely culprit for clicks landing in the wrong place
    (found live — a template match reported as correct still didn't
    dismiss the call banner, and the click was visibly off to the
    side). system_profiler's "Resolution:" line isn't guaranteed to be
    the MAIN display, or in the exact format expected, and there's no
    guarantee screencapture's un-targeted default output matches
    whichever line the regex happened to grab. Finder's own desktop
    window bounds are a much more direct, main-display-specific source
    of the logical point resolution."""
    global _display_scale_cache
    if _display_scale_cache is not None:
        return _display_scale_cache
    logical_w = None
    try:
        r = subprocess.run(
            ["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=10,
        )
        # Output looks like: "0, 0, 1470, 956" (left, top, right, bottom) in POINTS
        parts = [p.strip() for p in (r.stdout or "").split(",")]
        if len(parts) == 4:
            logical_w = int(parts[2]) - int(parts[0])
    except Exception:
        logical_w = None

    if not logical_w:
        # Fallback to the old text-parse approach only if Finder's bounds
        # couldn't be read at all.
        try:
            r = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                                capture_output=True, text=True, timeout=15)
            import re
            m = re.search(r"Resolution:\s*(\d+)\s*x\s*(\d+)", r.stdout or "")
            logical_w = int(m.group(1)) if m else screenshot_width
        except Exception:
            logical_w = screenshot_width

    _display_scale_cache = (screenshot_width / logical_w) if logical_w else 1.0
    print(f"  [scale] screenshot width={screenshot_width}px, logical width={logical_w}pt, "
          f"scale factor={_display_scale_cache:.3f}", file=sys.stderr)
    return _display_scale_cache


def _load_accept_button_template():
    """Loads and downsamples the real reference image once, cached for
    the life of the process."""
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    import numpy as np
    from PIL import Image

    if not os.path.exists(ACCEPT_BUTTON_TEMPLATE_PATH):
        raise RuntimeError(f"Accept-button template image missing: {ACCEPT_BUTTON_TEMPLATE_PATH}")
    tmpl = Image.open(ACCEPT_BUTTON_TEMPLATE_PATH).convert("RGB")
    small = tmpl.resize((max(1, tmpl.width // _TEMPLATE_MATCH_DOWNSAMPLE),
                          max(1, tmpl.height // _TEMPLATE_MATCH_DOWNSAMPLE)))
    _template_cache = (np.asarray(small, dtype=np.float32), tmpl.width, tmpl.height)
    return _template_cache


def _find_accept_button_visually(screenshot_path):
    """
    Returns (x, y) in POINT coordinates (already scale-corrected) of the
    detected accept button's center, or None if nothing matched with
    confidence. Real template matching against an actual screenshot crop
    of the button (assets/facetime_accept_button.png) — not a guessed
    color range. Confirmed on synthetic test data: a true match scores
    ~74 (normalized sum-of-squared-differences) vs. ~6600 for a clean
    no-match case, so _TEMPLATE_MATCH_SSD_THRESHOLD has wide margin.

    Real fallback for the fact that Accessibility scripting cannot see
    inside the call banner at all (see module-level comment above) — this
    only has to work well enough to be worth trying before
    ACCEPT_BUTTON_FALLBACK_XY_ENV's hardcoded-coordinate fallback.
    """
    import numpy as np
    from PIL import Image

    template_small, tmpl_w, tmpl_h = _load_accept_button_template()
    th, tw = template_small.shape[:2]

    img = Image.open(screenshot_path).convert("RGB")
    width, height = img.size
    xf0, xf1, yf0, yf1 = _SEARCH_REGION_FRACTION
    x0, x1 = int(width * xf0), int(width * xf1)
    y0, y1 = int(height * yf0), int(height * yf1)

    region = img.crop((x0, y0, x1, y1))
    region_small = region.resize((max(1, region.width // _TEMPLATE_MATCH_DOWNSAMPLE),
                                   max(1, region.height // _TEMPLATE_MATCH_DOWNSAMPLE)))
    region_arr = np.asarray(region_small, dtype=np.float32)

    if region_arr.shape[0] < th or region_arr.shape[1] < tw:
        return None  # search region smaller than the template — nothing to match

    windows = np.lib.stride_tricks.sliding_window_view(region_arr, (th, tw, 3))[:, :, 0, :, :, :]
    diff = windows - template_small
    ssd = np.sum(diff * diff, axis=(2, 3, 4))
    best_idx = np.unravel_index(np.argmin(ssd), ssd.shape)
    n_values = th * tw * 3
    best_score = float(ssd[best_idx]) / n_values

    if best_score > _TEMPLATE_MATCH_SSD_THRESHOLD:
        return None  # best candidate still isn't a confident match

    # best_idx is the template's top-left corner in downsampled region
    # coords — convert to full-res screenshot coords, centered on the
    # template's own real size (not the downsampled one).
    match_top_left_x = x0 + best_idx[1] * _TEMPLATE_MATCH_DOWNSAMPLE
    match_top_left_y = y0 + best_idx[0] * _TEMPLATE_MATCH_DOWNSAMPLE
    center_x = match_top_left_x + tmpl_w / 2
    center_y = match_top_left_y + tmpl_h / 2

    scale = _display_scale_factor(width)
    return round(center_x / scale), round(center_y / scale)


def _click_at(x_point, y_point):
    """Requires `brew install cliclick`. Synthesizes a real mouse click
    at the given point coordinates — this is the mechanism for both the
    visual-detection path and the hardcoded fallback."""
    subprocess.run(["cliclick", f"c:{x_point},{y_point}"], check=True, timeout=10)


CLICK_CACHE_PATH = os.path.expanduser("~/.curant/facetime_accept_click_cache.json")
CLICK_VERIFY_WAIT_SECONDS = 1.5      # how long to wait after a click before the FIRST check
CLICK_VERIFY_RECHECK_SECONDS = 1.0   # how long to wait between re-checks if still reading "ringing"
CLICK_VERIFY_MAX_WAIT_SECONDS = 5.0  # total time to keep re-checking before concluding the click failed —
                                      # live testing showed a genuinely successful click can still read
                                      # "still ringing" for a couple seconds after CLICK_VERIFY_WAIT_SECONDS
                                      # alone, so this gives the banner/daemon teardown more time to
                                      # actually finish before giving up and (previously) triggering a
                                      # phantom second click.
MAX_ACCEPT_ATTEMPTS = 6              # hard cap so a persistently-wrong click can't loop forever


def _load_click_cache():
    try:
        with open(CLICK_CACHE_PATH) as f:
            data = json.load(f)
        return tuple(data["xy"])
    except Exception:
        return None


def _save_click_cache(xy):
    try:
        os.makedirs(os.path.dirname(CLICK_CACHE_PATH), exist_ok=True)
        with open(CLICK_CACHE_PATH, "w") as f:
            json.dump({"xy": list(xy)}, f)
    except Exception as e:
        print(f"  Could not save click cache (non-fatal): {e}", file=sys.stderr)


def _call_still_ringing():
    """Same signal poll_for_incoming_call() uses, called again after a
    click to check whether it actually worked — if the banner and call
    daemon are both gone, the click landed on something real."""
    r = _run_osascript(
        'tell application "System Events" to tell process "NotificationCenter" to get name of every window'
    )
    if r.returncode != 0:
        return False  # can't tell — assume it's gone rather than retry forever on an error
    names = [n.strip() for n in (r.stdout or "").split(",")]
    return "Notification Center" in names and _facetime_call_daemon_active()


def _facetime_is_frontmost():
    """Second, independent positive signal that a call was actually
    answered: FaceTime.app only becomes the frontmost app once truly
    in-call (confirmed live — its menu bar appears then, not before).
    Checked BEFORE any retry click, because trusting only
    _call_still_ringing()'s single reading caused a real, reported bug:
    a first click that actually worked but got read as "still ringing"
    a moment too early led to a SECOND click landing on the new in-call
    screen instead of the (now-gone) banner — hitting whatever control
    happened to be at that point on the in-call UI, including hanging
    up a call that had just connected. Checking this first lets the
    retry loop stop immediately instead of clicking blind into a
    changed screen."""
    r = _run_osascript(
        'tell application "System Events" to get name of first process whose frontmost is true'
    )
    return r.returncode == 0 and (r.stdout or "").strip() == "FaceTime"


def _click_and_verify(x_point, y_point, attempt_label):
    """Clicks, waits, then checks whether the call actually got answered
    — not just whether cliclick exited 0. A "successful" click that
    doesn't move the actual call state is exactly the failure mode found
    in live testing (logged as accepted, banner never went away).

    CHANGED after a real, confirmed-live bug found via a user-submitted
    screen recording: this used to check _facetime_is_frontmost() first.
    That was wrong for FaceTime AUDIO calls specifically — the in-call UI
    is a small floating overlay/toolbar, not a real focused app window,
    so FaceTime.app never actually becomes "frontmost" for an audio
    call. That made this always fall through to _call_still_ringing(),
    whose banner/daemon signals were seen live to stay stale for a
    couple seconds into an already-connected call — so a click that
    genuinely worked (confirmed via the recording: FaceTime's own call
    timer was already at 0:01/0:02) still got reported as "still
    ringing, click did not work." That false failure made accept_call()
    return False, which made handle_call() give up and poll_for_incoming_
    call() detect a phantom second "incoming call" a couple seconds
    later — leading to a SECOND click at the same cached coordinate,
    which by then was sitting on the in-call toolbar's End Call button
    instead of the (long gone) Accept button, hanging up a call that had
    already connected successfully.

    REVERTED the _call_is_still_connected() swap tried next: three
    consecutive live test calls showed that check's underlying signal
    (whether FaceTime.app has become a real process) is NOT reliable —
    it read 'NO_PROCESS' the entire time in one run, even while a real
    accept + greeting was happening, and 'PROCESS_NO_WINDOWS' (process
    exists) in an earlier run at the equivalent moment. Since
    _call_is_still_connected() was also just changed to treat ANY
    non-error read as "still connected" (see its own docstring), reusing
    it here would make every single click report "success" immediately,
    even a total miss — worse than what it replaced.

    Back to _call_still_ringing() — the one signal that, across every
    real test call logged so far, has EVENTUALLY always correctly read
    "not ringing" once a click truly worked. The actual problem was
    never that signal being wrong, only READING it too early: one
    single check at CLICK_VERIFY_WAIT_SECONDS (1.5s) could catch the
    banner/daemon mid-teardown and misreport "still ringing" for a click
    that had, in fact, worked — which is what caused the phantom second
    click in the first place. Fix: keep re-checking for up to
    CLICK_VERIFY_MAX_WAIT_SECONDS total before giving up, instead of
    trusting one early read."""
    try:
        _click_at(x_point, y_point)
        print(f"  [{_ts()}] [{attempt_label}] clicked ({x_point},{y_point})", file=sys.stderr)
    except Exception as e:
        print(f"  [{_ts()}] [{attempt_label}] click at ({x_point},{y_point}) failed to execute: {e}", file=sys.stderr)
        return False

    deadline = time.monotonic() + CLICK_VERIFY_MAX_WAIT_SECONDS
    time.sleep(CLICK_VERIFY_WAIT_SECONDS)
    while True:
        still_ringing = _call_still_ringing()
        if not still_ringing:
            print(f"  [{_ts()}] [{attempt_label}] verified — banner gone, click worked",
                  file=sys.stderr)
            return True
        if time.monotonic() >= deadline:
            print(f"  [{_ts()}] [{attempt_label}] verified — "
                  f"still ringing after {CLICK_VERIFY_MAX_WAIT_SECONDS}s, click did not work",
                  file=sys.stderr)
            return False
        time.sleep(CLICK_VERIFY_RECHECK_SECONDS)


def _local_search_offsets(center, radii=(0, 15, 30, 50)):
    """Small expanding-radius grid around a point, for when a click at
    the 'right' coordinate still doesn't register — covers the
    possibility that detection/scale math is slightly off rather than
    completely wrong. Ordered nearest-first."""
    cx, cy = center
    seen = set()
    for r in radii:
        if r == 0:
            offsets = [(0, 0)]
        else:
            offsets = [(r, 0), (-r, 0), (0, r), (0, -r), (r, r), (-r, -r), (r, -r), (-r, r)]
        for dx, dy in offsets:
            point = (cx + dx, cy + dy)
            if point not in seen:
                seen.add(point)
                yield point


def accept_call():
    """
    SIMPLIFIED per explicit direction, after the verify-retry approach
    was suspected of contributing to call instability: click ONCE and
    trust it, no _click_and_verify() retry loop, no repeated
    _call_still_ringing() checks. Every one of those checks is its own
    osascript invocation -- same reasoning as the connectivity-polling
    cut in _wait_for_next_turn_segment (see its docstring): more
    Accessibility-scripting traffic during a call is more chances to
    trip whatever's causing FaceTime to auto-disconnect. This also
    fully removes the original phantom-second-click bug class (a
    misread verification triggering a second click at a now-relocated
    button) simply by never attempting a second click at all.

    Real tradeoff, not hidden: a click that genuinely misses is no
    longer self-corrected at runtime (no retry, no local-search jitter,
    no falling through to visual detection after a cache miss's one
    try). If the cached coordinate goes stale, fix it with
    CURANT_FACETIME_ACCEPT_XY or by clearing the cache file and
    re-testing live, not by expecting this function to recover on its
    own.

    Order of attempts (tries multiple SOURCES, but only ever clicks
    once, on the first one that has a coordinate available):
      1. A previously-successful coordinate, if cached on disk.
      2. Fresh visual template matching, if no cache.
      3. The hardcoded ACCEPT_BUTTON_FALLBACK_XY_ENV coordinate, if set
         and steps 1-2 found nothing.
    """
    cached = _load_click_cache()
    if cached:
        _click_at(*cached)
        print(f"  [{_ts()}] Clicked cached coordinate {cached} (single click, unverified).",
              file=sys.stderr)
        return True, f"clicked cached coordinate {cached} (unverified)"

    screenshot_path = _capture_screenshot()
    try:
        found = _find_accept_button_visually(screenshot_path)
    finally:
        os.remove(screenshot_path)

    if found:
        _click_at(*found)
        _save_click_cache(found)
        print(f"  [{_ts()}] Clicked visually-detected coordinate {found} (single click, unverified).",
              file=sys.stderr)
        return True, f"clicked visually-detected coordinate {found} (unverified)"

    fallback = os.environ.get(ACCEPT_BUTTON_FALLBACK_XY_ENV)
    if fallback:
        try:
            x_str, y_str = fallback.split(",")
            fallback_xy = (int(x_str), int(y_str))
        except Exception:
            return False, f"{ACCEPT_BUTTON_FALLBACK_XY_ENV} ({fallback}) is not valid 'x,y'"
        _click_at(*fallback_xy)
        _save_click_cache(fallback_xy)
        print(f"  [{_ts()}] Clicked fallback coordinate {fallback_xy} (single click, unverified).",
              file=sys.stderr)
        return True, f"clicked fallback coordinate {fallback_xy} (unverified)"

    return False, (
        f"no coordinate available to click — no cache, visual detection found nothing, and "
        f"{ACCEPT_BUTTON_FALLBACK_XY_ENV} isn't set. Hover your mouse over the real Accept "
        f"button during a live call and run `cliclick p` to get coordinates, then export "
        f"{ACCEPT_BUTTON_FALLBACK_XY_ENV}=\"x,y\""
    )


def caller_is_approved(window_desc, cfg):
    """
    Real caller-ID verification for 'approved' mode, via screenshot +
    OCR -- see the module-level comment above _callerid_search_region_fraction
    for why OCR (not Accessibility scripting, which genuinely cannot see
    inside the banner at all -- confirmed by testing) and not a
    notification-database read. STILL UNVERIFIED against a real live
    call (no live Mac available while building this) -- see that same
    comment for the honest caveat and the CURANT_FACETIME_CALLERID_REGION
    escape hatch if the default crop region turns out wrong.

    Fails CLOSED at every step: no configured handles, a screenshot
    failure, a missing OCR dependency, or OCR finding no matching digit
    sequence all result in REFUSING the call, never answering an
    unverified one. Only 'open' mode (explicit, no-allowlist, same
    pattern as the text watcher's CURANT_TEXT_ACCESS_MODE=open) skips
    verification entirely.
    """
    mode = _read_call_access_mode(cfg)
    if mode == "open":
        return True, "open call access mode (caller ID verification not implemented for calls)"

    _primary, customer_handles = _read_customer_handles(cfg)
    if not customer_handles:
        return False, (
            "approved mode requires verifying the caller against a configured handle, but none is "
            "set (CURANT_CUSTOMER_APPLE_ID / CURANT_CUSTOMER_HANDLES / config.json) — refusing rather "
            "than answering an unverified caller."
        )

    try:
        screenshot_path = _capture_screenshot()
    except Exception as e:
        return False, f"approved mode requires verifying the caller, but the screenshot needed for OCR failed: {e}"

    try:
        region = _callerid_search_region_fraction()
        ocr_text, ocr_error = _ocr_text_from_screenshot(screenshot_path, region)
    finally:
        if os.path.exists(screenshot_path):
            os.remove(screenshot_path)

    if ocr_error:
        return False, (
            f"approved mode requires verifying the caller via OCR, but OCR isn't available right now "
            f"({ocr_error}) — refusing rather than answering an unverified caller. Set "
            f"CURANT_CALL_ACCESS_MODE=open to bypass verification entirely (answers ANY call, no "
            f"allowlist), or install the OCR dependency above to restore real verification."
        )

    matched, matched_handle = _ocr_caller_matches_customer(ocr_text, customer_handles)
    if matched:
        return True, f"caller ID OCR matched configured handle {matched_handle}"

    return False, (
        f"approved mode requires verifying the caller — OCR read the call banner but found no digit "
        f"sequence matching any configured handle ({', '.join(customer_handles)}). Raw OCR text: "
        f"{ocr_text!r}. This means either the caller genuinely isn't approved, OR (this is UNVERIFIED "
        f"against a real call) the crop region needs tuning — set CURANT_FACETIME_CALLERID_REGION="
        f'"x0,x1,y0,y1" if the raw OCR text above looks like it missed the actual caller-ID text entirely.'
    )


# ─────────────────────────────────────────────────────────────────────────
# Step 2: audio in/out, once on the call
# ─────────────────────────────────────────────────────────────────────────

SWITCH_SETTLE_SECONDS = 0.35  # brief pause after a CoreAudio device switch before using it


def set_system_output_device(device_name):
    """Requires `brew install switchaudio-osx`. Sets the SYSTEM default
    output device — the mechanism this whole script relies on for BOTH
    directions of audio, for a reason specific to FaceTime AUDIO calls
    (confirmed live, not assumed): unlike FaceTime VIDEO calls, an
    audio-only call's menu bar shows an "Audio" menu with only Mic Mode
    (Voice Isolation/Wide Spectrum) — no per-call camera/microphone/
    output device picker at all (confirmed against Apple's own FaceTime
    User Guide). So there is no per-call "select BlackHole" step
    possible for audio calls; FaceTime falls back to whatever the
    SYSTEM's default input/output devices are, same as its documented
    fallback when nothing is explicitly chosen. This function (and its
    counterpart set_system_input_device below) is what handle_call()
    uses to point that system-wide default at BlackHole devices
    automatically, entirely replacing what used to be a manual FaceTime
    Video-menu step for video calls."""
    try:
        subprocess.run(["SwitchAudioSource", "-t", "output", "-s", device_name],
                        check=True, capture_output=True, timeout=10)
        time.sleep(SWITCH_SETTLE_SECONDS)
        return True
    except Exception as e:
        print(f"Could not switch system output to '{device_name}': {e}. "
              f"Is switchaudio-osx installed (brew install switchaudio-osx) "
              f"and is that device name exact?", file=sys.stderr)
        return False


def set_system_input_device(device_name):
    """Counterpart to set_system_output_device — sets the SYSTEM default
    INPUT device. Set once per call (not per-turn like output): FaceTime
    Audio has no per-call microphone picker, so it uses whatever the
    system default input is for the whole call. Pointing this at
    TTS_OUTPUT_DEVICE (BlackHole 2ch) is what lets FaceTime treat
    Curant's synthesized speech as if it were the real microphone input
    — BlackHole loops whatever is played to its output side back out as
    its input side, so anything afplay'd while system output is also
    BlackHole 2ch becomes available here."""
    try:
        subprocess.run(["SwitchAudioSource", "-t", "input", "-s", device_name],
                        check=True, capture_output=True, timeout=10)
        time.sleep(SWITCH_SETTLE_SECONDS)
        return True
    except Exception as e:
        print(f"Could not switch system input to '{device_name}': {e}. "
              f"Is switchaudio-osx installed (brew install switchaudio-osx) "
              f"and is that device name exact?", file=sys.stderr)
        return False


def _get_current_device(device_type):
    """Read-only counterpart to set_system_{input,output}_device() --
    returns the current default device name for device_type ('input' or
    'output'), or None on failure. Used to remember what the Mac's
    devices were BEFORE Curant ever touched them, so they can be
    restored after a call ends rather than guessing at a hardcoded
    'MacBook speakers' name that might not match every Mac."""
    try:
        r = subprocess.run(["SwitchAudioSource", "-t", device_type, "-c"],
                            capture_output=True, text=True, timeout=10)
        name = (r.stdout or "").strip()
        return name or None
    except Exception as e:
        print(f"Could not read current system {device_type} device: {e}", file=sys.stderr)
        return None


def _list_output_devices():
    """All output device names known to CoreAudio, via SwitchAudioSource.
    Returns [] on failure -- callers treat an empty list as "couldn't
    check", never as "there are no devices"."""
    try:
        r = subprocess.run(["SwitchAudioSource", "-a", "-t", "output"],
                           capture_output=True, text=True, timeout=10)
        return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    except Exception as e:
        print(f"  Could not enumerate output devices: {e}", file=sys.stderr)
        return None


def audit_output_devices():
    """Diagnoses the single most damaging misconfiguration this whole
    feature has: FaceTime rendering call audio into a DIFFERENT output
    device than the one this script captures from.

    REAL EVIDENCE (2026-08-21, user screenshot of a live call): the
    FaceTime call window's own audio-route control read "Multi-Output
    Device" with an active level meter, while `SwitchAudioSource -t
    output -c` simultaneously reported "Curant Call Output" as the
    system default. Those are two different devices. FaceTime was
    happily playing the caller into one of them; this script was
    capturing BlackHole 16ch, which is only fed by the OTHER one. That
    is exactly consistent with the symptom that resisted every other
    fix attempted that night: RMS reading EXACTLY 0.0 (not a low noise
    floor -- literally no signal at all) on every turn of several
    consecutive calls, on a setup where a plain `say` test captured
    through the very same BlackHole 16ch worked perfectly moments
    before and after.

    "Multi-Output Device" is also the DEFAULT NAME macOS assigns a
    newly-created Multi-Output Device in Audio MIDI Setup. Rebuilding
    that device (done several times while debugging) creates a fresh
    one under that default name, and a rename that doesn't take -- or
    an old device left behind -- leaves the Mac with two similar
    devices where FaceTime remembers the wrong one.

    This function doesn't try to fix that automatically (silently
    reassigning a customer's audio devices mid-call is exactly the kind
    of surprise this file has been burned by before). It reports,
    loudly and specifically, so the failure is diagnosable in one
    glance at the log instead of another night of guessing."""
    devices = _list_output_devices()
    if not devices:
        return
    stray = [d for d in devices
             if d != SYSTEM_OUTPUT_DEVICE and "multi-output" in d.lower()]
    if SYSTEM_OUTPUT_DEVICE not in devices:
        print(f"  AUDIO CONFIG WARNING: the configured capture output device "
              f"{SYSTEM_OUTPUT_DEVICE!r} does not exist on this Mac. Available output "
              f"devices: {devices}. Calls will be silent until this matches a real "
              f"Multi-Output Device that includes {CALLER_AUDIO_DEVICE!r}.", file=sys.stderr)
    if stray:
        print(f"  AUDIO CONFIG WARNING: found {len(stray)} other Multi-Output device(s) "
              f"besides {SYSTEM_OUTPUT_DEVICE!r}: {stray}. FaceTime picks its own audio "
              f"route and does NOT always follow the system default -- if a call is "
              f"silent (RMS exactly 0.0 every turn), open the FaceTime call window's "
              f"audio control and check which device it names. If it names one of "
              f"{stray}, either delete that device in Audio MIDI Setup so FaceTime "
              f"can't choose it, or switch the call's route to "
              f"{SYSTEM_OUTPUT_DEVICE!r}.", file=sys.stderr)


AUDIO_SELFTEST_TONE_SECONDS = 0.6
AUDIO_SELFTEST_ENV = "CURANT_FACETIME_SKIP_AUDIO_SELFTEST"


def audio_capture_selftest():
    """Proves -- before the call depends on it -- whether audio played
    into the system output actually reaches CALLER_AUDIO_DEVICE, by
    playing a real tone and capturing it back.

    Why this exists: every silent-call failure this feature has hit
    looks IDENTICAL from the logs (turn after turn of "Clip looked
    silent") whether the cause is (a) the caller genuinely saying
    nothing, (b) the Multi-Output Device being misconfigured, (c)
    FaceTime routing to a different device entirely, or (d) the system
    default having been changed out from under us by some other process
    opening the device. Those need completely different fixes and the
    logs alone have never distinguished them -- which is precisely why
    debugging this burned an entire night of live test calls, each one
    testing one guess.

    This collapses all of that into a single yes/no answer, logged once
    per call, before the caller has said anything: if the tone comes
    back, the capture path is proven working end to end and any later
    silence is genuinely the caller being quiet. If it doesn't, the
    path is broken and the log says so explicitly instead of
    accumulating misleading "looked silent" lines.

    Returns (ok: bool, detail: str). Never raises -- a self-test that
    itself fails must not stop a real call from being answered, so any
    internal error returns ok=True with an explanatory detail (fail
    OPEN: an inconclusive test is not evidence of breakage)."""
    if os.environ.get(AUDIO_SELFTEST_ENV) == "1":
        return True, "skipped (CURANT_FACETIME_SKIP_AUDIO_SELFTEST=1)"
    device_index = None
    tmp_wav = None
    try:
        device_index = _find_avfoundation_audio_device_index(CALLER_AUDIO_DEVICE)
        if device_index is None:
            return True, f"skipped ({CALLER_AUDIO_DEVICE} not found as a capture device)"
        fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        # Capture first, tone second -- the capture process needs to be
        # live before the tone plays or the tone is simply missed. sox's
        # synth generates the tone directly into the system output
        # device (same path speak() uses for real replies), so this
        # exercises the ACTUAL production audio route, not a simulation
        # of it.
        cap = subprocess.Popen(
            ["ffmpeg", "-y", "-f", "avfoundation", "-i", f":{device_index}",
             "-t", str(AUDIO_SELFTEST_TONE_SECONDS + 0.9), "-ar", "16000", tmp_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.45)  # let avfoundation actually open the device before the tone starts
        if _sox_available():
            subprocess.run(
                ["sox", "-n", "-t", "coreaudio", SYSTEM_OUTPUT_DEVICE,
                 "synth", str(AUDIO_SELFTEST_TONE_SECONDS), "sine", "440", "vol", "0.35"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        else:
            subprocess.run(["afplay", "/System/Library/Sounds/Ping.aiff"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        try:
            cap.wait(timeout=8)
        except Exception:
            cap.kill()
        mono = _extract_loudest_channel_mono(tmp_wav)
        try:
            _has, rms = _wav_has_speech(mono, threshold=SILENCE_RMS_THRESHOLD)
        finally:
            if mono != tmp_wav and os.path.exists(mono):
                os.remove(mono)
        if rms is None:
            return True, "inconclusive (could not measure the captured tone)"
        if rms <= TRUE_SILENCE_RMS_THRESHOLD:
            return False, (f"NO SIGNAL (captured RMS {rms:.1f}) -- a tone played into "
                           f"{SYSTEM_OUTPUT_DEVICE!r} did not reach {CALLER_AUDIO_DEVICE!r} at all")
        return True, f"OK (captured tone at RMS {rms:.1f})"
    except Exception as e:
        return True, f"inconclusive (self-test error: {e})"
    finally:
        if tmp_wav and os.path.exists(tmp_wav):
            os.remove(tmp_wav)


TTS_VOICE = "Samantha"  # explicit `say` voice -- see speak()'s docstring for why this is required,
                        # not optional. CHANGED from "Alex" (2026-08-21): live-checked via
                        # `say -v '?'` and "Alex" is not actually in the list of voices
                        # installed on this Mac at all -- it may have been removed/never
                        # downloaded since the original bug report that picked it (Alex was a
                        # known-good, explicitly-tested voice AT THE TIME, not a permanent
                        # guarantee). Switched to Samantha -- confirmed present in the same
                        # `say -v '?'` listing, and matches what a live no-flag `say` test
                        # sounded like (no explicit SelectedVoiceName is set on this Mac in
                        # either com.apple.speech.voice.prefs or
                        # com.apple.speech.synthesis.general.prefs, so Samantha is almost
                        # certainly macOS's own built-in fallback here). Still explicit, not a
                        # bare `say` call -- see the docstring below for why that matters.

_SOX_AVAILABLE = None  # cached shutil.which("sox") result


def _sox_available():
    global _SOX_AVAILABLE
    if _SOX_AVAILABLE is None:
        import shutil
        _SOX_AVAILABLE = shutil.which("sox") is not None
    return _SOX_AVAILABLE


SPEAK_INTERRUPT_POLL_SECONDS = 0.2  # how often speak() checks interrupt_check() while playback runs --
# LOWERED from 0.5 (latency pass): also a cheap check (proc.poll() +
# an in-memory RMS comparison, no network), tightening this shaves the
# average end-of-playback detection delay AND makes barge-in react
# faster as a side benefit.
SPEAK_MAX_SECONDS = 60  # hard backstop so a stuck playback process can't hang the call forever


def speak(text, device_name=None, interrupt_check=None, prerendered_aiff=None):
    """Same free, local 'standard' tier as curant-watcher.py's
    _tts_macos_say — generates speech and plays it into BlackHole 2ch so
    FaceTime picks it up as the caller-facing "microphone" (see
    set_system_input_device's docstring for why that loopback works).

    Uses SoX (`sox file -t coreaudio "<device>"`) to target that device
    DIRECTLY, deliberately NOT via the system default output. Real bug
    found live: switching the SYSTEM default output mid-call (even after
    the call is already answered) reliably cut the call -- the same
    class of problem input hot-swapping caused, just for output. SoX's
    explicit device targeting means playback never touches the system
    default at all, so there is nothing left to hot-swap once the call
    is live -- output can now stay fixed at CALLER_AUDIO_DEVICE for the
    ENTIRE session (set once at startup, see main()) with zero further
    switching, for either direction, ever.

    Falls back to the old afplay+system-output approach ONLY if SoX
    isn't installed, with a loud warning -- that path is known to risk
    dropping the call and is a stopgap, not a fix, if you land here.

    ALWAYS passes -v TTS_VOICE explicitly -- real bug found live: with
    no -v flag, `say` silently falls back to whatever this Mac's
    "default" voice is, and on this Mac that default is broken --
    confirmed via a standalone `say -o file text` test producing a
    4332-byte near-empty AIFF (versus ~118KB for a real ~2s sentence)
    with no error at all, while `defaults read
    com.apple.speech.voice.prefs SelectedVoiceName` showed no voice was
    even explicitly set. `say -v Alex` on the same text produced a
    normal ~118KB file. Since `say` doesn't error on this failure mode
    (sox and afplay both happily "play" the near-silent result without
    complaint), this was invisible except by comparing file sizes --
    every greeting and reply was likely going out silent or near-silent
    until this was pinned down. Explicitly naming a known-good voice
    sidesteps whatever is wrong with this Mac's actual default.

    interrupt_check, if given, is a zero-arg callable polled every
    SPEAK_INTERRUPT_POLL_SECONDS while playback is running (barge-in
    support, added per explicit request -- previously nothing the caller
    said could stop a reply already in progress, no matter how long it
    ran). If it returns truthy, playback is terminated immediately and
    speak() returns True (interrupted) instead of blocking to the end.
    Playback now always runs via Popen (not subprocess.run) so it CAN be
    terminated mid-way -- with interrupt_check=None (e.g. the initial
    greeting) this just polls in a tight loop until it finishes on its
    own, same effective behavior as the old blocking call.

    prerendered_aiff, if given and still present on disk, skips the
    `say` synthesis step entirely and plays that file instead. Used for
    fixed, known-in-advance lines (the greeting) so their audio is
    generated once at startup rather than re-synthesized while a caller
    is already connected and waiting -- see GREETING_TEXT.

    Returns True if playback was interrupted, False if it played to
    completion (or interrupt_check was never given)."""
    reuse = bool(prerendered_aiff) and os.path.exists(prerendered_aiff)
    if reuse:
        aiff_path = prerendered_aiff
    else:
        fd, aiff_path = tempfile.mkstemp(suffix=".aiff")
        os.close(fd)
    interrupted = False
    try:
        if not reuse:
            subprocess.run(["say", "-v", TTS_VOICE, "-o", aiff_path, text], check=True, timeout=30)
        # `say` exits 0 and produces a well-formed but near-empty AIFF on
        # this failure mode -- no exception to catch. Bytes-per-character
        # is a crude but effective tripwire: a real sentence runs roughly
        # 1-2KB/char at this sample rate; the broken default voice
        # produced ~30 bytes/char total regardless of text length. This
        # doesn't fix a bad voice, it just makes a silent/near-silent
        # greeting or reply loud in the logs instead of invisible.
        actual_bytes = os.path.getsize(aiff_path)
        expected_min_bytes = max(2000, len(text) * 200)
        if actual_bytes < expected_min_bytes:
            print(f"  WARNING: TTS output for voice '{TTS_VOICE}' looks suspiciously small "
                  f"({actual_bytes} bytes for {len(text)} chars of text, expected at least "
                  f"~{expected_min_bytes}) -- this may play as silence or near-silence. "
                  f"Check `say -v {TTS_VOICE} -o /tmp/t.aiff \"test\"` manually.", file=sys.stderr)
        target_device = device_name or TTS_OUTPUT_DEVICE
        if _sox_available():
            cmd = ["sox", aiff_path, "-t", "coreaudio", target_device]
        else:
            print("  sox not found (brew install sox) — falling back to afplay via "
                  "system default output, which can drop a live call if the system "
                  "default isn't already this device. Install sox to fix properly.",
                  file=sys.stderr)
            cmd = ["afplay", aiff_path]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + SPEAK_MAX_SECONDS
        while proc.poll() is None:
            if time.monotonic() > deadline:
                print(f"  [{_ts()}] speak(): playback exceeded {SPEAK_MAX_SECONDS}s -- killing it "
                      f"rather than hanging the call.", file=sys.stderr)
                proc.kill()
                break
            if interrupt_check is not None and interrupt_check():
                interrupted = True
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
                break
            time.sleep(SPEAK_INTERRUPT_POLL_SECONDS)
    finally:
        # NEVER delete a pre-rendered file -- it's a cache owned by the
        # caller and reused for the life of the process. Only temp files
        # this call created are cleaned up here.
        if not reuse and os.path.exists(aiff_path):
            os.remove(aiff_path)
    return interrupted


GREETING_TEXT = "Hi, this is Curant. I'm listening."
_GREETING_AIFF = None


def prerender_greeting():
    """Synthesizes the fixed greeting ONCE, at startup, so answering a
    call doesn't pay for TTS synthesis while the caller is already on
    the line.

    The greeting is the only line in the whole call that is known ahead
    of time, and it sits at the most latency-sensitive moment there is:
    the caller has just been connected and is listening to silence.
    `say` synthesis of this sentence measured ~112KB of AIFF and takes
    a few hundred milliseconds -- small in isolation, but it is pure
    dead air at the exact moment a human decides whether the thing that
    picked up is broken.

    Best-effort: on any failure this returns None and speak() falls
    back to synthesizing normally, so a broken cache can never stop a
    call from being greeted."""
    global _GREETING_AIFF
    try:
        fd, path = tempfile.mkstemp(prefix="curant_greeting_", suffix=".aiff")
        os.close(fd)
        subprocess.run(["say", "-v", TTS_VOICE, "-o", path, GREETING_TEXT],
                       check=True, timeout=30)
        size = os.path.getsize(path)
        if size < max(2000, len(GREETING_TEXT) * 200):
            # Same near-empty-AIFF tripwire speak() uses -- catching it
            # here means a broken voice is reported once at startup
            # instead of once per call.
            print(f"  WARNING: pre-rendered greeting looks near-empty ({size} bytes) for voice "
                  f"{TTS_VOICE!r} -- not caching it; speak() will synthesize per call.",
                  file=sys.stderr)
            os.remove(path)
            return None
        _GREETING_AIFF = path
        return path
    except Exception as e:
        print(f"  Could not pre-render the greeting ({e}) -- it will be synthesized per call.",
              file=sys.stderr)
        return None


def record_caller_audio(seconds):
    """DEPRECATED for the live-call loop -- see _start_continuous_capture()
    below for why. Kept only because it's a simple, self-contained way to
    grab one clip (useful for standalone debugging/manual testing), but
    handle_call() no longer calls this per-turn.

    Records from CALLER_AUDIO_DEVICE (FaceTime's Speaker/Output, per the
    routing setup) using ffmpeg's avfoundation input. The exact device
    index isn't stable across Macs, so this looks it up by name each
    time rather than hardcoding an index."""
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


SEGMENT_WAIT_POLL_SECONDS = 0.05  # how often to check whether the next segment file has appeared --
# LOWERED 0.5 -> 0.2 -> 0.05 across successive latency passes. This is
# a bare os.path.exists() on a local temp file: no network, no
# subprocess, no API. At 1s segments this is now polled ~20x per
# segment and still costs microseconds of CPU per check, while cutting
# the average pure-wait overhead from ~0.1s to ~0.025s per turn. Small
# on its own; it matters because it sits in series with every other
# per-turn delay and there are now ~3x more segments per turn than
# there were at 3s.
SEGMENT_WAIT_TIMEOUT_SECONDS = 20  # give up waiting for a turn's segment after this long (should
                                    # normally appear within ~TURN_RECORD_SECONDS of the previous one)


def _start_continuous_capture(seconds_per_segment):
    """Opens CALLER_AUDIO_DEVICE via ffmpeg's avfoundation input EXACTLY
    ONCE for the entire call, and lets ffmpeg's own segment muxer split
    the continuous stream into rolling per-turn WAV files -- instead of
    the old approach (record_caller_audio(), called fresh every single
    turn), which opened and closed a brand-new capture session on that
    SAME device roughly every 5-7 seconds for the whole call.

    Real suspicion behind this change, not yet 100% proven but the
    strongest remaining lead after ruling out device-switching (already
    fixed, see set_system_input_device/set_system_output_device) and the
    click-verification race (already fixed, see _click_and_verify): live
    testing showed a call still dropping at an unpredictable point in
    the first ~10-15s while turns kept recording (and reading as
    'silent') for over two minutes afterward with no crash -- meaning
    the SCRIPT never noticed the real call had ended, which also means
    the terminal logs alone can no longer prove or disprove this timing
    correlation. Repeatedly opening/closing a capture session on a
    device FaceTime is actively rendering INTO, mid-call, is the same
    class of problem that repeatedly switching system default devices
    mid-call already turned out to be -- this closes that same kind of
    gap for capture, the one remaining piece of the audio pipeline that
    still touched the device more than once per call.

    Returns (process, segment_dir, pattern) -- caller is responsible for
    calling _stop_continuous_capture(process) when the call ends, and
    for cleaning up segment_dir."""
    device_index = _find_avfoundation_audio_device_index(CALLER_AUDIO_DEVICE)
    if device_index is None:
        raise RuntimeError(
            f"Could not find an audio input device named '{CALLER_AUDIO_DEVICE}'. "
            f"Is BlackHole 16ch installed? See SETUP_FACETIME_CALLS.md."
        )
    segment_dir = tempfile.mkdtemp(prefix="curant_facetime_turns_")
    pattern = os.path.join(segment_dir, "turn_%05d.wav")
    # CHANGED after a real, confirmed-live bug: this used to request
    # "-ac 1" here, letting ffmpeg downmix BlackHole 16ch's 16 channels
    # to mono at capture time. A real call held connected for 4+ minutes
    # with the caller speaking clearly the whole time, and EVERY turn
    # still measured as near-total digital silence (RMS ~1, peak ~10-15
    # out of a possible 32768) -- with System Settings > Sound confirmed
    # showing BlackHole 16ch genuinely selected as the output device the
    # whole time (ruled out via a live screenshot), so the audio has to
    # be landing on SOME channel of the 16. ffmpeg's default mono-downmix
    # from a 16-channel avfoundation source doesn't reliably grab it.
    # Fix: capture ALL 16 channels raw and untouched here (no -ac flag
    # at all, so avfoundation's native channel count is preserved), then
    # _extract_loudest_channel_mono() picks whichever channel actually
    # has signal on it, per turn, after the fact.
    process = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "avfoundation", "-i", f":{device_index}",
         "-ar", "16000",
         "-f", "segment", "-segment_time", str(seconds_per_segment),
         "-reset_timestamps", "1", "-strftime", "0", pattern],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return process, segment_dir, pattern


AUDIOTAP_BIN = os.path.expanduser("~/bin/curant-facetime-audiotap")
AUDIOTAP_DISABLE_ENV = "CURANT_FACETIME_DISABLE_AUDIOTAP"
AUDIOTAP_ENABLE_ENV = "CURANT_FACETIME_ENABLE_AUDIOTAP"  # opt-in; see audiotap_available()
AUDIOTAP_SYSTEM_AUDIO_ENV = "CURANT_FACETIME_SYSTEM_AUDIO"  # =1 -> capture the whole system mix
AUDIOTAP_LOG_PATH = "/tmp/curant-facetime-audiotap.log"
AUDIOTAP_READY_TIMEOUT_SECONDS = 12


def audiotap_available():
    """OFF BY DEFAULT as of 2026-08-21. Opt in with
    CURANT_FACETIME_ENABLE_AUDIOTAP=1.

    WHY IT IS OFF -- measured, not assumed. Three capture routes were
    tested against a live FaceTime call on this Mac:

      1. Virtual output device (Multi-Output -> BlackHole 16ch, ffmpeg)
         Tone played into the same device mid-call: RMS 5097.5.
         FaceTime's call audio: EXACTLY 0.0.
      2. ScreenCaptureKit scoped to the FaceTime application
         Stream delivered 489 buffers at 48kHz/2ch -- so the OS was
         actively handing us audio -- with peak amplitude 0 throughout.
      3. ScreenCaptureKit capturing the ENTIRE system mix
         During a call: peak 17 out of 32768 (~-65dB, dither/noise).
         CONTROL, same binary, music playing, no call: peak 7614-8452.

    The control is what makes this conclusive rather than another
    guess: identical code, identical conversion path, real audio
    captured at full amplitude. The capture pipeline is correct. macOS
    simply does not expose FaceTime call audio to ScreenCaptureKit --
    it is protected communications audio -- and does not reliably
    render it into the system default output device either.

    The tap is kept (not deleted) because it is correct code that
    works for ordinary applications, and because Apple's treatment of
    this may change. But defaulting it ON would guarantee silence on
    every call, which is strictly worse than the BlackHole path -- that
    one at least succeeded on several real calls tonight (RMS 275, 694,
    57, 35), which is itself evidence that FaceTime DOES sometimes
    render into the capturable device, depending on the audio route
    FaceTime has chosen for that call."""
    if os.environ.get(AUDIOTAP_ENABLE_ENV) != "1":
        return False
    if os.environ.get(AUDIOTAP_DISABLE_ENV) == "1":
        return False
    return os.path.isfile(AUDIOTAP_BIN) and os.access(AUDIOTAP_BIN, os.X_OK)


def _start_audiotap_capture(seconds_per_segment):
    """Captures FaceTime's audio via ScreenCaptureKit instead of via a
    virtual output device.

    WHY THIS REPLACED THE BLACKHOLE PATH -- the decisive measurement
    (2026-08-21, live call): a test tone played into the system default
    output was captured back from BlackHole 16ch at RMS 5097.5 DURING a
    connected call, proving the capture path itself was working
    perfectly at that exact moment, while FaceTime's own call audio in
    the same window measured EXACTLY 0.0. Zero samples, not a low
    level. The only reading consistent with both numbers is that
    FaceTime never renders call audio into the system default output at
    all -- it uses the OS communications audio path, which bypasses
    aggregate/virtual devices. Every fix attempted against the old
    model (rebuilding the Multi-Output Device, drift correction,
    per-call vs startup switching, correcting the input device) failed
    for that reason, and the few calls that appeared to work were
    coincidence rather than the fix taking effect.

    ScreenCaptureKit taps the APPLICATION's audio wherever it actually
    goes, so device routing stops mattering entirely. It needs Screen
    Recording permission, which this feature already requires for its
    visual call detection -- so no new customer-facing permission.

    The helper writes the same turn_%05d.wav layout the ffmpeg segment
    muxer produced, already converted to 16kHz mono, so the entire turn
    loop downstream is unchanged. It prints READY once capture is
    actually live; we wait for that rather than assuming success,
    because "started but silently captured nothing" is precisely the
    failure this whole subsystem has been burned by.

    Returns (process, segment_dir, pattern) -- same contract as
    _start_continuous_capture()."""
    segment_dir = tempfile.mkdtemp(prefix="curant_facetime_turns_")
    pattern = os.path.join(segment_dir, "turn_%05d.wav")
    # REAL BUG this fixes: stderr was subprocess.PIPE and nothing ever
    # read it. Every diagnostic the Swift tap emits -- the audio format
    # it received, its heartbeat, its "zero buffers delivered" warning
    # -- went into a pipe no one drained, so from outside the tap was
    # completely mute and the first live test was undiagnosable. Worse,
    # an undrained pipe blocks the writer once the OS buffer fills
    # (~64KB), which could stall the tap itself. stderr now goes to a
    # real file that can be tailed like every other Curant log.
    log_path = AUDIOTAP_LOG_PATH
    tap_log = open(log_path, "a", buffering=1)
    tap_log.write(f"\n===== audiotap starting {time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"(segment {seconds_per_segment}s) =====\n")
    extra = ["--system-audio"] if os.environ.get(AUDIOTAP_SYSTEM_AUDIO_ENV) == "1" else []
    proc = subprocess.Popen(
        [AUDIOTAP_BIN, "--out-dir", segment_dir,
         "--segment-seconds", str(seconds_per_segment)] + extra,
        stdout=subprocess.PIPE, stderr=tap_log, text=True,
    )
    deadline = time.monotonic() + AUDIOTAP_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = ""
            try:
                with open(AUDIOTAP_LOG_PATH) as fh:
                    err = "".join(fh.readlines()[-12:]).strip()
            except Exception:
                pass
            shutil.rmtree(segment_dir, ignore_errors=True)
            raise RuntimeError(f"audiotap exited immediately: {err[:400]}")
        line = proc.stdout.readline() if proc.stdout else ""
        if line.strip() == "READY":
            # Keep draining stdout for the life of the process. Same
            # class of bug as the stderr one above: a pipe nobody reads
            # eventually blocks the writer.
            def _drain(stream):
                try:
                    for _ in iter(stream.readline, ""):
                        pass
                except Exception:
                    pass
            threading.Thread(target=_drain, args=(proc.stdout,), daemon=True).start()
            return proc, segment_dir, pattern
        if not line:
            time.sleep(0.05)
    try:
        proc.kill()
    except Exception:
        pass
    shutil.rmtree(segment_dir, ignore_errors=True)
    raise RuntimeError(f"audiotap did not report READY within {AUDIOTAP_READY_TIMEOUT_SECONDS}s")


def start_caller_capture(seconds_per_segment):
    """Picks a capture backend and starts it.

    ScreenCaptureKit first when it's built (see _start_audiotap_capture
    for why it is strongly preferred), falling back to the legacy
    BlackHole/ffmpeg path if the helper is missing, disabled, or fails
    to come up. The fallback is deliberate rather than fatal: an
    existing customer whose Multi-Output Device setup happens to work
    should not be broken by a helper that failed to build, and the
    fallback is loudly logged so a silent downgrade is impossible."""
    if audiotap_available():
        try:
            proc, segment_dir, pattern = _start_audiotap_capture(seconds_per_segment)
            print(f"  [{_ts()}] Capture backend: ScreenCaptureKit app tap (pid {proc.pid}) -- "
                  f"device routing bypassed.")
            return proc, segment_dir, pattern, "screencapturekit"
        except Exception as e:
            print(f"  [{_ts()}] ScreenCaptureKit tap failed to start ({e}) -- falling back to the "
                  f"BlackHole/ffmpeg capture path.", file=sys.stderr)
    proc, segment_dir, pattern = _start_continuous_capture(seconds_per_segment)
    print(f"  [{_ts()}] Capture backend: BlackHole/ffmpeg (pid {proc.pid}) -- depends on FaceTime "
          f"following the system default output device, which it has been observed NOT to do.")
    return proc, segment_dir, pattern, "blackhole"


def _stop_continuous_capture(process):
    """Best-effort clean shutdown of the persistent ffmpeg process
    started by _start_continuous_capture(). Never raises -- this runs on
    the way out of handle_call() and must not itself become a new source
    of problems."""
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _wait_for_next_turn_segment(pattern, index, stop_event=None):
    """Blocks until turn N's segment file is fully finalized, detected by
    the NEXT segment (N+1) appearing on disk -- ffmpeg's segment muxer
    only starts writing file N+1 once file N is closed out, so this is a
    reliable "is turn N done" signal without needing to open the file
    ourselves to check. Polls at SEGMENT_WAIT_POLL_SECONDS intervals, and
    gives up after SEGMENT_WAIT_TIMEOUT_SECONDS as a hard backstop.

    CHANGED after a real, still-unexplained live bug: a call dropped on
    its own even with the continuous-capture fix (one persistent ffmpeg
    process for the whole call, not one per turn) already in place --
    meaning repeated device open/close wasn't the only remaining cause,
    or wasn't the cause at all. The one thing that WAS happening very
    frequently throughout that call (every ~1.6-2s from the outer loop,
    ADDITIONALLY every SEGMENT_WAIT_POLL_SECONDS from here) is
    _call_is_still_connected() -- which spawns a fresh `osascript`
    process every single call. Earlier live evidence (a real macOS log
    capture) showed FaceTime's own call system can auto-disconnect a
    call when a tracked client process's connection is invalidated
    (`wantsCallDisconnectionOnInvalidation=YES`) -- confirmed for an
    abrupt kill (Ctrl+C), unconfirmed but plausible for ordinary rapid
    process churn too. Until that's ruled in or out, this no longer
    calls is_still_connected_fn() at all -- connectivity is checked ONCE
    per turn by the caller (handle_call()'s outer loop), not repeatedly
    while waiting for a segment. This cuts total AppleScript
    invocations from 2-4 per turn down to 1, directly reducing exposure
    to whatever this mechanism turns out to be, at the cost of relying
    more on SEGMENT_WAIT_TIMEOUT_SECONDS as the sole backstop if a
    segment genuinely never arrives.

    Returns the path to turn N's now-finalized segment file, or None if
    this timed out waiting (the caller's own connectivity check, run
    once per turn, is what actually detects a real hangup now).

    stop_event, if given (see _watch_for_call_end()), is checked on every
    poll tick (SEGMENT_WAIT_POLL_SECONDS, already 0.2s) so a real hangup
    detected via the call-end OCR watcher interrupts this wait almost
    immediately instead of blocking up to SEGMENT_WAIT_TIMEOUT_SECONDS."""
    this_segment = pattern % index
    next_segment = pattern % (index + 1)
    deadline = time.monotonic() + SEGMENT_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return None
        if os.path.exists(next_segment):
            return this_segment if os.path.exists(this_segment) else None
        time.sleep(SEGMENT_WAIT_POLL_SECONDS)
    return None


def _extract_loudest_channel_mono(multi_channel_path):
    """Real bug found live: BlackHole 16ch is a 16-channel virtual
    device, and capturing it with ffmpeg's default mono downmix
    (previously -ac 1 in _start_continuous_capture) produced near-total
    digital silence (RMS ~1, peak ~10-15 out of 32768) on EVERY turn of
    a real 4+ minute call, even though the caller spoke clearly the
    whole time and System Settings > Sound confirmed BlackHole 16ch was
    genuinely the selected output device throughout (checked via a live
    screenshot, ruling out a wrong-device explanation). The real audio
    has to be landing on some specific channel of the 16 that a naive
    downmix doesn't reliably pick up.

    _start_continuous_capture() now captures all 16 channels raw and
    untouched. This function runs once per turn, after the segment is
    ready: measures RMS independently on EVERY channel, and keeps only
    whichever one actually has the caller's voice on it, writing that
    single channel out as a new mono WAV. No channel is assumed ahead of
    time -- if the real channel shifts between calls or Macs, this still
    finds it.

    Returns the path to a new mono WAV file (caller must clean this up
    separately from the original multi-channel file), or the original
    path unchanged if it was already mono."""
    import wave
    import numpy as np
    with wave.open(multi_channel_path, "rb") as w:
        n_channels = w.getnchannels()
        sample_rate = w.getframerate()
        sampwidth = w.getsampwidth()
        n_frames = w.getnframes()
        frames = w.readframes(n_frames)

    if n_channels <= 1:
        return multi_channel_path  # already mono, nothing to extract

    if sampwidth != 2:
        # Only int16 PCM is handled below -- if the device ever produces
        # something else, fail loud rather than silently mis-decode it.
        raise RuntimeError(
            f"Unexpected sample width {sampwidth} bytes (expected 2/int16) "
            f"reading {multi_channel_path} -- can't safely extract a channel."
        )

    samples = np.frombuffer(frames, dtype=np.int16)
    usable_frames = samples.size // n_channels
    samples = samples[: usable_frames * n_channels].reshape(-1, n_channels)
    per_channel_rms = np.sqrt(np.mean(samples.astype(np.float64) ** 2, axis=0))
    best_channel = int(np.argmax(per_channel_rms))
    # TRIMMED (2026-08-08, latency pass): this used to print every one
    # of the 16 channels' individual RMS values on every single call to
    # this function -- useful during the channel-mismatch investigation
    # (see docstring above), pure noise now that the real channel is
    # confirmed stable. This function also runs on every barge-in watch
    # tick during reply playback (_InterruptWatcher.check()), not just
    # once per real turn, so the old line was printing far more often
    # than once per turn. Down to a single short line with just the
    # winning channel.
    print(f"  [{_ts()}] {n_channels}ch capture -> using channel {best_channel} "
          f"(RMS={per_channel_rms[best_channel]:.1f})", file=sys.stderr)

    mono_samples = np.ascontiguousarray(samples[:, best_channel])
    mono_path = multi_channel_path[:-4] + "_mono.wav" if multi_channel_path.endswith(".wav") \
        else multi_channel_path + "_mono.wav"
    with wave.open(mono_path, "wb") as w_out:
        w_out.setnchannels(1)
        w_out.setsampwidth(sampwidth)
        w_out.setframerate(sample_rate)
        w_out.writeframes(mono_samples.tobytes())
    return mono_path


SILENCE_RMS_THRESHOLD = 15.0  # int16 RMS units — RECALIBRATED from a real call, see below
# CHANGED after real live data: this was originally 250.0, a guess made
# before any real audio had ever actually been captured through this
# pipeline. Once the Multi-Output Device fix got real signal flowing
# for the first time, a live call with the caller speaking clearly the
# whole time measured RMS 17-40 (peak 166-753) per turn -- genuine
# speech, well above true silence (confirmed elsewhere as an exact
# RMS=0.0 baseline), but every single one of those turns was still
# being thrown away as "silent" against the old 250 threshold. 15.0 sits
# just above that true-silence floor while comfortably catching the
# real speech levels actually observed -- raise it again only if
# background-noise turns start false-triggering transcription, using
# the per-turn RMS now printed by _wav_has_speech to see exactly what
# the noise floor looks like on this specific Mac/setup.


def _wav_has_speech(wav_path, threshold=SILENCE_RMS_THRESHOLD):
    """Cheap voice-activity gate run BEFORE spending an API call on
    transcription. transcribe()'s Gemini prompt already asks for an
    empty string on silence, but live testing found that instruction
    isn't reliable: given a near-silent clip (e.g. because BlackHole
    16ch was never actually selected as FaceTime's Speaker for that
    call — a manual per-call step, see SETUP_FACETIME_CALLS.md step 5),
    Gemini can confabulate a fluent, entirely invented sentence instead
    of reporting silence. That's worse than just skipping a turn — it's
    a fabricated statement put in a real caller's mouth. So this
    doesn't trust the model's self-report; it measures the actual
    recorded audio's RMS against a noise-floor threshold first, and
    skips calling transcribe() at all if the clip looks silent.

    Threshold is in int16 PCM RMS units. Empirically: normal room noise
    floor recorded through BlackHole with nothing playing tends to sit
    well under 100; actual speech easily clears several hundred to a
    few thousand. 250 is a conservative starting point — raise it if
    hallucinated turns still get through with a genuinely quiet caller
    or noisy room, lower it if real quiet speech gets skipped.

    CHANGED (2026-08-08): now returns (has_speech, rms) instead of just
    a bool -- handle_call() needs the raw RMS too, to tell a genuinely
    CONNECTED-but-quiet call (real room-tone noise floor, RMS ~7-18 in
    every real call logged so far) apart from a call that's actually
    ENDED (exact/near-zero RMS across every channel, RMS=0.0 measured
    repeatedly whenever there was no real FaceTime audio source at
    all -- see _detect_hangup_from_silence()'s docstring for how this
    gets used). rms is None only when the file genuinely couldn't be
    read at all (caller should treat that as inconclusive, not as
    either signal)."""
    import wave
    import numpy as np
    try:
        with wave.open(wav_path, "rb") as w:
            n_frames = w.getnframes()
            n_channels = w.getnchannels()
            sample_rate = w.getframerate()
            frames = w.readframes(n_frames)
    except Exception as e:
        print(f"  [{_ts()}] Could not read {wav_path} for silence check ({e}) — "
              f"transcribing anyway rather than silently dropping the turn.",
              file=sys.stderr)
        return True, None
    # Always print what was actually measured, not just the pass/fail
    # verdict -- added live after every single turn of a real 4+ minute
    # call came back "silent" despite the caller speaking clearly. This
    # makes the next test tell us whether the clip is genuinely near-
    # zero (routing/device problem) or just under-threshold real audio
    # (threshold too high), instead of guessing between those two very
    # different problems. TRIMMED (2026-08-08, latency pass): this used
    # to be two separate print calls (frame/channel/rate info, then a
    # second line for RMS/peak/verdict) on every single check -- this
    # function also runs on every barge-in watch tick during reply
    # playback, not just once per real turn. Down to one line with just
    # what's actually needed to read a result at a glance; duration is
    # folded in, frame/channel/rate detail dropped since it was never
    # actually needed once the pipeline was confirmed working.
    if not frames:
        print(f"  [{_ts()}] WAV check: empty/corrupt segment file -- treating as silent.",
              file=sys.stderr)
        return False, 0.0
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        print(f"  [{_ts()}] WAV check: zero samples after decode -- treating as silent.",
              file=sys.stderr)
        return False, 0.0
    rms = float(np.sqrt(np.mean(samples ** 2)))
    duration = n_frames / sample_rate if sample_rate else 0
    print(f"  [{_ts()}] WAV check ({duration:.1f}s): RMS={rms:.1f}/threshold={threshold:.0f} "
          f"-> {'HAS SPEECH' if rms >= threshold else 'silent'}", file=sys.stderr)
    return rms >= threshold, rms


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


def _config_api_key(cfg, provider):
    return (cfg.get("api_keys", {}) or {}).get(provider)


# Ordered fallback chain for live-call transcription, mirroring
# curant-cli's GEMINI_MODEL_CHAINS["fast"] (kept as a separate literal
# here rather than imported, since this is a standalone process/SDK
# path using the native google-genai client, not curant-cli's
# OpenAI-compat call_llm()/_model_fallback_chain() machinery — see that
# file for the full rationale and the AI Studio dashboard numbers this
# ordering was derived from). Lite models with the deepest daily quota
# (500 RPD) come first since a single multi-turn call generates one
# transcription request per turn and can burn through a 20/day cap
# fast; quality is a secondary concern for this leg.
GEMINI_CALL_MODEL_CHAIN = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3-flash",
    "gemini-2.5-flash",
    "gemini-3.6-flash",
]
GEMINI_CALL_MODEL = GEMINI_CALL_MODEL_CHAIN[0]  # kept for any external reference to a single "the" call model


def _is_quota_error(e):
    """Same detection logic as curant-cli's _is_quota_error() — kept as
    a separate copy since this is a standalone process/SDK path (see
    GEMINI_CALL_MODEL_CHAIN comment above for why it isn't imported)."""
    msg = str(e).lower()
    return ("429" in msg or "resource_exhausted" in msg or "resource exhausted" in msg
            or "rate limit" in msg or "quota" in msg)


MAX_UTTERANCE_SECONDS = 15.0   # hard cap: flush and transcribe even if the caller hasn't paused

# How much trailing silence means "they're done talking, answer them".
# Expressed in SECONDS and converted, so it stays a real duration if
# TURN_RECORD_SECONDS changes again (it has changed four times now).
# 0.8s is around the low end of natural conversational turn-taking:
# long enough not to cut in on a brief mid-sentence breath, short
# enough that Curant doesn't feel slow to start answering. At 1.0s
# segments this rounds to a single silent segment.
UTTERANCE_TRAILING_SILENCE_SECONDS = 0.8
UTTERANCE_TRAILING_SILENCE_SEGMENTS = max(
    1, int(round(UTTERANCE_TRAILING_SILENCE_SECONDS / TURN_RECORD_SECONDS)))


def _concat_wavs(paths, out_path):
    """Joins same-format mono WAVs end to end into one file. Pure
    stdlib (wave module) -- no ffmpeg subprocess, because this runs on
    the critical path of every single turn and spawning another process
    per turn is exactly the kind of churn this file has repeatedly
    traced call instability back to."""
    import wave
    with wave.open(paths[0], "rb") as first:
        params = first.getparams()
    with wave.open(out_path, "wb") as out:
        out.setparams(params)
        for p in paths:
            with wave.open(p, "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))
    return out_path


class _UtteranceBuffer:
    """Accumulates consecutive speech segments into ONE utterance and
    only transcribes when the caller actually stops talking.

    THE PROBLEM THIS SOLVES, confirmed live across several nights of
    real calls: caller audio is chopped into fixed TURN_RECORD_SECONDS
    windows by ffmpeg's segment muxer, with no relationship whatsoever
    to where sentences begin and end. Every turn was transcribed in
    isolation, so a sentence spanning a boundary arrived as two partial
    fragments and Gemini -- correctly, per its own prompt -- returned an
    empty string for each rather than hallucinating. Observed
    repeatedly: turns with unmistakably real speech (RMS 694, 120, 57,
    35, all far above threshold) producing no transcript at all. Raising
    the window from 2s to 3s reduced but did NOT eliminate this, because
    no fixed window can align with natural speech -- it just moves where
    the cut lands.

    The fix is to stop treating a fixed window as a turn boundary at
    all. Segments with speech are buffered; the utterance is only
    considered complete (and sent for transcription as a single
    concatenated clip) once a segment comes back silent -- i.e. the
    caller actually paused -- or MAX_UTTERANCE_SECONDS is reached, which
    bounds worst-case latency for someone who talks without pausing.

    Segments are COPIED into the buffer's own temp files rather than
    borrowing the loop's paths, so the existing per-turn cleanup can go
    on deleting its files exactly as before -- a buffered utterance can
    never be left holding a path someone else already removed. A ~3s
    16kHz mono segment is under 100KB, so the copy is cheap next to the
    API round trip it protects."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="curant_utterance_")
        self.paths = []
        self.silent_run = 0
        self.seconds = 0.0
        self._preroll = None  # most recent BELOW-threshold segment, kept as lead-in (see note_silence)
        self._seq = 0         # monotonic, see _stash

    def _stash(self, wav_path, tag):
        # Names are keyed on a MONOTONIC counter, not len(self.paths).
        # Caught by the pre-roll unit test: len(self.paths) stays 0
        # while only pre-roll segments are arriving, so every stash
        # produced the identical filename -- the new copy overwrote the
        # old one and the "delete the previous pre-roll" step then
        # deleted the file that had just been written, leaving nothing
        # to prepend. Silent data loss, invisible in any log.
        self._seq += 1
        dest = os.path.join(self.dir, f"{tag}_{self._seq:05d}.wav")
        shutil.copy2(wav_path, dest)
        return dest

    def add_speech(self, wav_path, seconds):
        try:
            # PRE-ROLL: the segment immediately before speech was
            # detected gets prepended to the utterance. A segment is
            # classified by its AVERAGE RMS over the whole window, so a
            # word that begins in the last fraction of a segment can
            # easily leave that segment's average below threshold --
            # the segment reads "silent" and gets dropped, taking the
            # word's onset with it. Transcription of a clip that starts
            # mid-consonant is exactly the "real speech, empty
            # transcript" failure this file has chased for days.
            # Carrying one segment of lead-in costs one extra second of
            # audio in the request and removes that whole failure mode.
            if not self.paths and self._preroll is not None:
                self.paths.append(self._preroll)
                self.seconds += TURN_RECORD_SECONDS
                self._preroll = None
            self.paths.append(self._stash(wav_path, "part"))
            self.seconds += seconds or 0.0
            self.silent_run = 0
        except Exception as e:
            print(f"  [{_ts()}] Could not buffer utterance segment ({e}) -- "
                  f"transcribing this segment alone instead.", file=sys.stderr)
            self.paths.append(wav_path)  # degrade gracefully rather than lose the audio

    def note_silence(self, wav_path=None):
        self.silent_run += 1
        # Keep the newest silent segment as a candidate lead-in for
        # speech that may start in the next one. Only meaningful while
        # nothing is buffered yet -- mid-utterance silence is either
        # a real pause (ends the utterance) or already surrounded by
        # speech segments that were kept.
        if wav_path is not None and not self.paths:
            try:
                old = self._preroll
                self._preroll = self._stash(wav_path, "preroll")
                if old and os.path.exists(old):
                    os.remove(old)  # only ever keep ONE, so this can't grow unbounded
            except Exception:
                self._preroll = None

    def has_audio(self):
        return bool(self.paths)

    def should_flush(self):
        """Flush when the caller has paused, or when they've been going
        long enough that waiting for a pause would itself feel broken."""
        if not self.paths:
            return False
        return (self.silent_run >= UTTERANCE_TRAILING_SILENCE_SEGMENTS
                or self.seconds >= MAX_UTTERANCE_SECONDS)

    def build(self):
        """Returns a single WAV path for everything buffered, or None."""
        if not self.paths:
            return None
        if len(self.paths) == 1:
            return self.paths[0]
        out = os.path.join(self.dir, "utterance.wav")
        try:
            return _concat_wavs(self.paths, out)
        except Exception as e:
            print(f"  [{_ts()}] Could not join {len(self.paths)} utterance parts ({e}) -- "
                  f"falling back to the longest single part.", file=sys.stderr)
            return max(self.paths, key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0)

    def reset(self):
        try:
            shutil.rmtree(self.dir, ignore_errors=True)
        except Exception:
            pass
        self.dir = tempfile.mkdtemp(prefix="curant_utterance_")
        self.paths = []
        self.silent_run = 0
        self.seconds = 0.0
        self._preroll = None  # its file lived in the dir just removed
        self._seq = 0

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def _is_transient_api_error(e):
    """Broader than _is_quota_error(): also catches the SERVER-side
    overload/availability failures that aren't about this account's
    quota at all.

    REAL BUG (2026-08-21): a live call with genuinely captured speech
    (RMS 275.1 -- unambiguous, well above threshold) died on a Gemini
    "503 UNAVAILABLE: This model is currently experiencing high demand"
    response. 503 contains none of the substrings _is_quota_error()
    looks for, so it wasn't retried against the model fallback chain
    and wasn't recognised as transient anywhere -- it simply propagated
    and took the whole call down. These are precisely the errors most
    worth retrying: they're temporary by definition and usually clear
    within seconds."""
    msg = str(e).lower()
    return (_is_quota_error(e)
            or "503" in msg or "unavailable" in msg or "overloaded" in msg
            or "500" in msg or "internal error" in msg
            or "502" in msg or "504" in msg or "deadline" in msg
            or "timed out" in msg or "timeout" in msg
            or "connection" in msg or "temporarily" in msg)


TRANSCRIBE_RETRY_ATTEMPTS = 3
TRANSCRIBE_RETRY_BASE_SECONDS = 0.6


def transcribe_with_retry(wav_path, cfg):
    """transcribe() plus bounded retry-with-backoff on transient errors.

    A live caller is waiting, so this is deliberately impatient: a small
    number of attempts with short exponential backoff (~0.6s, ~1.2s),
    total added latency well under two seconds in the worst case. The
    alternative -- what shipped before this -- was that a single
    momentary 503 from Google's side lost the caller's sentence
    entirely, and (before the surrounding try/except was added) killed
    the whole call.

    Non-transient errors (bad API key, malformed request) are NOT
    retried -- they'd fail identically every time and retrying just
    burns the caller's patience. They propagate to the caller's own
    handler, which treats the turn as unheard and keeps the call
    alive."""
    last_err = None
    for attempt in range(TRANSCRIBE_RETRY_ATTEMPTS):
        try:
            return transcribe(wav_path, cfg)
        except Exception as e:
            last_err = e
            if not _is_transient_api_error(e) or attempt == TRANSCRIBE_RETRY_ATTEMPTS - 1:
                raise
            delay = TRANSCRIBE_RETRY_BASE_SECONDS * (2 ** attempt)
            print(f"  [{_ts()}] Transcription hit a transient error ({e}) -- retrying in "
                  f"{delay:.1f}s (attempt {attempt + 2}/{TRANSCRIBE_RETRY_ATTEMPTS}).",
                  file=sys.stderr)
            time.sleep(delay)
    raise last_err  # unreachable in practice -- the loop either returns or raises above


def _transcribe_gemini(wav_path, api_key):
    """Gemini's native audio understanding — no separate transcription
    service needed if you're already on Gemini for replies. Uses the
    same native google-genai SDK as curant-cli's Gemini tool-calling
    path (not the OpenAI-compat shim, which doesn't reliably support
    audio input).

    Walks GEMINI_CALL_MODEL_CHAIN on a quota/rate-limit error instead of
    failing the whole call turn outright — mirrors curant-cli's
    call_llm()/_model_fallback_chain(). A non-quota error (bad key,
    network failure, etc.) is NOT retried against another model; it
    propagates immediately, same as before this change."""
    from google import genai
    from google.genai import types as genai_types

    client = genai.Client(api_key=api_key)
    with open(wav_path, "rb") as f:
        audio_bytes = f.read()

    last_err = None
    for i, model in enumerate(GEMINI_CALL_MODEL_CHAIN):
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    "Transcribe this audio verbatim. Reply with ONLY the transcript "
                    "text, nothing else — no commentary, no quotation marks. If "
                    "there is no discernible speech (silence or just noise), reply "
                    "with an empty string.",
                    genai_types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
                ],
            )
            return (response.text or "").strip()
        except Exception as e:
            if not _is_quota_error(e):
                raise
            last_err = e
            more = (f"trying next model ({GEMINI_CALL_MODEL_CHAIN[i + 1]})"
                     if i + 1 < len(GEMINI_CALL_MODEL_CHAIN) else "no more models left to try")
            print(f"[quota] gemini/{model} hit a rate/quota limit ({e}) -- {more}.", file=sys.stderr)
    raise RuntimeError(
        f"Every model in GEMINI_CALL_MODEL_CHAIN hit a rate/quota limit. Last error: {last_err}"
    )


def _transcribe_openai_whisper(wav_path, api_key):
    """OpenAI's Whisper API — the original transcription path, still
    available as a fallback for anyone not using Gemini as their
    provider."""
    import requests
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


def transcribe(wav_path, cfg):
    """Tries Gemini first (native audio understanding, no separate
    account needed if that's already your provider), falls back to
    OpenAI's Whisper if a Gemini key isn't configured but an OpenAI one
    is. Raises with clear instructions for both options if neither key
    is set."""
    gemini_key = _config_api_key(cfg, "gemini") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        return _transcribe_gemini(wav_path, gemini_key)

    openai_key = _config_api_key(cfg, "openai") or os.environ.get("OPENAI_API_KEY")
    if openai_key:
        return _transcribe_openai_whisper(wav_path, openai_key)

    raise RuntimeError(
        "No API key configured for call transcription. Either: "
        "curant-cli set-api-key <key> --provider gemini (uses Gemini's native "
        "audio understanding, no separate service), or "
        "curant-cli set-api-key <key> --provider openai (uses Whisper)."
    )


def get_reply(text, apple_id):
    """Routes through curant-cli's normal relay, same persona/memory/tools
    as text — tier='fast' since a live caller is waiting on this, not
    reading a text at their own pace, and --voice so the reply comes
    back short and spoken-style instead of formatted like a written
    paragraph (real bug found live: a caller asked what time it was and
    got a ~14-second reply written like an email). See curant-cli
    relay()'s voice_mode docstring for exactly what --voice changes."""
    r = subprocess.run(
        ["curant-cli", "relay", text, "--apple-id", apple_id, "--tier", "fast", "--voice"],
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


def get_reply_streaming(text, apple_id, on_sentence):
    """
    Streaming counterpart to get_reply(), added per explicit request to
    cut perceived call latency. get_reply() uses subprocess.run(), which
    blocks until curant-cli's ENTIRE process exits -- meaning a caller
    sat in dead silence for the full reply-generation round trip
    (observed live: 2-20s) before speak() ever started. This calls
    on_sentence(sentence) for each complete sentence AS SOON AS curant-
    cli produces it, so handle_call() can start speak()ing the first
    sentence while the rest is still being generated.

    Protocol (see curant-cli relay's --stream flag docstring): zero or
    more {"chunk": "..."} lines, flushed by curant-cli as generated,
    followed by exactly one final {"reply": ..., "reply_format": ...,
    "voice_tier": ..., "attachment_path": ...} line -- same shape as
    the non-streaming response. If curant-cli never emits any chunk
    lines at all (provider fallback, or a mid-turn tool call aborted
    streaming -- see call_llm_with_tools_streaming /
    _call_gemini_with_tools_streaming's docstrings), the final line's
    full reply text is delivered as a single on_sentence() call instead
    -- correct either way, just without the early-start benefit on
    every path yet.

    Sentence-splitting happens HERE (buffering raw text chunks until a
    ., !, or ? is seen), not in curant-cli, so speak() gets whole
    sentences to synthesize rather than word-by-word fragments, which
    would sound choppy and add per-utterance TTS overhead for no
    latency benefit (the win is starting sentence 1 early, not
    minimizing sentence size).
    """
    args = ["curant-cli", "relay", text, "--apple-id", apple_id, "--tier", "fast", "--voice", "--stream"]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    buffer = ""
    full_text_parts = []
    final_reply = None
    saw_any_chunk = False

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue  # not a JSON line (shouldn't happen) -- skip rather than crash the whole call
            if "chunk" in data:
                saw_any_chunk = True
                buffer += data["chunk"]
                full_text_parts.append(data["chunk"])
                while True:
                    m = re.search(r"[.!?](\s|$)", buffer)
                    if not m:
                        break
                    end = m.end()
                    sentence = buffer[:end].strip()
                    buffer = buffer[end:]
                    if sentence:
                        on_sentence(sentence)
            elif "reply" in data:
                final_reply = data
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        stderr_out = proc.stderr.read() if proc.stderr else ""
        if stderr_out:
            print(stderr_out, file=sys.stderr, end="")

    if buffer.strip():
        on_sentence(buffer.strip())

    if final_reply is None:
        raise RuntimeError("curant-cli relay --stream never printed a final reply line")
    if final_reply.get("error"):
        raise RuntimeError(f"relay error: {final_reply['error']}")

    if not saw_any_chunk:
        whole = final_reply.get("reply") or ""
        if whole:
            on_sentence(whole)
        return whole

    return "".join(full_text_parts)


def _call_is_still_connected():
    """Read-only counterpart to hang_up() — checks whether the call is
    still connected, WITHOUT clicking anything. This is what
    handle_call()'s loop uses to detect the human has ended the call,
    deliberately NOT _facetime_is_frontmost() (see below for why).

    TEMPORARY DIAGNOSTIC MODE, and deliberately LOOSE right now: this
    was originally written checking specifically for Decline/End named
    buttons (same names hang_up() guesses at), but that version
    immediately reported "not connected" on every single real call,
    right after the greeting, every time — a suspiciously consistent,
    deterministic failure that (unlike the earlier _facetime_is_frontmost
    bug, which was timing/focus-dependent and thus variable) points at
    the button-name check itself being wrong, not an actual disconnect.
    This makes sense: hang_up()'s Decline/End names were never actually
    verified against a live FaceTime AUDIO in-call window's real
    Accessibility tree (only guessed, by analogy) — and we already know
    from way earlier in this project that FaceTime's PRE-answer banner
    exposes zero named controls to Accessibility at all; the in-call
    window may have its own different, unverified structure too.

    So: this version dumps the real window/button names to stderr every
    time it runs (so the next live call actually tells us what's there,
    instead of guessing a third time), and treats the call as still
    connected as long as the FaceTime process exists and has at least
    one window — much looser than requiring specific button names,
    deliberately erring toward NOT ending the call while we don't yet
    know the real structure. Tighten this back up once the diagnostic
    output shows the real button/window names.

    Real bug found live that led to needing ANY version of this check:
    _facetime_is_frontmost() checks which app currently has WINDOW
    FOCUS, not whether a call is connected — it was built and validated
    for a different, narrower purpose (confirming a click just answered
    a call, checked immediately after clicking). Reusing it as an
    ongoing "is the call still active" check was wrong: clicking into
    Terminal to read logs makes FaceTime stop being frontmost with the
    call still perfectly connected, which looked exactly like "Curant
    cutting the call" even though nothing was ever clicked.

    CHANGED again after a second real bug, found live: this used to
    treat raw == "NO_PROCESS" as proof the call had ended. Three
    consecutive real test calls in the same run all showed
    raw='NO_PROCESS' immediately after a successful accept and greeting
    — meaning FaceTime.app's own process never spun up at all for those
    calls, even while genuinely connected — cutting all three right
    after the greeting played. This directly contradicts an earlier
    test where the same check read 'PROCESS_NO_WINDOWS' (process
    exists, no windows) right after a successful accept. So FaceTime.app
    becoming a real process on answer is NOT reliable in either
    direction — sometimes it happens, sometimes it doesn't, for reasons
    outside this script's control. Given the hard requirement that
    Curant must never be the one to end a call, NO_PROCESS can no longer
    be trusted as "ended" — only a literal AppleScript execution failure
    (System Events itself erroring) is treated as inconclusive now.
    KNOWN GAP, not hidden: this means the loop currently has no reliable
    way to detect a real hangup at all, and will keep trying to
    record/transcribe/reply into a dead call until it's manually killed
    or a better signal is found — accepted as the lesser problem versus
    falsely cutting a call that's still live."""
    script = '''
    tell application "System Events"
        if not (exists process "FaceTime") then return "NO_PROCESS"
        tell process "FaceTime"
            set out to ""
            repeat with w in windows
                set out to out & "WINDOW=" & (name of w)
                try
                    set out to out & " BUTTONS=" & ((name of every button of w) as string)
                on error errMsg
                    set out to out & " BUTTONS_ERROR=" & errMsg
                end try
                set out to out & " || "
            end repeat
            if out is "" then return "PROCESS_NO_WINDOWS"
            return out
        end tell
    end tell
    '''
    r = _run_osascript(script)
    raw = (r.stdout or "").strip()
    print(f"  [{_ts()}] [_call_is_still_connected diagnostic] rc={r.returncode} raw={raw!r} "
          f"stderr={(r.stderr or '').strip()!r}", file=sys.stderr)

    if r.returncode != 0:
        return False
    # NO_PROCESS, PROCESS_NO_WINDOWS, or any window dump at all -> still
    # connected, deliberately loose (see CHANGED note above) until a
    # genuinely reliable connected/ended signal is found.
    return True


def hang_up():
    """NOT CALLED FROM ANYWHERE IN THIS FILE ANYMORE, DELIBERATELY. Per
    explicit direction, Curant must never be the one to end a call —
    only the human can, by hanging up on their own end. handle_call()
    used to call this both on a "bye"-style keyword match and when its
    turn budget ran out; both of those were removed so nothing in this
    script can ever trigger a hangup anymore. Kept here, unused, only in
    case an explicit, human-initiated hangup control (e.g. a dashboard
    button, a different kind of command) is added later — do not wire
    this back into the automatic call-handling flow.

    Best-effort. Unlike accept_call(), this targets FaceTime.app's own
    window via Accessibility — plausible this actually works, since once
    a call is answered, control genuinely transfers to a normal in-call
    FaceTime window (confirmed FaceTime.app becomes the active app then,
    from a live screenshot showing its menu bar) rather than staying on
    the unscriptable pre-answer banner. Still unverified — if this fails,
    the call is left open; log it loudly rather than silently leaving a
    customer connected to a Curant that's stopped responding."""
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


def _preflight_check_apis(cfg):
    """Fails fast at startup, before the poll loop ever begins, rather
    than mid-call. Checks two INDEPENDENT things that are easy to
    silently mismatch:

      1. HEAR/transcribe — transcribe() only needs a Gemini or OpenAI
         key present in api_keys, checked directly, regardless of
         which provider is configured for replies.
      2. UNDERSTAND/reply — get_reply() shells out to `curant-cli
         relay`, which uses config["provider"] (defaults to
         "anthropic" if unset — see curant-cli's DEFAULT_PROVIDER) to
         pick the model, and needs THAT provider's key specifically.

    These can drift apart in a confusing way: running only
    `curant-cli set-api-key <key> --provider gemini` sets up
    transcription fine, but does nothing to the configured provider —
    if it's still defaulted to "anthropic" (or whatever it was before)
    and no Anthropic key is set, calls would answer, transcribe the
    caller correctly, and then fail on every single reply. Catching
    that here means the failure is one clear line at startup instead
    of a mystery mid-call.

    SPEAK is real-local `say`/`afplay` (see speak()) — no API key
    involved, so nothing to check for it here."""
    problems = []

    gemini_key = _config_api_key(cfg, "gemini") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    openai_key = _config_api_key(cfg, "openai") or os.environ.get("OPENAI_API_KEY")
    if gemini_key:
        transcription_via = "Gemini (native audio)"
    elif openai_key:
        transcription_via = "OpenAI Whisper"
    else:
        transcription_via = None
        problems.append(
            "HEAR is not wired up: no Gemini or OpenAI key found for transcription. Run "
            "`curant-cli set-api-key <key> --provider gemini` (or --provider openai)."
        )

    reply_provider = (cfg.get("provider") or "anthropic").strip().lower()
    reply_key = _config_api_key(cfg, reply_provider)
    if reply_provider == "anthropic" and not reply_key:
        reply_key = cfg.get("anthropic_api_key")  # legacy field, matches curant-cli's own fallback
    if not reply_key:
        problems.append(
            f"UNDERSTAND/reply is not wired up: curant-cli is configured to use "
            f"'{reply_provider}' for replies (config[\"provider\"], default 'anthropic' if "
            f"never set), but no API key is stored for '{reply_provider}'. Either run "
            f"`curant-cli set-api-key <key> --provider {reply_provider}`, or switch providers "
            f"with `curant-cli set-provider gemini` to match your transcription key."
        )

    if problems:
        print("API preflight check FAILED — refusing to start:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)

    print(f"API preflight check passed — hear: {transcription_via}, "
          f"understand/reply: {reply_provider}, speak: local (say/afplay, no API key).")


# ─────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────

class _InterruptWatcher:
    """Barge-in support for speak(): while a reply is playing, watches
    the SAME already-running continuous-capture stream (no second
    device tap -- see _start_continuous_capture's docstring on why
    touching the capture device more than once per call is exactly the
    class of bug this whole file has spent most of its history fixing)
    for a newly-finalized segment with real speech in it. If the caller
    starts talking while Curant is still mid-reply, check() returns
    True and speak() kills playback immediately instead of running the
    reply out to the end.

    Granularity is bounded by TURN_RECORD_SECONDS -- segments only
    finalize every ~N seconds, so this is "notices within about one
    segment length that the caller started talking and cuts the reply
    off there," not sub-second barge-in. Still a real, meaningful
    change from before: previously nothing the caller said could
    interrupt a reply already in progress, no matter how long it ran.

    Silent segments that finalize during playback (caller didn't say
    anything, just normal quiet) are discarded and watching moves on to
    the next one -- they're not interruptions and aren't worth keeping.
    """

    def __init__(self, pattern, start_index):
        self.pattern = pattern
        self.next_check_index = start_index
        self.found = None  # (raw_wav_path, mono_wav_path) once triggered

    def check(self):
        if self.found is not None:
            return True
        seg = self.pattern % self.next_check_index
        nxt = self.pattern % (self.next_check_index + 1)
        if not (os.path.exists(seg) and os.path.exists(nxt)):
            return False  # this segment isn't finalized yet -- nothing new to check
        try:
            mono = _extract_loudest_channel_mono(seg)
        except Exception as e:
            print(f"  [{_ts()}] Interrupt-watch: could not extract a channel from {seg} "
                  f"({e}) -- using it as-is.", file=sys.stderr)
            mono = seg
        has_speech, _rms = _wav_has_speech(mono, threshold=SILENCE_RMS_THRESHOLD)
        if has_speech:
            print(f"  [{_ts()}] Caller spoke during reply playback -- interrupting.")
            self.found = (seg, mono)
            return True
        # Finalized but silent -- not an interruption. Discard and move
        # the watch window forward to the next segment.
        try:
            if os.path.exists(seg):
                os.remove(seg)
            if mono != seg and os.path.exists(mono):
                os.remove(mono)
        except Exception:
            pass
        self.next_check_index += 1
        return False

    def resume_index(self):
        """Where the main loop's turn_index should resume after playback
        ends -- past whatever silent segments were already discarded,
        and (if triggered) one further past the segment that triggered
        the interrupt, since that segment is handed directly to the
        main loop for transcription instead of being waited for again."""
        return self.next_check_index + (1 if self.found else 0)


# Set once in main(), before Curant ever touches system audio -- what
# to restore to after each call (see handle_call()'s own finally block).
# Module-level (not local to main()) since handle_call() needs to read
# it too and this file's functions aren't otherwise wired for passing
# extra state through poll_for_incoming_call() -> handle_call().
_ORIGINAL_INPUT_DEVICE = None
_ORIGINAL_OUTPUT_DEVICE = None


def handle_call(window_desc, apple_id, dry_run):
    print(f"[{_ts()}] Incoming call detected: {window_desc!r}")
    cfg = _load_config()
    approved, reason = caller_is_approved(window_desc, cfg)
    print(f"  access check: {'PASS' if approved else 'REFUSED'} — {reason}")
    if not approved:
        return

    if dry_run:
        print("  [dry-run] would click Accept now. Stopping here.")
        return

    # REVERTED (2026-08-20) back to the startup-only switch, per this
    # comment's own documented fallback plan: per-call switching (right
    # here, before accept_call()) was tried again per explicit request,
    # and confirmed live -- twice, consistently silent, RMS never above
    # ~2 against a threshold of 15 -- to reproduce the exact previously-
    # documented failure mode this comment warned about: FaceTime
    # negotiates/locks its own audio session the moment a call starts
    # RINGING or connects, before or despite this script's switch, and
    # our system-default change either doesn't take or gets silently
    # overridden. One early test call DID work with per-call switching,
    # but two follow-ups didn't -- consistent with a race this script
    # cannot reliably win, not a config problem (Multi-Output Device
    # "Curant Call Output" checked fine both times). Devices are now
    # switched to BlackHole once in main(), before the poll loop starts,
    # and held for the process's entire life -- the same tradeoff this
    # had before the per-call experiment: this Mac's normal mic/speakers
    # are unavailable for other things the whole time the FaceTime
    # answering service is running, in exchange for calls actually
    # working.
    ok, detail = accept_call()
    if not ok:
        print(f"  [{_ts()}] Failed to accept call: {detail}", file=sys.stderr)
        return
    print(f"  [{_ts()}] Accepted: {detail}")

    speak(GREETING_TEXT, prerendered_aiff=_GREETING_AIFF)
    print(f"  [{_ts()}] Greeting playback finished.")

    # Per-call proof that the capture path works, run BEFORE the caller
    # has had a real chance to say anything -- see
    # audio_capture_selftest()'s docstring for why this exists at all
    # (every distinct silent-call cause produces an identical-looking
    # log without it). Deliberately re-run per call, not just once at
    # startup: the system default output has been observed changing
    # out from under this process mid-session, so a pass at startup is
    # not a guarantee for a call minutes later.
    # The tone self-test only means anything for the BlackHole backend,
    # where we capture a system output device that our own tone also
    # plays into. The ScreenCaptureKit tap captures FACETIME's audio
    # specifically and deliberately excludes this process's own output
    # (excludesCurrentProcessAudio), so a tone played by Curant is
    # CORRECTLY not captured -- running the test there would report a
    # scary, meaningless failure on a perfectly healthy setup.
    if not audiotap_available():
        _st_ok, _st_detail = audio_capture_selftest()
        if _st_ok:
            print(f"  [{_ts()}] Audio capture self-test: {_st_detail}")
        else:
            print(f"  [{_ts()}] AUDIO CAPTURE SELF-TEST FAILED: {_st_detail}. Every turn of this "
                  f"call will read as silent -- this is a routing/config problem, NOT the caller "
                  f"being quiet. See the startup log for the full checklist.", file=sys.stderr)

    # Fast, direct hangup detection (see _watch_for_call_end()'s docstring
    # for why this is a separate throttled thread, not an inline check).
    # call_ended_event is threaded through to _wait_for_next_turn_segment()
    # below and checked at the top of the main loop, and is always set in
    # this function's own finally block so the watcher thread exits with
    # the call regardless of which path out of this function is taken.
    #
    # baseline_text: a snapshot of the call-end OCR region taken RIGHT
    # NOW, before any real hangup could possibly have happened yet --
    # see _call_end_banner_detected()'s docstring for the real, live bug
    # this fixes (stale "<number> left" text sitting in a Terminal
    # window within the OCR region matched and hung up a real, still-
    # active call). Best-effort: if the snapshot itself fails, fall back
    # to no baseline (old behavior) rather than blocking call handling
    # on it.
    try:
        _call_end_baseline_text, _ = _raw_call_end_region_text()
    except Exception:
        _call_end_baseline_text = None
    call_ended_event = threading.Event()
    call_end_watcher = threading.Thread(
        target=_watch_for_call_end, args=(call_ended_event,),
        kwargs={"baseline_text": _call_end_baseline_text}, daemon=True
    )
    call_end_watcher.start()

    # CURANT NEVER ENDS THE CALL — only the human can, by hanging up on
    # their own end. Per explicit direction. Two things changed to make
    # that true, not just claimed:
    #   1. No more keyword-triggered hangup. The old code broke out of
    #      the loop (and then called hang_up()) the moment the caller's
    #      transcribed speech contained "bye"/"goodbye"/"hang up"/"that's
    #      all" — but a misheard or hallucinated word (see _wav_has_speech
    #      and the silence-gate history above) could trigger that
    #      wrongly, and even a correct transcript is still Curant
    #      deciding to end a call a human didn't actually ask it to end.
    #      Gone entirely — nothing the caller says can end the call now.
    #   2. No more MAX_CALL_TURNS hangup. The old loop ran for a bounded
    #      number of turns and then called hang_up() unconditionally when
    #      it ran out — that's still Curant ending the call, just on a
    #      timer instead of a keyword. Loop is unbounded now (while True)
    #      — the ONLY way this function returns is detecting the human
    #      has already ended the call themselves (see below), never by
    #      actively hanging up itself. hang_up() (still defined above)
    #      is intentionally never called from anywhere in this file
    #      anymore — kept only in case a future, explicit, human-
    #      initiated hangup control is added.
    # Caller audio is now captured by ONE persistent ffmpeg process for
    # the entire call, split into rolling per-turn segments by ffmpeg's
    # own segment muxer -- NOT one fresh ffmpeg invocation per turn (the
    # old record_caller_audio() approach). Real suspicion behind this
    # change: live testing showed a call still dropping at an
    # unpredictable point despite every previously-fixed device-timing
    # bug being genuinely fixed, and repeatedly opening/closing a capture
    # session on the SAME device FaceTime is actively rendering into,
    # mid-call, is the same class of problem per-turn device SWITCHING
    # already turned out to be. See _start_continuous_capture()'s
    # docstring for the full reasoning.
    capture_process = None
    segment_dir = None
    utterance = None  # defined here, not just inside the try below, so the finally can always
                      # clean it up even if _start_continuous_capture() itself raises first
    try:
        capture_process, segment_dir, pattern, capture_backend = start_caller_capture(TURN_RECORD_SECONDS)
        print(f"  [{_ts()}] Started continuous caller-audio capture (pid {capture_process.pid}).")

        turn_index = 0
        # pending_wav, when set, is caller audio that's ALREADY been
        # captured and confirmed to have speech on it -- specifically,
        # the segment that triggered an _InterruptWatcher barge-in
        # during the previous reply's playback. When set, the top of
        # the loop processes it immediately instead of calling
        # _wait_for_next_turn_segment() again (which would otherwise
        # just re-wait for a segment we already have in hand).
        pending_wav = None
        # See HANGUP_CONSECUTIVE_TRUE_SILENT_TURNS's docstring above --
        # counts consecutive turns with true (not just below-speech-
        # threshold) silence. Reset to 0 by ANY real signal (speech, or
        # even just nonzero ambient room noise); once it reaches the
        # limit, this function returns so main()'s outer loop can go
        # back to polling for a genuinely new incoming call instead of
        # staying stuck babysitting a call that's already over.
        consecutive_true_silent_turns = 0
        # Speech is accumulated here across segments and only sent for
        # transcription once the caller pauses -- see _UtteranceBuffer's
        # docstring for the (confirmed-live) empty-transcript bug that
        # fixed-window-per-turn transcription caused.
        utterance = _UtteranceBuffer()
        _turn_start = None  # set when a pause is detected; see the END-TO-END CLOCK note below.
                            # Initialised here so the latency log can never raise NameError on a
                            # path that reaches the reply block without going through a flush.
        while True:
            # REMOVED per explicit direction: no more _call_is_still_
            # connected() check here at all -- not once per turn, not
            # ever, during the live-call loop. A call still dropped on
            # its own even after cutting this down to once per turn (see
            # _wait_for_next_turn_segment's docstring for that earlier
            # step), and every remaining osascript invocation is a
            # remaining suspect for whatever's triggering FaceTime's own
            # auto-disconnect behavior. Real, accepted tradeoff: this
            # loop can no longer detect a real hangup at all and will
            # keep running (recording, transcribing on real speech,
            # replying) into a dead call until the process is killed
            # manually -- deliberately preferred over any chance of an
            # AppleScript check itself contributing to a drop. Curant
            # still never hangs up itself either way.

            if call_ended_event.is_set():
                print(f"  [{_ts()}] Call end detected (OCR banner match) -- "
                      f"returning to listen for a new incoming call.")
                return

            if pending_wav is not None:
                raw_wav_path, wav_path = pending_wav
                pending_wav = None
                print(f"  [{_ts()}] Processing caller audio that interrupted the last reply: "
                      f"{os.path.basename(raw_wav_path)}")
            else:
                raw_wav_path = _wait_for_next_turn_segment(pattern, turn_index, stop_event=call_ended_event)
                if call_ended_event.is_set():
                    print(f"  [{_ts()}] Call end detected (OCR banner match) -- "
                          f"returning to listen for a new incoming call.")
                    return
                if raw_wav_path is None:
                    # This specific segment timed out or never got written
                    # (e.g. the capture process died, which itself often
                    # means the call ended and the audio device vanished)
                    # -- counts toward the same hangup counter as true
                    # silence, since it's the same underlying signal: no
                    # real audio coming through anymore.
                    consecutive_true_silent_turns += 1
                    if consecutive_true_silent_turns >= HANGUP_CONSECUTIVE_TRUE_SILENT_TURNS:
                        print(f"  [{_ts()}] Assuming the call has ended -- "
                              f"{consecutive_true_silent_turns} consecutive turns with no real audio "
                              f"(segments timing out / true silence). Returning to listen for a new call.")
                        return
                    time.sleep(RECORDING_FAILURE_RETRY_SECONDS)
                    turn_index += 1  # don't get stuck waiting on the same missing segment forever
                    continue
                turn_index += 1
                print(f"  [{_ts()}] Turn segment ready: {os.path.basename(raw_wav_path)}")

                try:
                    wav_path = _extract_loudest_channel_mono(raw_wav_path)
                except Exception as e:
                    print(f"  [{_ts()}] Could not extract a channel from {raw_wav_path} ({e}) -- "
                          f"using it as-is.", file=sys.stderr)
                    wav_path = raw_wav_path

            # DEBUG AID: if CURANT_FACETIME_DEBUG_KEEP_AUDIO is set, copy
            # every turn's segment to that directory instead of just
            # deleting it after the silence check -- added live after
            # every turn of a real call kept reading "silent" and there
            # was no way to actually listen to what was captured to tell
            # whether that was really true. Off by default (adds disk
            # I/O and leaves files behind) -- only meant to be turned on
            # for a single diagnostic test call. Copies BOTH the raw
            # 16-channel capture and the extracted mono channel so a
            # bad extraction can be told apart from genuinely no signal
            # anywhere in the raw capture.
            debug_keep_dir = os.environ.get("CURANT_FACETIME_DEBUG_KEEP_AUDIO")
            if debug_keep_dir:
                try:
                    os.makedirs(debug_keep_dir, exist_ok=True)
                    import shutil as _shutil
                    _shutil.copy2(raw_wav_path, os.path.join(debug_keep_dir, os.path.basename(raw_wav_path)))
                    if wav_path != raw_wav_path:
                        _shutil.copy2(wav_path, os.path.join(debug_keep_dir, os.path.basename(wav_path)))
                except Exception as e:
                    print(f"  [{_ts()}] Could not copy debug audio: {e}", file=sys.stderr)

            try:
                has_speech, rms = _wav_has_speech(wav_path)
                # See HANGUP_CONSECUTIVE_TRUE_SILENT_TURNS's docstring --
                # rms is None only when the file couldn't be read at all
                # (inconclusive, don't touch the counter either way).
                # Real nonzero ambient noise (a connected-but-quiet call)
                # resets it; sustained true-zero RMS builds toward
                # assuming the call has actually ended.
                if rms is not None:
                    if rms <= TRUE_SILENCE_RMS_THRESHOLD:
                        consecutive_true_silent_turns += 1
                        if consecutive_true_silent_turns >= HANGUP_CONSECUTIVE_TRUE_SILENT_TURNS:
                            print(f"  [{_ts()}] Assuming the call has ended -- "
                                  f"{consecutive_true_silent_turns} consecutive turns of true digital "
                                  f"silence (RMS <= {TRUE_SILENCE_RMS_THRESHOLD}), no real audio source "
                                  f"detected. Returning to listen for a new incoming call.")
                            return
                    else:
                        consecutive_true_silent_turns = 0
                # UTTERANCE ENDPOINTING (see _UtteranceBuffer): a
                # segment with speech is buffered, not transcribed on
                # its own. Only a pause (or a very long monologue) ends
                # the utterance and triggers one transcription of the
                # whole thing, so a sentence that happens to straddle a
                # segment boundary is no longer torn into two
                # untranscribable fragments.
                try:
                    _seg_seconds = os.path.getsize(wav_path) / float(16000 * 2)
                except Exception:
                    _seg_seconds = TURN_RECORD_SECONDS
                if has_speech:
                    utterance.add_speech(wav_path, _seg_seconds)
                    if not utterance.should_flush():
                        print(f"  [{_ts()}] Speech detected -- buffering "
                              f"({utterance.seconds:.1f}s so far), waiting for the caller to pause.")
                        continue
                    print(f"  [{_ts()}] Utterance hit the {MAX_UTTERANCE_SECONDS:.0f}s cap -- "
                          f"transcribing what we have so far.")
                    _turn_start = time.monotonic()
                else:
                    utterance.note_silence(wav_path)
                    if not utterance.has_audio():
                        print(f"  [{_ts()}] Clip looked silent -- nothing buffered, still listening.")
                        continue
                    if not utterance.should_flush():
                        continue
                    print(f"  [{_ts()}] Caller paused -- transcribing {utterance.seconds:.1f}s "
                          f"of buffered speech ({len(utterance.paths)} segment(s)).")
                # END-TO-END CLOCK starts the moment the pause is
                # detected -- i.e. the moment the caller has finished
                # talking and starts waiting. Every other timer in this
                # loop measures one stage in isolation; this is the only
                # number that corresponds to what the person on the
                # phone actually experiences as "how long until it
                # answered me".
                _turn_start = time.monotonic()
                # TIMED (2026-08-08, added per explicit request to make
                # transcription/response faster): rather than guess at
                # what's slow, measure the two real API round trips
                # directly so the NEXT live test shows exactly where
                # the time is going instead of another round of
                # speculation. See get_reply() below for the matching
                # timer on the reply side.
                _transcribe_start = time.monotonic()
                _utterance_wav = utterance.build() or wav_path
                try:
                    text = transcribe_with_retry(_utterance_wav, cfg)
                except Exception as e:
                    # Real bug found live (2026-08-20): a transient
                    # Gemini "503 UNAVAILABLE -- high demand" error during
                    # transcribe() was completely uncaught here. It
                    # propagated out of this whole function, past
                    # handle_call()'s own try/finally blocks (none of
                    # which have an `except`, only cleanup `finally`s),
                    # all the way up to main()'s poll-loop catch-all --
                    # which printed "Unexpected error in poll loop
                    # (continuing)" and went back to LISTENING FOR A NEW
                    # CALL, silently abandoning the real, still-connected
                    # one. Confirmed live: that exact call had genuine
                    # captured speech (RMS 275.1, clearly real, well
                    # above threshold) that never got a chance to
                    # generate a reply at all -- not an audio problem,
                    # a single transient API error killing the entire
                    # call. A quota/overload error on ONE turn's
                    # transcription should cost that turn, not the
                    # whole conversation.
                    print(f"  [{_ts()}] Transcription failed this turn ({e}) -- "
                          f"treating as no speech and continuing to listen.", file=sys.stderr)
                    text = None
                _transcribe_elapsed = time.monotonic() - _transcribe_start
                if not text:
                    # Preserve the actual audio BEFORE the finally block
                    # below deletes it -- added after two rounds of pure
                    # guessing (segment-boundary theory, then a widened
                    # TURN_RECORD_SECONDS) failed to fully explain repeat
                    # empty transcripts on clips with real, well-above-
                    # threshold RMS. Saved copies let this actually be
                    # LISTENED to instead of guessed at blind. Best-effort
                    # -- a failure to save must never break the call.
                    try:
                        debug_dir = os.path.expanduser("~/.curant/logs/empty_transcript_clips")
                        os.makedirs(debug_dir, exist_ok=True)
                        saved_path = os.path.join(
                            debug_dir, f"{_ts().replace(':', '-')}_{os.path.basename(wav_path)}"
                        )
                        shutil.copy2(_utterance_wav, saved_path)
                        print(f"  [{_ts()}] Saved failing clip for inspection: {saved_path}",
                              file=sys.stderr)
                    except Exception as e:
                        print(f"  [{_ts()}] Could not save failing clip (non-fatal): {e}",
                              file=sys.stderr)
            finally:
                if os.path.exists(raw_wav_path):
                    os.remove(raw_wav_path)
                if wav_path != raw_wav_path and os.path.exists(wav_path):
                    os.remove(wav_path)
            # This utterance has now been transcribed (successfully or
            # not) -- clear the buffer so the next one starts clean
            # instead of re-sending audio that was already sent once.
            # Deliberately AFTER the finally above: the failing-clip
            # save inside the try still needs the joined utterance file,
            # which lives in the buffer's own temp dir.
            utterance.reset()
            if not text:
                # Previously silent here -- indistinguishable in the logs
                # from a turn that never had speech at all. Real bug found
                # live (2026-08-20): after the Multi-Output Device fix
                # finally got real caller audio flowing, TWO consecutive
                # turns both measured well above SILENCE_RMS_THRESHOLD
                # (RMS 694 and 120) -- genuine speech, confirmed audible --
                # yet neither produced a transcript, and this branch just
                # silently moved on with zero trace of what happened.
                # Suspected cause, not yet confirmed: TURN_RECORD_SECONDS
                # (2s) slicing one continuous utterance across a segment
                # boundary, leaving two partial/unclear fragments that
                # Gemini's transcription correctly declines to guess at
                # (it's explicitly instructed to return empty rather than
                # hallucinate -- see _transcribe_gemini's prompt). Raising
                # TURN_RECORD_SECONDS to 3 did NOT fully fix this --
                # confirmed live, still recurring -- so the clip is now
                # also saved to disk (see above) for actual inspection
                # instead of further guessing.
                print(f"  [{_ts()}] Had speech (RMS above threshold) but got an empty "
                      f"transcript back (transcription took {_transcribe_elapsed:.2f}s) -- "
                      f"clip saved for inspection, see stderr log for the path.")
                continue  # likely silence in this window — just listen again
            print(f"  [{_ts()}] Caller said: {text} (transcription took {_transcribe_elapsed:.2f}s)")
            _reply_start = time.monotonic()

            # STREAMING per explicit request to cut perceived call
            # latency: sentences are spoken as they're generated,
            # not after the whole reply is ready -- see
            # get_reply_streaming()'s docstring. Barge-in (see
            # _InterruptWatcher's docstring) uses ONE watcher shared
            # across every sentence in this turn, same as before,
            # just now potentially checked across several speak()
            # calls instead of one.
            watcher = _InterruptWatcher(pattern, turn_index)
            sentences_spoken = []
            first_chunk_time = [None]  # list, not a plain var -- mutated from the nested callback below
            interrupted_holder = [False]

            def _speak_sentence(sentence):
                if first_chunk_time[0] is None:
                    first_chunk_time[0] = time.monotonic()
                if interrupted_holder[0]:
                    return  # already interrupted this turn -- don't keep speaking later sentences
                sentences_spoken.append(sentence)
                was_interrupted = speak(sentence, interrupt_check=watcher.check)
                if was_interrupted and watcher.found:
                    interrupted_holder[0] = True

            # Three-layer fallback so a caller is never left in silence
            # after Curant has already heard them:
            #   1. streaming reply (fastest -- speaks sentence by sentence)
            #   2. plain non-streaming reply, if streaming itself broke
            #      (a bug in the stream plumbing shouldn't cost a reply
            #      that the model can still perfectly well generate)
            #   3. a spoken apology, so the turn still ENDS in speech
            # Layer 2 is skipped when layer 1 already spoke something --
            # re-answering a question the caller has partly heard
            # answered would be worse than just moving on.
            reply = None
            try:
                reply = get_reply_streaming(text, apple_id, _speak_sentence)
            except Exception as e:
                print(f"  [{_ts()}] Streaming reply failed: {e}", file=sys.stderr)
                if not sentences_spoken:
                    try:
                        print(f"  [{_ts()}] Falling back to non-streaming reply.", file=sys.stderr)
                        reply = get_reply(text, apple_id)
                        if reply:
                            _speak_sentence(reply)
                    except Exception as e2:
                        print(f"  [{_ts()}] Non-streaming reply also failed: {e2}", file=sys.stderr)
                        reply = None
            if reply is None and not sentences_spoken:
                speak("Sorry, I ran into a problem there — could you say that again?",
                      interrupt_check=watcher.check)

            _reply_elapsed = time.monotonic() - _reply_start
            turn_index = watcher.resume_index()
            if reply:
                _first_word_elapsed = (first_chunk_time[0] - _reply_start) if first_chunk_time[0] else _reply_elapsed
                _e2e = (first_chunk_time[0] - _turn_start) if (first_chunk_time[0] and _turn_start) else None
                _e2e_txt = f"{_e2e:.2f}s" if _e2e is not None else "n/a"
                print(f"  [{_ts()}] Curant says: {reply}")
                # Stage breakdown, printed every turn so a slow call can
                # be attributed instead of guessed at: END-TO-END is
                # what the caller feels; the rest says which stage owns
                # it. hear = pause detection -> transcript in hand,
                # think = transcript -> first token of the reply.
                print(f"  [{_ts()}] LATENCY end-to-end {_e2e_txt} "
                      f"(hear+transcribe {_transcribe_elapsed:.2f}s, "
                      f"think->first words {_first_word_elapsed:.2f}s, "
                      f"full reply {_reply_elapsed:.2f}s)")
            if interrupted_holder[0]:
                print(f"  [{_ts()}] Reply playback interrupted by caller.")
                pending_wav = watcher.found  # (raw_wav_path, mono_wav_path) — process next loop iteration
            else:
                print(f"  [{_ts()}] Reply playback finished.")
    finally:
        if utterance is not None:
            try:
                utterance.cleanup()  # buffered utterance temp dir -- see _UtteranceBuffer
            except Exception:
                pass
        call_ended_event.set()  # stop the watcher thread regardless of how this function returns
        if capture_process is not None:
            _stop_continuous_capture(capture_process)
        if segment_dir is not None:
            shutil.rmtree(segment_dir, ignore_errors=True)  # shutil is module-level now, see top of file

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apple-id", default=None,
                     help="Treat calls as coming from this Apple ID for curant-cli context "
                          "(defaults to the configured customer_apple_id)")
    ap.add_argument("--dry-run", action="store_true",
                     help="Detect calls and log what would happen, but never click Accept or speak")
    ap.add_argument("--transcribe-file", metavar="WAV", default=None,
                     help="Transcribe one WAV through the exact production path and exit. "
                          "Isolates the hear->transcribe stage from call capture entirely.")
    ap.add_argument("--record-seconds", type=float, default=None, metavar="N",
                     help="With --transcribe-file, first record N seconds from the given "
                          "input device into that path (see --record-device).")
    ap.add_argument("--record-device", default=None, metavar="NAME",
                     help="Input device name to record from (default: the Mac's current "
                          "system input). Use with --record-seconds.")
    args = ap.parse_args()

    # ---- Isolated transcription test -------------------------------------
    # Added because every FaceTime failure so far has been a CAPTURE
    # failure, and capture failures and transcription failures produce
    # identical-looking logs ("Clip looked silent" / empty transcript).
    # This runs the same _wav_has_speech -> transcribe_with_retry path a
    # real turn uses, on audio whose provenance is known, so the two can
    # be told apart instead of guessed at.
    if args.transcribe_file:
        cfg = _load_config()
        wav = os.path.expanduser(args.transcribe_file)
        if args.record_seconds:
            device = args.record_device or _get_current_device("input")
            if not device:
                print("Could not determine an input device to record from.", file=sys.stderr)
                sys.exit(1)
            idx = _find_avfoundation_audio_device_index(device)
            if idx is None:
                print(f"No avfoundation capture device named {device!r}.", file=sys.stderr)
                sys.exit(1)
            print(f"Recording {args.record_seconds}s from {device!r} -- speak now...")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "avfoundation", "-i", f":{idx}",
                 "-t", str(args.record_seconds), "-ar", "16000", "-ac", "1", wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            print(f"Recorded to {wav}")
        if not os.path.exists(wav):
            print(f"No such file: {wav}", file=sys.stderr)
            sys.exit(1)

        mono = _extract_loudest_channel_mono(wav)
        has_speech, rms = _wav_has_speech(mono)
        print(f"Silence gate: RMS={rms} threshold={SILENCE_RMS_THRESHOLD} -> "
              f"{'HAS SPEECH' if has_speech else 'SILENT'}")
        if not has_speech:
            print("This clip would be SKIPPED by a real turn (never sent for transcription).")
            print("If you can hear speech in it, the silence threshold is wrong for this audio.")
        t0 = time.monotonic()
        try:
            text = transcribe_with_retry(mono, cfg)
        except Exception as e:
            print(f"Transcription FAILED: {e}", file=sys.stderr)
            sys.exit(2)
        elapsed = time.monotonic() - t0
        if mono != wav and os.path.exists(mono):
            os.remove(mono)
        if text:
            print(f"\nTRANSCRIPT ({elapsed:.2f}s): {text}")
        else:
            print(f"\nEMPTY TRANSCRIPT ({elapsed:.2f}s) -- audio reached the model, "
                  f"which returned nothing. Listen to the file: afplay {wav}")
        sys.exit(0)

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
    print(f"  call access mode: {_read_call_access_mode(cfg)}")

    global _ORIGINAL_INPUT_DEVICE, _ORIGINAL_OUTPUT_DEVICE

    if not args.dry_run:
        _preflight_check_apis(cfg)

        # REVERTED (2026-08-20) back to the startup-only switch -- see
        # handle_call()'s comment at the same timestamp for the full
        # story. Per-call switching (right before accept_call(), reverted
        # once the call ended) was tried again per explicit request, and
        # confirmed live -- twice, consistently silent, against one
        # earlier call that did work -- to still hit the exact failure
        # this approach was already known to risk: FaceTime not actually
        # routing audio through whatever the system default is switched
        # to at call time. Back to switching once here, before the poll
        # loop starts, and holding for the process's entire life -- this
        # Mac's normal mic/speakers are unavailable for anything else
        # while this service runs, same tradeoff as the very first
        # version of this script, in exchange for calls reliably working.
        _ORIGINAL_INPUT_DEVICE = _get_current_device("input")
        _ORIGINAL_OUTPUT_DEVICE = _get_current_device("output")
        print(f"  Current system audio devices before switching — "
              f"input: {_ORIGINAL_INPUT_DEVICE}, output: {_ORIGINAL_OUTPUT_DEVICE}")
        if SYSTEM_OUTPUT_DEVICE == CALLER_AUDIO_DEVICE:
            print(f"  NOTE: system output is bare '{CALLER_AUDIO_DEVICE}', not a Multi-Output "
                  f"Device — confirmed live this produces total digital silence for FaceTime call "
                  f"audio (RMS=0.0 across all 16 channels during a real connected call). Set "
                  f"CURANT_FACETIME_SYSTEM_OUTPUT_DEVICE to a Multi-Output Device name (Audio MIDI "
                  f"Setup) that includes {CALLER_AUDIO_DEVICE!r} before expecting hearing to work.",
                  file=sys.stderr)
        # INPUT is still switched to BlackHole 2ch on every backend:
        # that is how Curant's spoken replies reach FaceTime, which
        # reads the system input device as its "microphone". Nothing
        # about ScreenCaptureKit changes the SPEAKING direction.
        if not set_system_input_device(TTS_OUTPUT_DEVICE):
            print("  System input device switch failed at startup -- calls will likely "
                  "not be heard by the caller.", file=sys.stderr)

        # OUTPUT is only hijacked for the BlackHole capture path, which
        # needs the Mac's default output to be a Multi-Output Device
        # feeding BlackHole 16ch so that FaceTime's audio (in theory)
        # lands somewhere capturable. The ScreenCaptureKit tap reads
        # FaceTime's audio directly and does not care what the default
        # output is -- so on that backend we leave the customer's real
        # speakers completely alone. That removes the single worst
        # tradeoff this feature had: previously the Mac's normal audio
        # output was commandeered for the entire life of the service,
        # not just during calls.
        if audiotap_available():
            print(f"  Switched system INPUT to {TTS_OUTPUT_DEVICE} (so FaceTime hears Curant). "
                  f"System OUTPUT left untouched at {_ORIGINAL_OUTPUT_DEVICE!r} -- the "
                  f"ScreenCaptureKit tap does not need it, so your speakers keep working "
                  f"normally while this service runs.")
        else:
            if not set_system_output_device(SYSTEM_OUTPUT_DEVICE):
                print("  System output device switch failed at startup -- calls will likely "
                      "not be heard BY Curant.", file=sys.stderr)
            print(f"  Switched system audio devices for the life of this process — "
                  f"input: {TTS_OUTPUT_DEVICE}, output: {SYSTEM_OUTPUT_DEVICE}. "
                  f"(Will NOT revert until this process exits -- your Mac's normal speakers "
                  f"are unavailable for other things while this service is running. Building "
                  f"the ScreenCaptureKit tap removes this tradeoff entirely.)")
        # Report (never silently "fix") output-device configurations
        # known to make calls silent -- see audit_output_devices().
        # Only relevant to the BlackHole path: the ScreenCaptureKit tap
        # doesn't route through an output device at all, so stray
        # Multi-Output devices are harmless there and warning about
        # them would be noise.
        if not audiotap_available():
            audit_output_devices()
        if prerender_greeting():
            print("  Greeting audio pre-rendered once -- calls skip TTS synthesis on answer.")
        if audiotap_available():
            print("  Capture backend: ScreenCaptureKit app tap (hearing does NOT depend on "
                  "BlackHole, the Multi-Output Device, or the system default output).")
            print("  Skipping the output-device tone self-test -- not meaningful for this "
                  "backend, which taps FaceTime's own audio directly.")
            ok, detail = True, "n/a (ScreenCaptureKit backend)"
        else:
            print("  Capture backend: BlackHole/ffmpeg (the ScreenCaptureKit tap is disabled by "
                  "default -- macOS was measured NOT to expose FaceTime call audio to it; see "
                  "audiotap_available()'s docstring for the numbers).")
            ok, detail = audio_capture_selftest()
        if ok:
            print(f"  Audio capture self-test: {detail}")
        else:
            print(f"  AUDIO CAPTURE SELF-TEST FAILED: {detail}.\n"
                  f"    Calls will be answered and Curant will still speak, but it will not "
                  f"hear ANYTHING the caller says until this is fixed.\n"
                  f"    Check, in this order: (1) does a Multi-Output Device named "
                  f"{SYSTEM_OUTPUT_DEVICE!r} exist in Audio MIDI Setup with "
                  f"{CALLER_AUDIO_DEVICE!r} CHECKED as one of its outputs and Drift "
                  f"Correction enabled on it; (2) is the system output still "
                  f"{SYSTEM_OUTPUT_DEVICE!r} (another app opening an audio device can "
                  f"silently steal it); (3) during a live call, does the FaceTime call "
                  f"window's own audio control name {SYSTEM_OUTPUT_DEVICE!r} -- if it names "
                  f"a different Multi-Output Device, FaceTime is routing elsewhere and this "
                  f"script cannot hear it.", file=sys.stderr)

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
