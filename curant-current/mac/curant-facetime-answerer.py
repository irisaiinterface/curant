#!/usr/bin/env python3
"""
curant-facetime-answerer — EXPERIMENTAL. Auto-answers incoming FaceTime
calls and lets the caller talk to your Curant in real time: it clicks
"Accept" via UI automation, records the caller's voice, transcribes it,
asks curant-cli for a reply (fast model tier — a live caller is
waiting), and speaks the reply back with macOS's built-in `say`.

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
  - Full setup in SETUP_FACETIME_CALLS.md (BlackHole, per-call FaceTime
    audio device selection) done FIRST.
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
    running) narrows this back down to calls specifically."""
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
CLICK_VERIFY_WAIT_SECONDS = 1.5   # how long to wait after a click before checking if it worked
MAX_ACCEPT_ATTEMPTS = 6           # hard cap so a persistently-wrong click can't loop forever


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


def _click_and_verify(x_point, y_point, attempt_label):
    """Clicks, waits, then checks whether the call actually got answered
    — not just whether cliclick exited 0. A "successful" click that
    doesn't move the actual call state is exactly the failure mode found
    in live testing (logged as accepted, banner never went away)."""
    try:
        _click_at(x_point, y_point)
    except Exception as e:
        print(f"  [{attempt_label}] click at ({x_point},{y_point}) failed to execute: {e}", file=sys.stderr)
        return False
    time.sleep(CLICK_VERIFY_WAIT_SECONDS)
    still_ringing = _call_still_ringing()
    print(f"  [{attempt_label}] clicked ({x_point},{y_point}) — "
          f"{'still ringing, click did not work' if still_ringing else 'banner gone, click worked'}",
          file=sys.stderr)
    return not still_ringing


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
    Verify-retry-and-remember: a click 'succeeding' (cliclick exiting 0,
    or even landing on a template-matched coordinate) doesn't mean the
    call actually got answered — confirmed live, where a logged
    "Accepted" click left the banner on screen the whole time. So every
    click here is followed by a real check (_call_still_ringing) before
    it's trusted.

    Order of attempts:
      1. A previously-successful coordinate, if one is cached on disk
         (fast path — skips screenshotting/matching entirely once this
         Mac's actual working spot is known).
      2. Fresh visual template matching, with a small expanding local
         search around the detected point if the exact match doesn't
         verify (covers slightly-off scale/detection math, not just
         totally-wrong guesses).
      3. The hardcoded ACCEPT_BUTTON_FALLBACK_XY_ENV coordinate, same
         local-search treatment.

    Whichever coordinate is first VERIFIED to work gets saved to
    CLICK_CACHE_PATH — the next call tries that first before anything
    else. If the cached spot ever stops working (banner style changes,
    resolution changes), this naturally falls through to fresh
    detection again and re-caches whatever works this time.
    """
    attempts = 0

    cached = _load_click_cache()
    if cached:
        attempts += 1
        if _click_and_verify(*cached, f"attempt {attempts}/cache"):
            return True, f"clicked cached working coordinate {cached}"

    screenshot_path = _capture_screenshot()
    try:
        found = _find_accept_button_visually(screenshot_path)
    finally:
        os.remove(screenshot_path)

    if found:
        for point in _local_search_offsets(found):
            if attempts >= MAX_ACCEPT_ATTEMPTS:
                break
            attempts += 1
            if _click_and_verify(*point, f"attempt {attempts}/visual"):
                _save_click_cache(point)
                return True, f"clicked visually-detected accept button (verified) at point {point}"

    fallback = os.environ.get(ACCEPT_BUTTON_FALLBACK_XY_ENV)
    if fallback:
        try:
            x_str, y_str = fallback.split(",")
            fallback_xy = (int(x_str), int(y_str))
        except Exception:
            return False, f"{ACCEPT_BUTTON_FALLBACK_XY_ENV} ({fallback}) is not valid 'x,y'"
        for point in _local_search_offsets(fallback_xy):
            if attempts >= MAX_ACCEPT_ATTEMPTS:
                break
            attempts += 1
            if _click_and_verify(*point, f"attempt {attempts}/fallback"):
                _save_click_cache(point)
                return True, f"clicked hardcoded-fallback-derived coordinate (verified) at point {point}"

    return False, (
        f"exhausted {attempts} click attempt(s) (cache, visual detection + local search, "
        f"and{' ' if fallback else ' no '}{ACCEPT_BUTTON_FALLBACK_XY_ENV} fallback) — none "
        f"verified as actually answering the call. If {ACCEPT_BUTTON_FALLBACK_XY_ENV} isn't "
        f"set yet, hover your mouse over the real button during a live call and run "
        f"`cliclick p` to get coordinates, then export {ACCEPT_BUTTON_FALLBACK_XY_ENV}=\"x,y\""
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


def hang_up():
    """Best-effort. Unlike accept_call(), this targets FaceTime.app's own
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
