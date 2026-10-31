#!/usr/bin/env bash
set -euo pipefail
APP="${1:-/Applications/Pymss Studio Plugin Test.app}"
EXEC="$APP/Contents/MacOS/pymss-studio"
if [[ ! -x "$EXEC" ]]; then
  echo "Install Pymss Studio Plugin Test.app in /Applications, then run this command again." >&2
  exit 1
fi
KIT="$(cd "$(dirname "$0")" && pwd)"
RES="$APP/Contents/Resources"
if [[ ! -f "$RES/python/worker.py" ]]; then RES="$RES/resources"; fi
REVISION=$(PYTHONHOME="$RES/python-runtime" PYTHONDONTWRITEBYTECODE=1 "$RES/python-runtime/bin/python3" -B -c 'import json,sys;info=json.load(open(sys.argv[1]));assert info==json.load(open(sys.argv[2])),"The application and companion kit belong to different builds";print(info["snapshotDigest"][:8])' "$KIT/build-info.json" "$RES/python/plugin-test-build.json")
export PYMSS_STUDIO_DATA_ROOT="$HOME/.pymss-studio-plugin-test-$REVISION"
export PYMSS_PLUGINS_DIR="$PYMSS_STUDIO_DATA_ROOT/manual-plugins"
export PYMSS_USER_MODELS="$PYMSS_STUDIO_DATA_ROOT/settings/user-models.json"
export PYMSS_MODEL_DIR="$PYMSS_STUDIO_DATA_ROOT/models"
exec "$EXEC"
