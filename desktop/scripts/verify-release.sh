#!/usr/bin/env bash
# Isolated production-artifact check (Phase 17).
#
# Copies the built Sam.app to a fresh temporary directory and runs its
# headless self-test (start the BUNDLED backend, authenticate over the bridge,
# read status and grants, stop it) with:
#   * an empty environment except HOME and TMPDIR (no PYTHONPATH, no venv);
#   * cwd = the temporary directory (unrelated to the repository);
#   * a macOS sandbox that DENIES reading the repository, the project .venv,
#     the verification venv and the uv Python the bundle was copied from, and
#     denies all network except loopback;
#   * a temporary data directory (the owner's real data is never touched).
# Usage: verify-release.sh [path/to/Sam.app]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
APP="${1:-$ROOT/desktop/src-tauri/target/release/bundle/macos/Sam.app}"
WORK="$(mktemp -d /private/tmp/sam-release-check.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
cp -R "$APP" "$WORK/Sam.app"
mkdir -m 700 "$WORK/data"
UV_PYTHONS="$HOME/.local/share/uv/python"
PROFILE="(version 1)(allow default)
(deny file-read* (subpath \"$ROOT\"))
(deny file-read* (subpath \"/private/tmp/claude-501/sam-verify-venv\"))
(deny file-read* (subpath \"$UV_PYTHONS\"))
(deny file-read* (subpath \"/Library/Frameworks/Python.framework\"))
(deny network-outbound (remote ip \"*:*\"))
(allow network-outbound (remote ip \"localhost:*\"))
(allow network-inbound (local ip \"localhost:*\"))"
cd "$WORK"
# Negative controls: the sandbox really hides the repository and every
# development Python (otherwise this check would prove nothing).
for probe in "$ROOT/pyproject.toml" /private/tmp/claude-501/sam-verify-venv/pyvenv.cfg \
             "$UV_PYTHONS"; do
  if [ -e "$probe" ] && sandbox-exec -p "$PROFILE" /bin/ls "$probe" >/dev/null 2>&1; then
    echo "{\"self_test\":\"failed\",\"reason\":\"sandbox_not_isolating\"}"; exit 1
  fi
done
env -i HOME="$HOME" TMPDIR="$WORK" \
  sandbox-exec -p "$PROFILE" \
  "$WORK/Sam.app/Contents/MacOS/sam-desktop" --self-test "$WORK/data"
