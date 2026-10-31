#!/usr/bin/env bash
set -euo pipefail
APP="${1:-/Applications/Pymss Studio Plugin Test.app}"
EXEC="$APP/Contents/MacOS/pymss-studio"
if [[ ! -x "$EXEC" ]]; then
  echo "Install Pymss Studio Plugin Test.app in /Applications, then run this command again." >&2
  exit 1
fi
export PYMSS_STUDIO_DATA_ROOT="$HOME/.pymss-studio-plugin-test"
export PYMSS_PLUGINS_DIR="$PYMSS_STUDIO_DATA_ROOT/manual-plugins"
export PYMSS_USER_MODELS="$PYMSS_STUDIO_DATA_ROOT/settings/user-models.json"
export PYMSS_MODEL_DIR="$PYMSS_STUDIO_DATA_ROOT/models"
exec "$EXEC"
