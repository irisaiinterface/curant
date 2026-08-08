#!/usr/bin/env python3
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

ACCESS CONTROL: honors the same CURANT_ACCESS_MODE the watcher uses,
but with a real capability gap for calls specifically — see the
REGRESSION note above and caller_is_approved()'s docstring. "approved"
mode currently refuses every call; "open" mode answers every call, with
no verification of who's calling.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

def _ts():
    """Wall-clock timestamp (local time, millisecond precision) prefixed
    onto the key lifecycle prints below. Added after live debugging where
    figuring out exactly how many seconds elapsed between 'Accepted' and
    a call dropping required manually counting terminal scrollback --
    this makes that a direct read instead of a guess."""
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


CONFIG_PATH = os.path.expanduser("~/.curant/config.json")

CALL_POLL_INTERVAL_SECONDS = 2
TURN_RECORD_SECONDS = 5          # length of each caller-audio recording chunk
RECORDING_FAILURE_RETRY_SECONDS = 1  # brief pause before re-checking call state after a failed recording

# NOTE: there is deliberately no MAX_CALL_TURNS anymore. Per explicit
# direction, Curant must never be the one to end a call — only the human
# can, by hanging up on their own end. handle_call()'s loop is unbounded
# and exits only when _facetime_is_frontmost() reports the call has
# already ended; see handle_call() for the full reasoning.

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
    corroborating ps check below (FTConversationService actively
    running) narrows this back down to calls specifically.

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
    rather than papered over with a better process check."""
    r = _run_osascript(
        'tell application "System Events" to tell process "NotificationCenter" to get name of every window'
    )
    if r.returncode != 0:
        return None
    names = [n.strip() for n in (r.stdout or "").split(",")]
    if "Notification Center" not in names:
        return None
    if not _facetime_call_daemon_active():
        return None  # a banner is up, but not a FaceTime call specifically
    return "FaceTime call banner active (NotificationCenter window detected)"


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
    IMPORTANT REGRESSION vs. the text-based watcher: caller ID
    verification for FaceTime calls is NOT currently implemented. The
    old version of this function tried to read the caller's name/number
    out of the call window's title text — but live testing proved the
    banner exposes no text to Accessibility scripting at all (same
    finding that forced accept_call() over to visual detection). There's
    no caller-identifying string available to check against configured
    handles right now.

    The caller's number IS visible as rendered pixels in the banner
    (confirmed from screenshots) — OCR (e.g. via Vision framework or
    pytesseract) could read it back out and restore real verification,
    but that's unbuilt. Until then, this fails CLOSED: 'approved' mode
    (the safe default) refuses every call, since it cannot tell who's
    calling. Only 'open' mode (already an explicit, documented
    no-allowlist choice for the text watcher too) will actually answer.
    """
    mode = _read_access_mode(cfg)
    if mode == "open":
        return True, "open access mode (caller ID verification not implemented for calls)"
    return False, (
        "approved mode requires verifying the caller, but caller-ID text isn't "
        "readable from the call banner (Accessibility scripting can't see it — "
        "confirmed by testing) — refusing to auto-answer rather than answer an "
        "unverified caller. Set CURANT_ACCESS_MODE=open if you want this Mac to "
        "auto-answer any FaceTime call regardless of who it's from, or build OCR-based "
        "caller-ID reading before trusting 'approved' mode for calls."
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


TTS_VOICE = "Alex"  # explicit `say` voice -- see speak()'s docstring for why this is required, not optional

_SOX_AVAILABLE = None  # cached shutil.which("sox") result


def _sox_available():
    global _SOX_AVAILABLE
    if _SOX_AVAILABLE is None:
        import shutil
        _SOX_AVAILABLE = shutil.which("sox") is not None
    return _SOX_AVAILABLE


def speak(text, device_name=None):
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
    sidesteps whatever is wrong with this Mac's actual default."""
    fd, aiff_path = tempfile.mkstemp(suffix=".aiff")
    os.close(fd)
    try:
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
            subprocess.run(["sox", aiff_path, "-t", "coreaudio", target_device],
                            check=True, timeout=60)
        else:
            print("  sox not found (brew install sox) — falling back to afplay via "
                  "system default output, which can drop a live call if the system "
                  "default isn't already this device. Install sox to fix properly.",
                  file=sys.stderr)
            subprocess.run(["afplay", aiff_path], check=True, timeout=60)
    finally:
        if os.path.exists(aiff_path):
            os.remove(aiff_path)


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


SEGMENT_WAIT_POLL_SECONDS = 0.5   # how often to check whether the next segment file has appeared
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


def _wait_for_next_turn_segment(pattern, index):
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
    once per turn, is what actually detects a real hangup now)."""
    this_segment = pattern % index
    next_segment = pattern % (index + 1)
    deadline = time.monotonic() + SEGMENT_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
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
    print(f"  [{_ts()}] {n_channels}-channel capture, per-channel RMS: "
          + ", ".join(f"ch{i}={r:.1f}" for i, r in enumerate(per_channel_rms))
          + f" -- using channel {best_channel} (loudest)", file=sys.stderr)

    mono_samples = np.ascontiguousarray(samples[:, best_channel])
    mono_path = multi_channel_path[:-4] + "_mono.wav" if multi_channel_path.endswith(".wav") \
        else multi_channel_path + "_mono.wav"
    with wave.open(mono_path, "wb") as w_out:
        w_out.setnchannels(1)
        w_out.setsampwidth(sampwidth)
        w_out.setframerate(sample_rate)
        w_out.writeframes(mono_samples.tobytes())
    return mono_path


SILENCE_RMS_THRESHOLD = 250.0  # int16 RMS units — tune per SETUP_FACETIME_CALLS.md notes


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
    or noisy room, lower it if real quiet speech gets skipped."""
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
        return True
    # Always print what was actually measured, not just the pass/fail
    # verdict -- added live after every single turn of a real 4+ minute
    # call came back "silent" despite the caller speaking clearly. This
    # makes the next test tell us whether the clip is genuinely near-
    # zero (routing/device problem) or just under-threshold real audio
    # (threshold too high), instead of guessing between those two very
    # different problems.
    print(f"  [{_ts()}] WAV check: {n_frames} frames, {n_channels}ch, {sample_rate}Hz "
          f"({n_frames / sample_rate if sample_rate else 0:.2f}s)", file=sys.stderr)
    if not frames:
        print(f"  [{_ts()}] WAV check: zero frames read -- empty/corrupt segment file.",
              file=sys.stderr)
        return False
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        print(f"  [{_ts()}] WAV check: zero samples after decode.", file=sys.stderr)
        return False
    rms = float(np.sqrt(np.mean(samples ** 2)))
    peak = float(np.max(np.abs(samples)))
    print(f"  [{_ts()}] WAV check: RMS={rms:.1f} peak={peak:.0f} threshold={threshold:.0f} "
          f"-> {'HAS SPEECH' if rms >= threshold else 'silent'}", file=sys.stderr)
    return rms >= threshold


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


def _transcribe_gemini(wav_path, api_key):
    """Gemini's native audio understanding — no separate transcription
    service needed if you're already on Gemini for replies. Uses the
    same native google-genai SDK as curant-cli's Gemini tool-calling
    path (not the OpenAI-compat shim, which doesn't reliably support
    audio input) and the same model curant-cli's PROVIDER_MODELS
    already pins for Gemini, so behavior stays consistent with the
    rest of Curant rather than picking a different model here."""
    from google import genai
    from google.genai import types as genai_types

    client = genai.Client(api_key=api_key)
    with open(wav_path, "rb") as f:
        audio_bytes = f.read()

    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=[
            "Transcribe this audio verbatim. Reply with ONLY the transcript "
            "text, nothing else — no commentary, no quotation marks. If "
            "there is no discernible speech (silence or just noise), reply "
            "with an empty string.",
            genai_types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
        ],
    )
    return (response.text or "").strip()


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

    # System input/output are set ONCE at startup, in main(), and never
    # touched again for the rest of this process's life — NOT per-call,
    # NOT per-turn. Two real bugs found live got us here, in order:
    #   1. Switching input AFTER accept_call() dropped every call at a
    #      fixed ~4 seconds in (FaceTime locks its mic in at connect
    #      time; changing it after looks like the mic vanished).
    #   2. Even after fixing #1 (input set before accept), switching
    #      OUTPUT per-turn (to CALLER_AUDIO_DEVICE for listening, back
    #      to TTS_OUTPUT_DEVICE for speaking) ALSO dropped the call —
    #      same class of problem, just for the other direction and
    #      later in the flow.
    # Fix: output now stays fixed at CALLER_AUDIO_DEVICE (BlackHole
    # 16ch) for the ENTIRE time this script runs, same as input at
    # TTS_OUTPUT_DEVICE (BlackHole 2ch) — see main(). Nothing left to
    # hot-swap mid-call, in either direction. speak() plays Curant's own
    # voice into BlackHole 2ch directly via SoX (device-targeted,
    # bypassing the system default entirely) instead of relying on
    # system output ever pointing there — see speak()'s docstring.

    ok, detail = accept_call()
    if not ok:
        print(f"  [{_ts()}] Failed to accept call: {detail}", file=sys.stderr)
        return
    print(f"  [{_ts()}] Accepted: {detail}")

    speak("Hi, this is Curant. I'm listening.")
    print(f"  [{_ts()}] Greeting playback finished.")

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
    try:
        capture_process, segment_dir, pattern = _start_continuous_capture(TURN_RECORD_SECONDS)
        print(f"  [{_ts()}] Started continuous caller-audio capture (pid {capture_process.pid}).")

        turn_index = 0
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

            wav_path = _wait_for_next_turn_segment(pattern, turn_index)
            if wav_path is None:
                # This specific segment timed out or never got written
                # (e.g. the capture process died) -- loop back and try
                # the next one rather than assuming the call is over.
                time.sleep(RECORDING_FAILURE_RETRY_SECONDS)
                turn_index += 1  # don't get stuck waiting on the same missing segment forever
                continue
            turn_index += 1
            print(f"  [{_ts()}] Turn segment ready: {os.path.basename(wav_path)}")

            raw_wav_path = wav_path
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
                if not _wav_has_speech(wav_path):
                    print(f"  [{_ts()}] Clip looked silent -- skipping transcription this turn.")
                    continue  # near-silent clip — skip the API call, don't risk a hallucinated transcript
                text = transcribe(wav_path, cfg)
            finally:
                if os.path.exists(raw_wav_path):
                    os.remove(raw_wav_path)
                if wav_path != raw_wav_path and os.path.exists(wav_path):
                    os.remove(wav_path)
            if not text:
                continue  # likely silence in this window — just listen again
            print(f"  [{_ts()}] Caller said: {text}")
            try:
                reply = get_reply(text, apple_id)
            except Exception as e:
                print(f"  [{_ts()}] Reply failed: {e}", file=sys.stderr)
                reply = "Sorry, I ran into a problem there — could you say that again?"
            if reply:
                print(f"  [{_ts()}] Curant says: {reply}")
                speak(reply)
                print(f"  [{_ts()}] Reply playback finished.")
    finally:
        if capture_process is not None:
            _stop_continuous_capture(capture_process)
        if segment_dir is not None:
            import shutil
            shutil.rmtree(segment_dir, ignore_errors=True)


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

    if not args.dry_run:
        _preflight_check_apis(cfg)

        # Set BlackHole as the SYSTEM default input/output BEFORE the poll
        # loop even starts, and NEVER touch either again for the rest of
        # this process's life — not per-call, not per-turn. Two real bugs
        # found live, in order, got us here:
        #   1. Switching devices per-call, even before accept_call(),
        #      wasn't early enough — the caller heard nothing, despite
        #      `SwitchAudioSource -c` confirming correct devices by
        #      accept time. FaceTime most likely negotiates its audio
        #      session the moment a call starts RINGING, before this
        #      script's poll loop even reacts. Moving the switch to
        #      startup closes that timing window entirely.
        #   2. Per-turn OUTPUT switching (input was already fixed, but
        #      output still flipped between TTS_OUTPUT_DEVICE and
        #      CALLER_AUDIO_DEVICE each turn) dropped every call anyway —
        #      turns out hot-swapping output mid-call is just as
        #      disruptive to FaceTime as hot-swapping input was. So
        #      output is now ALSO fixed here, permanently, at
        #      CALLER_AUDIO_DEVICE (not TTS_OUTPUT_DEVICE) — Curant's own
        #      speech instead goes directly to TTS_OUTPUT_DEVICE via SoX
        #      in speak(), bypassing the system default output so it
        #      never needs to change.
        #
        # REAL TRADEOFF, not hidden: while this script is running, this
        # Mac's system microphone input is BlackHole 2ch and its output
        # is BlackHole 16ch — both silent/dead as far as anything else on
        # this Mac (Zoom, Voice Memos, actually hearing your own speakers)
        # is concerned, until you stop the script. Acceptable for a
        # dedicated always-on answering Mac; worth knowing if this Mac is
        # also used for other calls or everyday audio.
        print("  Setting system audio devices at startup — input: "
              f"{TTS_OUTPUT_DEVICE}, output: {CALLER_AUDIO_DEVICE}. Both stay "
              "fixed for as long as this process runs (see comment above).")
        if not set_system_input_device(TTS_OUTPUT_DEVICE):
            print("  System input device switch failed at startup — calls "
                  "will likely answer but the caller won't hear anything "
                  "until this is fixed. See SETUP_FACETIME_CALLS.md.",
                  file=sys.stderr)
        if not set_system_output_device(CALLER_AUDIO_DEVICE):
            print("  System output device switch failed at startup — "
                  "recording the caller's voice likely won't work until "
                  "this is fixed. See SETUP_FACETIME_CALLS.md.",
                  file=sys.stderr)

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
