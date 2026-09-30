#!/usr/bin/env bash
# Build the trusted production backend bundled into Sam.app (Phase 17).
#
#   $SAM_BACKEND_DIST/python/   (default ~/Library/Caches/app.sam.desktop.build/
#                               backend-dist: outside the repository and outside
#                               iCloud) relocatable CPython (python-build-
#                               standalone, via `uv python`) with the locked
#                               production dependencies and the `sam` wheel
#
# Dependencies are EXACTLY the versions in uv.lock, installed with
# --require-hashes (every file is checked against the lock's sha256), from the
# uv cache or PyPI. Nothing is resolved or upgraded here. No test, fake
# adapter, mutation harness or repository path is included. build-release.sh
# copies the result into Sam.app/Contents/Resources/backend.
set -euo pipefail

PYTHON_VERSION="3.12.13"
# The macOS Keychain boundary (sam.system.secrets): keyring and its locked
# dependency closure, taken hash-pinned from the lock's voice-local group.
KEYRING_CLOSURE="keyring jaraco-classes jaraco-context jaraco-functools more-itertools"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="${SAM_BACKEND_DIST:-$HOME/Library/Caches/app.sam.desktop.build/backend-dist}"
case "$OUT" in
  "$ROOT"*) echo "refusing: the backend build must live outside the repository" >&2; exit 1 ;;
esac
REQS="$(mktemp -t sam-backend-reqs)"
KEYRING_REQS="$(mktemp -t sam-backend-keyring)"
ALL_GROUP="$(mktemp -t sam-backend-group)"
trap 'rm -f "$REQS" "$KEYRING_REQS" "$ALL_GROUP"' EXIT

SOURCE_PYTHON="$(uv python find --no-project "$PYTHON_VERSION")"
PYTHON_HOME="$(cd "$(dirname "$SOURCE_PYTHON")/.." && pwd)"
case "$PYTHON_HOME" in
  */uv/python/cpython-"$PYTHON_VERSION"-*) ;;
  *) echo "refusing: not a uv-managed standalone CPython: $PYTHON_HOME" >&2; exit 1 ;;
esac

rm -rf "$OUT"
mkdir -p "$OUT"
cp -R "$PYTHON_HOME" "$OUT/python"
rm -f "$OUT/python/lib/python3.12/EXTERNALLY-MANAGED"
# Unused standard-library parts that only add size or surface.
rm -rf "$OUT/python/lib/python3.12/test" "$OUT/python/lib/python3.12/idlelib" \
       "$OUT/python/lib/python3.12/tkinter" "$OUT/python/lib/python3.12/turtledemo" \
       "$OUT/python/share" "$OUT/python/include"
BUNDLED="$OUT/python/bin/python3.12"

(cd "$ROOT" && uv export --frozen --no-dev --no-emit-project --no-header \
  --format requirements-txt -o "$REQS" >/dev/null)
(cd "$ROOT" && uv export --frozen --no-dev --no-emit-project --no-header \
  --only-group voice-local --format requirements-txt -o "$ALL_GROUP" >/dev/null)
python3 - "$ALL_GROUP" "$KEYRING_REQS" $KEYRING_CLOSURE <<'PY'
import re, sys
source, target, *wanted = sys.argv[1:]
blocks, current = [], []
for line in open(source):
    if line and not line[0].isspace() and not line.startswith("#"):
        if current:
            blocks.append(current)
        current = [line]
    elif current:
        current.append(line)
if current:
    blocks.append(current)
keep = [b for b in blocks if re.split(r"[=\s;]", b[0], maxsplit=1)[0] in wanted]
found = {re.split(r"[=\s;]", b[0], maxsplit=1)[0] for b in keep}
missing = set(wanted) - found
if missing:
    sys.exit(f"missing from uv.lock: {sorted(missing)}")
open(target, "w").write("".join("".join(b) for b in keep))
PY
# The whole locked voice-local group (hash-pinned): voice is Sam's primary
# interaction, so the packaged backend carries the local STT and speaker
# verification stack. Models are NOT bundled or downloaded here: the stack stays
# inactive until the owner sets up the verified models (sam.voice_local.setup).
uv pip install --quiet --python "$BUNDLED" --break-system-packages \
  --require-hashes -r "$REQS" -r "$KEYRING_REQS" -r "$ALL_GROUP"
# Build the `sam` wheel from a minimal clean copy (package source only: no
# tests, docs, desktop sources or anything else from the repository).
STAGE="$(mktemp -d -t sam-backend-src)"
trap 'rm -f "$REQS" "$KEYRING_REQS" "$ALL_GROUP"; rm -rf "$STAGE"' EXIT
cp "$ROOT/pyproject.toml" "$ROOT/README.md" "$STAGE/"
mkdir -p "$STAGE/src"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/src/sam" "$STAGE/src/"
uv build --quiet --wheel --out-dir "$STAGE/dist" "$STAGE" >/dev/null
uv pip install --quiet --python "$BUNDLED" --break-system-packages \
  --no-deps --reinstall "$STAGE"/dist/sam-*.whl

# The installer itself is not needed at runtime.
rm -rf "$OUT"/python/lib/python3.12/site-packages/pip "$OUT"/python/lib/python3.12/site-packages/pip-*.dist-info "$OUT"/python/bin/pip*

# Self-check in isolated mode from an unrelated directory: everything must
# import from the bundle alone.
(cd / && env -i HOME="$HOME" "$BUNDLED" -I -B -c \
  "import sam.system.backend, sam.main, keyring, uvicorn, fastapi, faster_whisper, speechbrain, torch, torchaudio; print('backend ok')")
if find "$OUT/python" -path '*site-packages/tests*' | grep -q .; then
  echo "refusing: test code in the bundle" >&2; exit 1
fi
echo "built $OUT"
