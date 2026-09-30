#!/usr/bin/env bash
# Production desktop artifact (Phase 17): the trusted backend + the Tauri app.
#   1. build-backend.sh -> $SAM_BACKEND_DIST/python (outside the repository)
#   2. tauri build (app bundle, not signed with any owner identity) with that
#      directory bundled as Contents/Resources/backend/python
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
export SAM_BACKEND_DIST="${SAM_BACKEND_DIST:-$HOME/Library/Caches/app.sam.desktop.build/backend-dist}"
"$HERE/build-backend.sh"
CONFIG="$(printf '{"bundle":{"resources":{"%s/python/":"backend/python/"}}}' "$SAM_BACKEND_DIST")"
cd "$ROOT/desktop"
npx tauri build --bundles app --config "$CONFIG"
