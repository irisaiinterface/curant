#!/usr/bin/env bash
#
# curant-current/setup-facetime.command
#
# One-time setup for FaceTime auto-answer calling (EXPERIMENTAL --
# see mac/SETUP_FACETIME_CALLS.md for the full technical background).
# Run from INSIDE the folder you were given, AFTER install.command has
# already been run once (this needs curant-cli and your config already
# in place).
#
# What this automates: Homebrew/pip dependencies, generating a
# per-user launchd plist (the checked-in one in mac/ is hardcoded to
# the developer's own Mac and will NOT work copied as-is onto yours),
# and installing the answerer script.
#
# What this CANNOT automate, because macOS deliberately doesn't allow
# it to be scripted:
#   - Granting Accessibility and Screen Recording permission to
#     Terminal. You have to click these yourself in System Settings --
#     this script opens the right panes for you and waits.
#   - Creating the "Curant Call Output" Multi-Output audio device.
#     There's no command-line way to create one; it's a few clicks in
#     Audio MIDI Setup.app. Without it, callers get total silence --
#     this script checks whether it already exists and walks you
#     through creating it if not.
#   - Confirming call detection actually works. That needs a real
#     incoming FaceTime call while you watch the terminal. This script
#     runs the safe dry-run test for you, but a human has to place the
#     test call and read the output.
#
# Safe to re-run: every step checks whether it's already done before
# doing it again. If you get interrupted (e.g. told to restart your
# Mac partway through), just run this again afterward.

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------------

if [ "$(uname -s)" != "Darwin" ]; then
    echo "FaceTime answering only runs on macOS. Stopping here."
    exit 1
fi

if [ "$EUID" -eq 0 ]; then
    echo "Don't run this as root/sudo -- run it as your normal user. Stopping here."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "$SCRIPT_DIR/mac/curant-facetime-answerer.py" ] || [ ! -f "$SCRIPT_DIR/mac/assets/facetime_accept_button.png" ]; then
    echo "This script needs to run from inside the folder it came with"
    echo "(couldn't find mac/curant-facetime-answerer.py and mac/assets/facetime_accept_button.png next to it)."
    echo "Looked in: $SCRIPT_DIR"
    exit 1
fi

BIN_DIR="$HOME/bin"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
GUI_DOMAIN="gui/$(id -u)"

if [ ! -x "$BIN_DIR/curant-cli" ] || [ ! -f "$HOME/.curant/config.json" ]; then
    echo "Curant Home isn't set up on this Mac yet -- run install.command first"
    echo "(this needs your provider/API key and phone number already configured)."
    exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew isn't on PATH -- run install.command first, or open a new"
    echo "Terminal window if you just installed it, then try again."
    exit 1
fi
BREW_PREFIX="$(brew --prefix)"
PY312="$(brew --prefix python@3.12)/bin/python3.12"
if [ ! -x "$PY312" ]; then
    echo "Python 3.12 isn't installed via Homebrew -- run install.command first."
    exit 1
fi

echo "======================================================"
echo " Curant -- FaceTime auto-answer setup (EXPERIMENTAL)"
echo "======================================================"
echo ""
echo "Read this before continuing -- this feature is genuinely different"
echo "from texting, which just works. FaceTime auto-answer:"
echo ""
echo "  - Accepts calls by taking a screenshot and clicking the exact"
echo "    pixel where the green Accept button is, because macOS doesn't"
echo "    expose that button to Accessibility scripting at all. This is"
echo "    more fragile than everything else Curant does -- it can be"
echo "    thrown off by the call banner appearing somewhere unusual, a"
echo "    second notification stacking above it, or a macOS update."
echo "  - Needs a real, live test call to confirm it works on YOUR Mac."
echo "    This isn't a five-minute setup, and it may need iteration."
echo "  - Uses your Mac's system microphone/speaker exclusively while"
echo "    running -- you won't hear anything through your normal"
echo "    speakers, and no other app (Zoom, Voice Memos) can use your"
echo "    real mic, for as long as the background service is running."
echo "  - Costs real money per call minute (transcription) plus your"
echo "    normal per-message cost for generating replies."
echo ""
read -r -p "Type 'I understand' to continue: " CONFIRM
if [ "$CONFIRM" != "I understand" ]; then
    echo "Stopping here -- nothing was changed."
    exit 0
fi

# ---------------------------------------------------------------------------
# 1. Homebrew dependencies
# ---------------------------------------------------------------------------

echo ""
echo "==> Installing Homebrew dependencies..."
FACETIME_BREW_PKGS="blackhole-2ch blackhole-16ch switchaudio-osx ffmpeg cliclick sox tesseract"
for pkg in $FACETIME_BREW_PKGS; do
    if brew list "$pkg" >/dev/null 2>&1; then
        echo "    $pkg: already installed."
    else
        echo "    $pkg: installing..."
        brew install "$pkg"
    fi
done
echo "    Done."

# ---------------------------------------------------------------------------
# 2. Python packages
# ---------------------------------------------------------------------------

echo ""
echo "==> Installing required Python packages..."
"$PY312" -m pip install --break-system-packages --quiet pillow numpy google-genai requests pytesseract
echo "    Done."

# ---------------------------------------------------------------------------
# 3. Confirm BlackHole audio devices actually showed up. They sometimes
#    don't appear in CoreAudio until a restart, and continuing past this
#    point without them means every call will connect but be silent.
# ---------------------------------------------------------------------------

echo ""
echo "==> Checking BlackHole audio devices..."
BLACKHOLE_CHECK="$(system_profiler SPAudioDataType 2>/dev/null | grep -c "BlackHole" || true)"
if [ "$BLACKHOLE_CHECK" -lt 2 ]; then
    echo ""
    echo "======================================================"
    echo " BlackHole isn't showing up in your audio devices yet."
    echo " This is normal right after installing it -- CoreAudio"
    echo " needs a restart (or at least a log out/in) to pick it up."
    echo ""
    echo " Restart your Mac now, then run this script again --"
    echo " everything above will be skipped since it's already done."
    echo "======================================================"
    exit 0
fi
echo "    Found BlackHole 2ch and BlackHole 16ch."

# ---------------------------------------------------------------------------
# 4. Multi-Output device for hearing the caller through your real
#    speakers too, not just silently through BlackHole. No CLI can
#    create this -- it's a few clicks in Audio MIDI Setup.app. Without
#    it, calls connect but the caller-side audio path works fine while
#    YOU (the person watching this Mac) hear literal silence -- not a
#    bug, just how a Multi-Output Device works.
# ---------------------------------------------------------------------------

echo ""
echo "==> Checking for the \"Curant Call Output\" Multi-Output device..."
if system_profiler SPAudioDataType 2>/dev/null | grep -q "Curant Call Output"; then
    echo "    Found it -- already set up."
else
    echo ""
    echo "======================================================"
    echo " One manual step: create a Multi-Output audio device so"
    echo " you can actually hear calls through your speakers (not"
    echo " required for Curant to work, but you'll hear nothing"
    echo " otherwise while testing)."
    echo ""
    echo " 1. Opening Audio MIDI Setup..."
    open -a "Audio MIDI Setup"
    echo " 2. Click the '+' in the bottom-left corner -> 'Create Multi-Output Device'"
    echo " 3. In the list on the right, check BOTH:"
    echo "      - BlackHole 16ch"
    echo "      - your Mac's normal speakers/output"
    echo " 4. Rename it (right-click -> Rename) to exactly: Curant Call Output"
    echo "======================================================"
    echo ""
    read -r -p "Press Enter once you've created and named it (or press Enter to skip -- calls will still connect, you just won't hear them)... " _
fi

# ---------------------------------------------------------------------------
# 5. macOS permissions -- cannot be granted from a script. Open the
#    right panes directly and wait for confirmation.
# ---------------------------------------------------------------------------

echo ""
echo "==> Opening System Settings for the two permissions this needs..."
echo ""
echo "In each pane that opens: click '+', add Terminal (or iTerm, whichever"
echo "you're running this from), and make sure its toggle is ON."
echo ""
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
read -r -p "Press Enter once Terminal is added and enabled under Accessibility... " _
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
read -r -p "Press Enter once Terminal is added and enabled under Screen Recording... " _
echo ""
echo "If you just added these for the first time, fully quit Terminal (Cmd+Q)"
echo "and reopen it now, then re-run this script -- the grant doesn't always"
echo "take effect in the same window it was given in."
read -r -p "Press Enter to continue anyway, or Ctrl+C to quit and reopen Terminal first... " _

# ---------------------------------------------------------------------------
# 6. Install the answerer script + assets
# ---------------------------------------------------------------------------

echo ""
echo "==> Installing curant-facetime-answerer.py to $BIN_DIR..."
mkdir -p "$BIN_DIR/assets"
cp "$SCRIPT_DIR/mac/curant-facetime-answerer.py" "$BIN_DIR/curant-facetime-answerer.py"
cp "$SCRIPT_DIR/mac/assets/facetime_accept_button.png" "$BIN_DIR/assets/facetime_accept_button.png"
chmod +x "$BIN_DIR/curant-facetime-answerer.py"
echo "    Installed."

# ---------------------------------------------------------------------------
# 6b. Build the ScreenCaptureKit audio tap.
#
#     This is what actually lets Curant HEAR the caller. The original
#     design fed FaceTime's audio through a Multi-Output Device into
#     BlackHole and recorded that virtual device -- which was disproven
#     live: during a connected call, a test tone played into the system
#     default output came back at RMS 5097 (capture path provably fine)
#     while FaceTime's own audio measured exactly 0.0. FaceTime is a
#     VoIP client and does not render call audio into the system default
#     output device at all.
#
#     ScreenCaptureKit taps FaceTime's audio at the application level,
#     wherever it goes, so device routing stops mattering. It reuses the
#     Screen Recording permission this feature already needs for call
#     detection, so there is no additional prompt. If the build fails,
#     the answerer still runs and falls back to the old BlackHole path
#     automatically -- degraded, not broken.
# ---------------------------------------------------------------------------

echo ""
echo "==> Building the ScreenCaptureKit audio tap..."
if ! command -v swiftc >/dev/null 2>&1; then
    echo "    swiftc not found. Install Apple's command line tools with:"
    echo "        xcode-select --install"
    echo "    Then re-run this script. (Skipping for now -- Curant will fall back to the"
    echo "    older BlackHole capture path, which is known to be unreliable with FaceTime.)"
else
    MACOS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
    if [ "${MACOS_MAJOR:-0}" -lt 13 ]; then
        echo "    macOS ${MACOS_MAJOR} detected -- ScreenCaptureKit audio capture needs macOS 13+."
        echo "    Skipping; the BlackHole fallback will be used."
    elif swiftc -O -framework ScreenCaptureKit -framework AVFoundation -framework CoreMedia \
            -o "$BIN_DIR/curant-facetime-audiotap" \
            "$SCRIPT_DIR/mac/curant-facetime-audiotap.swift" 2>/tmp/curant-audiotap-build.log; then
        chmod +x "$BIN_DIR/curant-facetime-audiotap"
        echo "    Built $BIN_DIR/curant-facetime-audiotap"
        echo "    NOTE: this tap is DISABLED by default. Measured on macOS 26: ScreenCaptureKit"
        echo "    is not given FaceTime call audio (app-scoped: peak 0; whole-system: noise"
        echo "    floor only -- while the same binary captures music at full amplitude)."
        echo "    Built anyway because it is useful for diagnostics and may work on future"
        echo "    macOS versions. Opt in with CURANT_FACETIME_ENABLE_AUDIOTAP=1."
    else
        echo "    Build FAILED -- see /tmp/curant-audiotap-build.log"
        echo "    Curant will still run and fall back to the BlackHole capture path."
    fi
fi

# ---------------------------------------------------------------------------
# 7. Generate the launchd plist fresh for THIS Mac and THIS user. The
#    one checked into the repo hardcodes the developer's own username
#    and an Apple-Silicon-only path -- copying it as-is would silently
#    fail to run on any other Mac.
# ---------------------------------------------------------------------------

echo ""
echo "==> Setting up the background FaceTime-answering service..."
mkdir -p "$LAUNCH_AGENTS"
FACETIME_PLIST="$LAUNCH_AGENTS/com.curant.facetime.plist"
MULTIOUTPUT_NAME="BlackHole 16ch"
if system_profiler SPAudioDataType 2>/dev/null | grep -q "Curant Call Output"; then
    MULTIOUTPUT_NAME="Curant Call Output"
fi

cat > "$FACETIME_PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>app.curant.facetime</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PY312}</string>
        <string>-u</string>
        <string>${BIN_DIR}/curant-facetime-answerer.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${BIN_DIR}:${BREW_PREFIX}/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>CURANT_CALL_ACCESS_MODE</key>
        <string>approved</string>
        <key>CURANT_FACETIME_SYSTEM_OUTPUT_DEVICE</key>
        <string>${MULTIOUTPUT_NAME}</string>
        <key>CURANT_DEV_UNLICENSED</key>
        <string>1</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/curant-facetime.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/curant-facetime-error.log</string>
</dict>
</plist>
PLIST_EOF
echo "    Wrote $FACETIME_PLIST"
echo "    Call access mode: approved (verifies the caller via OCR against your"
echo "    configured handles -- refuses calls it can't verify, rather than"
echo "    answering everyone)."

# ---------------------------------------------------------------------------
# 8. Dry-run call detection -- the one step that genuinely needs a real,
#    live call and a human watching the output. Everything above this
#    point is done; this is the last manual step before going live.
# ---------------------------------------------------------------------------

echo ""
echo "======================================================"
echo " Almost done. Last step before going live: confirm this"
echo " Mac can actually detect a ringing call."
echo ""
echo " In a moment this will start watching for calls in dry-run"
echo " mode (it will NOT answer or speak -- completely safe)."
echo " Once it's running, place a real FaceTime call to this Mac"
echo " from another device or Apple ID."
echo ""
echo " You should see: \"Incoming call detected: ...\""
echo ""
echo " Press Ctrl+C to stop the test once you've confirmed that."
echo "======================================================"
echo ""
echo " AFTER you confirm detection works, load the background service with:"
echo ""
echo "   launchctl bootout $GUI_DOMAIN/app.curant.facetime 2>/dev/null"
echo "   launchctl bootstrap $GUI_DOMAIN \"$FACETIME_PLIST\""
echo "   launchctl list | grep curant   # expect both app.curant.watcher and app.curant.facetime"
echo ""
echo " Logs: /tmp/curant-facetime.log and /tmp/curant-facetime-error.log"
echo ""
echo " If detection does NOT print anything while a call is visibly ringing,"
echo " see the troubleshooting section (\"Expect to iterate\") near the end of"
echo " mac/SETUP_FACETIME_CALLS.md -- most common cause is Screen Recording"
echo " permission not actually granted yet (screenshots come back black)."
echo "======================================================"
echo ""
read -r -p "Press Enter to start the dry-run test now... " _
echo ""
trap 'echo ""; echo "Test stopped."; exit 0' INT
"$PY312" -u "$BIN_DIR/curant-facetime-answerer.py" --dry-run
