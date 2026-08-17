#!/usr/bin/env bash
#
# run-regression.sh -- one command to pull the latest fixes, run the
# persona regression suite, and drop a clearly-named, fully-written
# output file on your Desktop ready to drag into chat.
#
# Usage:
#   export GEMINI_API_KEY=AIzaSy...   (only needed once per Terminal session)
#   bash run-regression.sh
#
# Run from anywhere -- it finds the repo root itself.

set -euo pipefail

if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ]; then
    echo "No GEMINI_API_KEY or GOOGLE_API_KEY set in this Terminal session."
    echo "Get one (free) at https://aistudio.google.com/apikey, then run:"
    echo "  export GEMINI_API_KEY=<the key you got>"
    echo "...and re-run this script."
    exit 1
fi

REPO_ROOT="$HOME/curant"
if [ ! -d "$REPO_ROOT" ]; then
    echo "Couldn't find $REPO_ROOT -- edit REPO_ROOT at the top of this script"
    echo "if your checkout lives somewhere else."
    exit 1
fi

echo "==> Pulling latest fixes..."
cd "$REPO_ROOT"
git pull

OUT_FILE="$HOME/Desktop/regression_output_$(date +%Y%m%d_%H%M%S).txt"

echo "==> Running the suite (this can take a few minutes for 50 cases)..."
echo "    Writing to: $OUT_FILE"
# set +e around this specific command: the suite legitimately exits 1
# when a test case fails, which is expected/normal output to report, not
# a bug in this script -- `set -e` would otherwise kill this script
# before it ever prints where the output file landed.
set +e
python3 -u tests/run_persona_regression.py --provider gemini --model gemini-3.1-flash-lite --target home > "$OUT_FILE" 2>&1
EXIT_CODE=$?
set -e

echo ""
echo "==> Done (exit code $EXIT_CODE). File is fully written -- safe to drag in now:"
echo "    $OUT_FILE"
open "$(dirname "$OUT_FILE")" 2>/dev/null || true
