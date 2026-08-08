#!/usr/bin/env bash
# Builds a bytecode-only, docstring-stripped distributable of curant-cli and
# curant-watcher.py into curant-current/dist/ — the source .py files never
# ship from here; only the compiled .pyc payload plus a two-line loader
# stub does.
#
# HONEST SCOPE, read before relying on this:
#   - Comments are always stripped by compilation (Python bytecode never
#     retains them) — that alone removes a lot of the "why" reasoning this
#     codebase's own comments carry.
#   - Compiled with -OO, so docstrings and assert statements are ALSO
#     stripped. Confirmed by testing: `strings` on a normal .pyc still
#     finds full module docstrings verbatim; an -OO .pyc does not.
#   - What this does NOT hide: ordinary string literals (customer-facing
#     messages, tool descriptions, persona prompt text) remain in the
#     compiled file's constant pool and ARE extractable with `strings` or
#     `dis`. A motivated reader can also decompile the bytecode back to
#     close-to-original Python logic with tools like decompyle3. This is
#     real, cheap, casual-reading protection — not cryptographic security,
#     and it should not be described as either to a customer or in
#     marketing. For genuine protection (a real compiled binary, not
#     inspectable bytecode), the honest next step is Nuitka or PyInstaller,
#     not this script — noted here as an open gap, not solved by this.
#   - .pyc files are tied to the exact Python version that compiled them
#     (the magic number baked into the file). Build with whatever Python
#     version the target Mac(s) will actually run — a build done under 3.9
#     will NOT run under 3.12 and vice versa. This script uses whatever
#     `python3` resolves to in the environment it's run in.
set -euo pipefail
cd "$(dirname "$0")"

DIST_DIR="dist"
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Building with python3 (detected version: $PYVER) — .pyc output will only run under this version."

build_one() {
  local src="$1" name="$2"
  python3 -OO -c "
import py_compile
py_compile.compile('${src}', cfile='${DIST_DIR}/${name}.pyc', doraise=True, optimize=2)
"
  cat > "${DIST_DIR}/${name}" << WRAPPER
#!/bin/sh
# Loader stub — the real logic lives in ${name}.pyc, not here.
exec python3 "\$(dirname "\$0")/${name}.pyc" "\$@"
WRAPPER
  chmod +x "${DIST_DIR}/${name}"
  echo "  built ${DIST_DIR}/${name} (from ${src})"
}

build_one "curant-cli" "curant-cli"
build_one "mac/curant-watcher.py" "curant-watcher"

echo
echo "Done. ${DIST_DIR}/ contains only compiled bytecode + loader stubs — no .py source."
echo "Run from there exactly like the originals, e.g.: ${DIST_DIR}/curant-cli status"
