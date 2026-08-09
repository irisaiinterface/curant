#!/usr/bin/env bash
# curant-current/deploy.sh
#
# One-command deploy for the two background services (texts + calls).
#
# Real bug this exists to prevent: on 2026-08-08, /usr/local/bin/
# curant-facetime-answerer.py (a plain COPY of the repo file, not a
# symlink) silently fell hours out of date -- the launchd service kept
# running old code with no error at all, and a long stretch of live
# debugging made no sense until the two files were diffed directly and
# found to be different. "Deploying" a change used to mean: git pull,
# then separately remember to `cp` the file into /usr/local/bin, then
# bootout+bootstrap the service -- easy to forget a step, and nothing
# would ever tell you if you did.
#
# This script makes that whole bug class structurally impossible rather
# than just less likely: it SYMLINKS /usr/local/bin's copies straight
# into the repo (same pattern curant-cli on PATH already used, which is
# exactly why THAT file never went stale the same way) instead of
# copying bytes around, so a plain `git pull` alone keeps them current
# from now on. This script's real remaining job on every run is just
# restarting the services so they actually pick up whatever changed,
# and confirming both came back up cleanly.
#
# Usage: bash curant-current/deploy.sh   (run from anywhere, this
# script finds its own location to work out the repo root -- no need
# to cd into the repo first)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MAC_DIR="$SCRIPT_DIR/mac"

echo "==> Repo: $REPO_DIR"
echo "==> Pulling latest from git..."
cd "$REPO_DIR"
git pull

echo "==> Ensuring /usr/local/bin scripts are symlinked to the repo (not stale copies)..."
for f in curant-facetime-answerer.py curant-watcher.py; do
    target="/usr/local/bin/$f"
    src="$MAC_DIR/$f"
    if [ ! -f "$src" ]; then
        echo "    ERROR: expected source file missing: $src" >&2
        exit 1
    fi
    if [ -L "$target" ] && [ "$(readlink "$target")" = "$src" ]; then
        echo "    OK (already symlinked): $f"
    else
        if [ -e "$target" ] || [ -L "$target" ]; then
            echo "    Replacing existing $target with a symlink..."
        else
            echo "    Creating symlink for $f..."
        fi
        rm -f "$target"
        ln -s "$src" "$target"
        echo "    Linked: $target -> $src"
    fi
done

echo "==> Verifying deployed files actually match the repo..."
deploy_ok=true
for f in curant-facetime-answerer.py curant-watcher.py; do
    if diff -q "/usr/local/bin/$f" "$MAC_DIR/$f" > /dev/null 2>&1; then
        echo "    OK: $f matches repo"
    else
        echo "    MISMATCH: $f does NOT match the repo -- deploy did not work as expected!" >&2
        deploy_ok=false
    fi
done
if [ "$deploy_ok" != true ]; then
    echo "==> Aborting before restarting services -- fix the mismatch above first." >&2
    exit 1
fi

echo "==> Restarting both background services..."
GUI_DOMAIN="gui/$(id -u)"
launchctl bootout "$GUI_DOMAIN/app.curant.watcher" 2>/dev/null || true
launchctl bootout "$GUI_DOMAIN/app.curant.facetime" 2>/dev/null || true
sleep 1
launchctl bootstrap "$GUI_DOMAIN" "$HOME/Library/LaunchAgents/com.curant.watcher.plist"
launchctl bootstrap "$GUI_DOMAIN" "$HOME/Library/LaunchAgents/com.curant.facetime.plist"

echo "==> Waiting for startup..."
sleep 3

echo ""
echo "==> curant-watcher.log:"
tail -5 /tmp/curant-watcher.log 2>/dev/null || echo "    (no log yet)"
echo "==> curant-watcher-error.log:"
tail -5 /tmp/curant-watcher-error.log 2>/dev/null || echo "    (empty)"
echo "==> curant-facetime.log:"
tail -5 /tmp/curant-facetime.log 2>/dev/null || echo "    (no log yet)"
echo "==> curant-facetime-error.log:"
tail -5 /tmp/curant-facetime-error.log 2>/dev/null || echo "    (empty)"

echo ""
echo "==> Running services (should show both):"
if launchctl list | grep curant; then
    echo "==> Deploy complete."
else
    echo "==> WARNING: no curant services found running -- something failed to start." >&2
    exit 1
fi
