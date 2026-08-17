#!/usr/bin/env bash
#
# curant-current/install.command
#
# One-time setup for a trusted beta tester on their own Mac. Double-click
# this file in Finder (or run `bash install.command` in Terminal) from
# INSIDE the folder you were given -- it only works run from here, since
# it installs the curant-cli and curant-watcher.py sitting right next to it.
#
# What this does NOT do, on purpose:
#   - Does not touch FaceTime call-answering. That needs manual macOS
#     permission grants (Accessibility, Screen Recording) and an audio
#     driver (BlackHole) that can't be silently installed -- see
#     mac/SETUP_FACETIME_CALLS.md if you want that later. Texting works
#     fully without it.
#   - Does not install com.curant.server.plist. That's the developer's own
#     local billing/license dashboard, not something a tester's Mac needs.
#   - Does not require a real license key. Curant's hosted license server
#     isn't live yet -- this beta runs with license checks bypassed
#     (CURANT_DEV_UNLICENSED=1), the same way it's been tested this whole
#     time. When real activation exists, re-run `curant-cli activate <key>`.
#
# Safe to re-run: every step checks whether it's already done before
# doing it again.

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------------

if [ "$(uname -s)" != "Darwin" ]; then
    echo "Curant Home only runs on macOS. Stopping here."
    exit 1
fi

if [ "$EUID" -eq 0 ]; then
    echo "Don't run this as root/sudo -- run it as your normal user. Stopping here."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "$SCRIPT_DIR/curant-cli" ] || [ ! -f "$SCRIPT_DIR/mac/curant-watcher.py" ]; then
    echo "This script needs to run from inside the folder it came with"
    echo "(couldn't find curant-cli and mac/curant-watcher.py next to it)."
    echo "Looked in: $SCRIPT_DIR"
    exit 1
fi

BIN_DIR="$HOME/bin"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
GUI_DOMAIN="gui/$(id -u)"

echo "======================================================"
echo " Curant Home -- beta install"
echo "======================================================"
echo ""
echo "This will take a few minutes and will ask for your Mac password"
echo "at least once (Homebrew needs it to install)."
echo ""
read -r -p "Press Enter to begin, or Ctrl+C to stop now... " _

# ---------------------------------------------------------------------------
# 1. Homebrew
# ---------------------------------------------------------------------------

echo ""
echo "==> Checking for Homebrew..."
if ! command -v brew >/dev/null 2>&1; then
    echo "    Not found -- installing Homebrew (this alone can take several minutes)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Homebrew installs to a different place on Apple Silicon vs Intel and
    # doesn't add itself to PATH for this same script run -- find it fresh.
    if [ -x /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew ]; then
        eval "$(/usr/local/bin/brew shellenv)"
    else
        echo "    Homebrew install finished but 'brew' still isn't on PATH."
        echo "    Close this window, open a NEW Terminal, and run this script again."
        exit 1
    fi
else
    echo "    Found: $(command -v brew)"
fi

BREW_PREFIX="$(brew --prefix)"

# ---------------------------------------------------------------------------
# 2. Python 3.12 (via Homebrew, not Apple's system Python -- see the long
#    comment in mac/com.curant.watcher.plist for why: macOS ties Automation
#    permissions to the exact binary, and only the Homebrew one can get them)
# ---------------------------------------------------------------------------

echo ""
echo "==> Checking for Python 3.12 (via Homebrew)..."
if ! brew list python@3.12 >/dev/null 2>&1; then
    echo "    Not found -- installing (brew install python@3.12)..."
    brew install python@3.12
else
    echo "    Already installed."
fi

PY312="$(brew --prefix python@3.12)/bin/python3.12"
if [ ! -x "$PY312" ]; then
    echo "    ERROR: expected python3.12 at $PY312 but it's not there."
    echo "    Something went wrong with the Homebrew install above."
    exit 1
fi
echo "    Using: $PY312"

# ---------------------------------------------------------------------------
# 3. Python packages Curant needs
# ---------------------------------------------------------------------------

echo ""
echo "==> Installing required Python packages..."
"$PY312" -m pip install --break-system-packages --quiet --upgrade pip
"$PY312" -m pip install --break-system-packages --quiet anthropic openai google-genai cryptography mcp requests
# google-genai specifically: real gap found live -- curant-cli setup offers
# Gemini as an equal first-class provider choice alongside Anthropic/OpenAI
# (same prompt, no caveat), but its actual live relay path imports the
# native google-genai SDK directly (from google import genai), not just
# OpenAI's compatibility endpoint. Without this package installed, picking
# Gemini during setup produces a completely silent, working-looking install
# that then fails every single message with "No module named 'google'" --
# confirmed live on a fresh install, customer gets no response at all with
# no indication why.
echo "    Done."

# ---------------------------------------------------------------------------
# 4. curant-cli + curant-watcher.py -> ~/bin
# ---------------------------------------------------------------------------

echo ""
echo "==> Installing curant-cli and curant-watcher.py to $BIN_DIR..."
mkdir -p "$BIN_DIR"
cp "$SCRIPT_DIR/curant-cli" "$BIN_DIR/curant-cli"
cp "$SCRIPT_DIR/mac/curant-watcher.py" "$BIN_DIR/curant-watcher.py"
chmod +x "$BIN_DIR/curant-cli" "$BIN_DIR/curant-watcher.py"
echo "    Installed."

# Make ~/bin available in the Terminal too (not just background jobs, which
# get PATH set explicitly further down) -- zsh is the default shell on every
# supported macOS version. Also export the beta license bypass here so
# running curant-cli by hand behaves the same as the background jobs do,
# rather than confusingly reporting "not_activated" only when run manually.
ZPROFILE="$HOME/.zprofile"
touch "$ZPROFILE"
if ! grep -q 'CURANT_BETA_INSTALL_BLOCK' "$ZPROFILE" 2>/dev/null; then
    echo "" >> "$ZPROFILE"
    echo "# --- CURANT_BETA_INSTALL_BLOCK (added by install.command) ---" >> "$ZPROFILE"
    echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$ZPROFILE"
    echo "# Beta only: Curant's hosted license server isn't live yet, so license" >> "$ZPROFILE"
    echo "# checks are bypassed. Remove this line once real activation exists." >> "$ZPROFILE"
    echo "export CURANT_DEV_UNLICENSED=1" >> "$ZPROFILE"
    echo "# --- end CURANT_BETA_INSTALL_BLOCK ---" >> "$ZPROFILE"
    echo "    Added $BIN_DIR to PATH in $ZPROFILE (new Terminal windows will pick this up)."
else
    echo "    PATH/env already set up in $ZPROFILE."
fi
export PATH="$BIN_DIR:$PATH"
export CURANT_DEV_UNLICENSED=1

# ---------------------------------------------------------------------------
# 5. One foreground run, so macOS's Automation permission prompt (for
#    controlling Messages.app) actually shows up. Confirmed elsewhere in
#    this codebase (see com.curant.watcher.plist's comments): that prompt
#    is unreliable from a background launchd job the first time, but shows
#    correctly on a real foreground run.
# ---------------------------------------------------------------------------

echo ""
echo "==> Running a quick one-time check (macOS may ask for permission here --"
echo "    if a popup appears asking to control 'Messages' or 'System Events',"
echo "    click Allow)..."
CURANT_DEV_UNLICENSED=1 "$PY312" -u "$BIN_DIR/curant-watcher.py" --daily-briefing || true
echo "    Done."

# ---------------------------------------------------------------------------
# 6. Background services (launchd). Generated fresh for THIS Mac and THIS
#    user -- the plist files checked into the repo hardcode the developer's
#    own username and are Apple-Silicon-only paths, so they are deliberately
#    NOT copied as-is here.
# ---------------------------------------------------------------------------

echo ""
echo "==> Setting up background services..."
mkdir -p "$LAUNCH_AGENTS"

write_plist() {
    # $1 = label suffix (e.g. "watcher"), $2 = extra ProgramArguments flag
    # (empty for the plain watcher), $3 = schedule XML block, $4 = extra
    # <true/> KeepAlive line (only the watcher itself needs this)
    local suffix="$1" flag="$2" schedule="$3" keepalive="$4"
    local label="app.curant.${suffix}"
    local plist="$LAUNCH_AGENTS/com.curant.${suffix}.plist"
    local flag_xml=""
    if [ -n "$flag" ]; then
        flag_xml="        <string>${flag}</string>"
    fi

    cat > "$plist" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PY312}</string>
        <string>-u</string>
        <string>${BIN_DIR}/curant-watcher.py</string>
${flag_xml}
    </array>
${schedule}
${keepalive}
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${BIN_DIR}:${BREW_PREFIX}/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>CURANT_DEV_UNLICENSED</key>
        <string>1</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/curant-${suffix}.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/curant-${suffix}-error.log</string>
</dict>
</plist>
PLIST_EOF
}

load_plist() {
    local suffix="$1"
    local label="app.curant.${suffix}"
    local plist="$LAUNCH_AGENTS/com.curant.${suffix}.plist"
    launchctl bootout "$GUI_DOMAIN/$label" >/dev/null 2>&1 || true
    launchctl bootstrap "$GUI_DOMAIN" "$plist"
}

# Texting -- runs continuously, watches for incoming messages.
write_plist "watcher" "" \
"    <key>RunAtLoad</key>
    <true/>" \
"    <key>KeepAlive</key>
    <true/>"

# Daily briefing -- 9am and 5pm.
write_plist "dailybriefing" "--daily-briefing" \
"    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>0</integer></dict>
    </array>
    <key>RunAtLoad</key>
    <false/>" \
""

# Proactive check-ins -- 8am daily. Opt-in server-side, harmless to load.
write_plist "proactive" "--proactive-check" \
"    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <key>RunAtLoad</key>
    <false/>" \
""

# Weekly rollup -- Sunday 6pm. Grace-only, harmless no-op otherwise.
write_plist "weeklyrollup" "--weekly-rollup" \
"    <key>StartCalendarInterval</key>
    <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
    <key>RunAtLoad</key>
    <false/>" \
""

# Meeting prep -- every 15 min. Grace-only, harmless no-op otherwise.
write_plist "meetingprep" "--meeting-prep-check" \
"    <key>StartInterval</key>
    <integer>900</integer>
    <key>RunAtLoad</key>
    <false/>" \
""

# IRIS inbox check -- hourly. Read+draft only, never sends; harmless no-op
# until you connect a Gmail account with `curant-cli connect-email`.
write_plist "irisinbox" "--iris-inbox-check" \
"    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <false/>" \
""

for svc in watcher dailybriefing proactive weeklyrollup meetingprep irisinbox; do
    load_plist "$svc"
done

# Automated backup runs a plain `curant-cli` command, not curant-watcher.py --
# separate template, Grace-only + opt-in, harmless to load either way.
AUTOBACKUP_PLIST="$LAUNCH_AGENTS/com.curant.autobackup.plist"
cat > "$AUTOBACKUP_PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>app.curant.autobackup</string>
    <key>ProgramArguments</key>
    <array>
        <string>${BIN_DIR}/curant-cli</string>
        <string>run-automated-backup</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
    <key>RunAtLoad</key>
    <false/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${BIN_DIR}:${BREW_PREFIX}/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>CURANT_DEV_UNLICENSED</key>
        <string>1</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/curant-autobackup.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/curant-autobackup-error.log</string>
</dict>
</plist>
PLIST_EOF
load_plist "autobackup"

echo "    Waiting for services to start..."
sleep 2

echo ""
echo "==> Services now running:"
if launchctl list | grep curant; then
    :
else
    echo "    WARNING: no curant services found running -- something failed above." >&2
fi

# ---------------------------------------------------------------------------
# 7. Hand off to the existing interactive setup wizard (consent, provider,
#    API key, who Curant should listen to).
# ---------------------------------------------------------------------------

echo ""
echo "======================================================"
echo " Background services are set up. Last step: your"
echo " personal settings (this part asks you questions)."
echo "======================================================"
echo ""
"$BIN_DIR/curant-cli" setup

echo ""
echo "======================================================"
echo " Done. Try texting the phone number/Apple ID you just"
echo " set, and check status any time with: curant-cli status"
echo ""
echo " Optional next steps, not required to start texting:"
echo "   - Connect Gmail:   curant-cli connect-email"
echo "   - FaceTime calls:  see mac/SETUP_FACETIME_CALLS.md"
echo "======================================================"
