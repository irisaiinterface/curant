#!/usr/bin/env bash
#
# curant-current/package-for-beta.sh
#
# Zips up exactly what a trusted beta tester needs (curant-cli,
# install.command, the background service scripts, docs) into a single
# file you can AirDrop/upload/email -- and nothing that shouldn't leave
# this machine.
#
# Run from anywhere: bash curant-current/package-for-beta.sh
# Output: curant-current-beta-YYYY-MM-DD.zip, written next to this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DATE_TAG="$(date +%Y-%m-%d)"
STAGE_DIR="$(mktemp -d)/curant-current"
# Lands in ~/Downloads rather than next to this script -- matches where a
# real customer's copy would actually sit (AirDrop/browser downloads both
# default there), and keeps it out of the git working tree so it never
# shows up in `git status` or gets accidentally committed.
OUT_ZIP="$HOME/Downloads/curant-current-beta-${DATE_TAG}.zip"

echo "==> Staging a clean copy in a temp directory..."
mkdir -p "$STAGE_DIR"

# rsync with explicit excludes, rather than a blind cp, so a new kind of
# junk file added later doesn't silently ship by accident -- anything not
# explicitly excluded here DOES get included, which is the safer default
# for a source-available beta (a tester who can't see the code can't
# meaningfully debug it with you).
rsync -a \
    --exclude '__pycache__/' \
    --exclude '.DS_Store' \
    --exclude 'dist/' \
    --exclude 'package-for-beta.sh' \
    --exclude '.curant/' \
    --exclude '*.pyc' \
    --exclude 'server/' \
    --exclude 'mac/com.curant.server.plist' \
    "$SCRIPT_DIR/" "$STAGE_DIR/"

# server/ and com.curant.server.plist are deliberately left out: that's
# your own local billing/license dashboard (not something a tester's Mac
# runs -- install.command never touches it), and com.curant.server.plist
# specifically has a real Flask secret key baked into the tracked file
# (see its own comment for why -- GitHub's push protection already caught
# one attempt to also bake in real Gmail OAuth credentials there). No
# reason to hand every tester a copy of that.

echo "==> Verifying nothing sensitive slipped through..."
# Cheap but real check: fail loudly rather than silently ship a key.
if grep -rlE '(sk-ant-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{20,}|github_pat_[A-Za-z0-9_]{20,})' "$STAGE_DIR" 2>/dev/null; then
    echo "    ERROR: the above file(s) matched a live-looking API key/token pattern." >&2
    echo "    Not building the zip. Check them by hand before re-running." >&2
    rm -rf "$(dirname "$STAGE_DIR")"
    exit 1
fi
echo "    Clean."

echo "==> Building $OUT_ZIP..."
rm -f "$OUT_ZIP"
(cd "$(dirname "$STAGE_DIR")" && zip -rq "$OUT_ZIP" "curant-current")
rm -rf "$(dirname "$STAGE_DIR")"

echo ""
echo "==> Done: $OUT_ZIP"
echo "    ($(du -h "$OUT_ZIP" | cut -f1))"
echo ""
echo "It's in your Downloads folder now -- send it to a tester as-is, or"
echo "AirDrop/upload it directly from there. They should:"
echo "  1. Unzip it"
echo "  2. Open the curant-current folder"
echo "  3. Double-click install.command"
