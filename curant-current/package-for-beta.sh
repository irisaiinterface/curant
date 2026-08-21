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
# Output: curant-current-beta-<VERSION>.zip in ~/Downloads, where VERSION is
# computed from the HEAD commit being packaged (see below).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Version is computed from the git commit being packaged, not typed by
# hand: the committer timestamp of HEAD, down to the second, in the same
# "YYYY-MM-DD.<digits>" format CURANT_VERSION has always used (see the
# comment above CURANT_VERSION in curant-cli -- _version_is_newer() is a
# plain string comparison, so this format is load-bearing, not cosmetic).
# Result: every commit that gets packaged gets its own distinct,
# correctly-ordered version automatically -- no more forgetting to bump
# CURANT_VERSION by hand and having the auto-updater silently think
# nothing changed.
if ! git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "ERROR: $SCRIPT_DIR isn't inside a git checkout -- can't compute a version." >&2
    exit 1
fi
if [ -n "$(git -C "$SCRIPT_DIR" status --porcelain -- curant-cli install.command mac/ 2>/dev/null)" ]; then
    echo "WARNING: curant-cli/install.command/mac/ have uncommitted changes." >&2
    echo "         The version below is derived from HEAD's commit, so it won't" >&2
    echo "         reflect those uncommitted edits. Commit first if this build" >&2
    echo "         should include them." >&2
fi
VERSION="$(git -C "$SCRIPT_DIR" log -1 --date=format:'%Y-%m-%d.%H%M%S' --format=%cd)"
COMMIT_SUBJECT="$(git -C "$SCRIPT_DIR" log -1 --format=%s)"
COMMIT_HASH="$(git -C "$SCRIPT_DIR" log -1 --format=%h)"
# Customer-facing v1.0-style version -- see CURANT_DISPLAY_VERSION's own
# comment in curant-cli. Read straight from source, not auto-generated;
# only changes when a human bumps it for a real milestone.
DISPLAY_VERSION="$(grep -m1 '^CURANT_DISPLAY_VERSION = ' "$SCRIPT_DIR/curant-cli" | sed -E 's/^CURANT_DISPLAY_VERSION = "(.*)"$/\1/')"

STAGE_DIR="$(mktemp -d)/curant-current"
# Lands in ~/Downloads rather than next to this script -- matches where a
# real customer's copy would actually sit (AirDrop/browser downloads both
# default there), and keeps it out of the git working tree so it never
# shows up in `git status` or gets accidentally committed.
#
# Filename uses VERSION (not just DATE_TAG) now that a build can legitimately
# happen more than once a day -- keeps same-day builds from silently
# overwriting each other in Downloads.
OUT_ZIP="$HOME/Downloads/curant-current-beta-${VERSION}.zip"

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
    --exclude 'mac/*.plist' \
    --exclude 'deploy.sh' \
    --exclude 'build_protected.sh' \
    --exclude 'curant.rb' \
    --exclude 'BETA_SMOKE_TEST.md' \
    "$SCRIPT_DIR/" "$STAGE_DIR/"

echo "==> Stamping version $VERSION into the staged copy..."
# Overwrite the hardcoded dev-mode literal with the computed version --
# see the comment above CURANT_VERSION in curant-cli for why this is safe
# (format is fixed, this script is the one place that's allowed to touch it).
python3 - "$STAGE_DIR/curant-cli" "$VERSION" <<'PYEOF2'
import re, sys
path, version = sys.argv[1], sys.argv[2]
with open(path) as f:
    content = f.read()
new_content, n = re.subn(
    r'CURANT_VERSION = "[^"]*"',
    f'CURANT_VERSION = "{version}"',
    content,
    count=1,
)
if n != 1:
    print("ERROR: could not find CURANT_VERSION line to stamp", file=sys.stderr)
    sys.exit(1)
with open(path, "w") as f:
    f.write(new_content)
PYEOF2

# mac/*.plist: every checked-in plist hardcodes the DEVELOPER's own home
# directory (/Users/AbhyudayDhundhel/bin) in its PATH. None of them are
# used by an install: install.command generates the watcher and all
# scheduled-service plists fresh from ${BIN_DIR}/${BREW_PREFIX}, and
# setup-facetime.command generates the FaceTime one the same way (both
# verified by inspecting the shipped package). So they are dead weight
# that (a) leaks the developer's username and home layout to every
# tester and (b) is actively dangerous if a tester ever copies one by
# hand, since it would point launchd at a path that doesn't exist on
# their Mac and fail silently -- the exact bug class that cost a full
# debugging session before setup-facetime.command started generating
# its plist per-user.
#
# deploy.sh / build_protected.sh: developer-only scripts. deploy.sh
# symlinks /usr/local/bin into THIS repo checkout, which is meaningless
# (and misleading) on a tester's machine.
#
# curant.rb: a Homebrew formula pointing at a public tap that does not
# exist. Beta distribution is a private zip; shipping a formula implies
# an install route that isn't real.
#
# BETA_SMOKE_TEST.md: internal release checklist. Testers don't run it,
# and it documents which failure modes we already know about -- not a
# useful first impression.
#
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
mkdir -p "$(dirname "$OUT_ZIP")"
rm -f "$OUT_ZIP"
(cd "$(dirname "$STAGE_DIR")" && zip -rq "$OUT_ZIP" "curant-current")
rm -rf "$(dirname "$STAGE_DIR")"

echo ""
echo "==> Done: $OUT_ZIP"
echo "    ($(du -h "$OUT_ZIP" | cut -f1))"
echo ""
echo "Built from commit $COMMIT_HASH: $COMMIT_SUBJECT"
echo ""
echo "It's in your Downloads folder now -- send it to a tester as-is, or"
echo "AirDrop/upload it directly from there. They should:"
echo "  1. Unzip it"
echo "  2. Open the curant-current folder"
echo "  3. Double-click install.command"
echo ""
echo "==> Next: upload this zip to the update Gist, then update manifest.json"
echo "    there to match. Paste this in (fill in download_url after uploading"
echo "    the zip -- right-click its 'View raw' link in the Gist -> Copy Link):"
echo ""
cat <<MANIFEST
{
  "version": "$VERSION",
  "display_version": "$DISPLAY_VERSION",
  "download_url": "PASTE_RAW_GIST_URL_HERE",
  "changelog": "$COMMIT_SUBJECT"
}
MANIFEST
echo ""
