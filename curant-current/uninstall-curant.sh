#!/usr/bin/env bash
#
# curant-current/uninstall-curant.sh
#
# Removes every trace of Curant from this Mac -- background services,
# ~/bin files, ~/.curant (config, memory, database, backups -- all of
# it), connected Gmail credentials, and log files. Does NOT touch
# anything outside those specific, listed paths.
#
# Existing ~/curant git checkout is left alone on purpose -- this script
# only undoes what install.command did, not the repo itself.
#
# Usage: bash uninstall-curant.sh

set -euo pipefail

echo "This will permanently remove:"
echo "  - All Curant background services (app.curant.*)"
echo "  - ~/bin/curant-cli and ~/bin/curant-watcher.py"
echo "  - ~/.curant/  (license state, persona, memory, database, backups -- everything)"
echo "  - ~/.gmail-mcp*  (connected Gmail credentials, personal and IRIS)"
echo "  - /tmp/curant-*.log files"
echo ""
echo "It will NOT touch anything else on this Mac, including the ~/curant git checkout."
echo ""
read -r -p "Type 'yes' to continue: " confirm
if [ "$confirm" != "yes" ]; then
    echo "Cancelled -- nothing was removed."
    exit 0
fi

echo ""
echo "==> Stopping and removing background services..."
for label in $(launchctl list 2>/dev/null | grep app.curant | awk '{print $3}'); do
    echo "    Stopping $label..."
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
done
rm -f "$HOME/Library/LaunchAgents"/com.curant.*.plist
echo "    Done."

echo "==> Removing ~/bin/curant-cli and ~/bin/curant-watcher.py..."
rm -f "$HOME/bin/curant-cli" "$HOME/bin/curant-watcher.py"
echo "    Done."

echo "==> Removing ~/.curant (config, memory, database, backups)..."
rm -rf "$HOME/.curant"
echo "    Done."

echo "==> Removing connected Gmail credentials (~/.gmail-mcp*)..."
rm -rf "$HOME/.gmail-mcp" "$HOME"/.gmail-mcp-*
echo "    Done."

echo "==> Removing log files..."
rm -f /tmp/curant-*.log /tmp/curant-*-error.log
echo "    Done."

echo ""
echo "==> Checking for leftover PATH/env additions in ~/.zprofile..."
if [ -f "$HOME/.zprofile" ] && grep -q "CURANT_BETA_INSTALL_BLOCK" "$HOME/.zprofile" 2>/dev/null; then
    echo "    Found the block install.command added. Removing it automatically"
    echo "    (a backup of the original file is saved as ~/.zprofile.pre-uninstall)..."
    cp "$HOME/.zprofile" "$HOME/.zprofile.pre-uninstall"
    # Delete everything from the start marker to the end marker, inclusive.
    sed -i '' '/# --- CURANT_BETA_INSTALL_BLOCK/,/# --- end CURANT_BETA_INSTALL_BLOCK ---/d' "$HOME/.zprofile"
    echo "    Done. Original saved at ~/.zprofile.pre-uninstall in case anything looks wrong."
else
    echo "    Nothing found -- skipping."
fi

echo ""
echo "==> Uninstall complete."
echo ""
echo "IMPORTANT: open a NEW Terminal window (or tab) before testing a fresh"
echo "install -- this current window still has the old PATH and any"
echo "exported env vars (like GEMINI_API_KEY) cached in its shell session."
echo ""
echo "Verify it's really gone in that new window with:"
echo "  launchctl list | grep curant   (should print nothing)"
echo "  which curant-cli               (should say 'not found')"
