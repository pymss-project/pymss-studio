#!/bin/bash
set -euo pipefail
KIT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${1:-/Applications/Pymss Studio Timing Test.app}"
if [ ! -d "$APP" ] && [ "$#" -eq 0 ]; then APP="$HOME/Applications/Pymss Studio Timing Test.app"; fi
if [ ! -d "$APP" ]; then
  echo "Test application missing. Install Pymss Studio Timing Test.app first, or pass its path as the first argument." >&2
  exit 1
fi
test "$(/usr/bin/plutil -extract CFBundleIdentifier raw -o - "$APP/Contents/Info.plist")" = 'studio.pymss.timing-test.b48688e26'
test "$(/usr/bin/plutil -extract CFBundleShortVersionString raw -o - "$APP/Contents/Info.plist")" = '0.0.25-dev.timing.20261103.b48688e26'
EXECUTABLE="$(/usr/bin/plutil -extract CFBundleExecutable raw -o - "$APP/Contents/Info.plist")"
EXPECTED_EXECUTABLE_SHA='__CI_EXECUTABLE_SHA256__'
test "$(/usr/bin/shasum -a 256 "$APP/Contents/MacOS/$EXECUTABLE" | /usr/bin/awk '{print $1}')" = "$EXPECTED_EXECUTABLE_SHA"
/usr/bin/codesign --verify --deep --strict "$APP"
(cd "$KIT_ROOT" && /usr/bin/shasum -a 256 -c SHA256SUMS.txt > /dev/null)
# Scope quarantine handling to this verified test bundle and its plugin source.
/usr/bin/xattr -rc -s "$APP"
/usr/bin/xattr -rc -s "$KIT_ROOT"
RES="$APP/Contents/Resources"
if [ ! -f "$RES/python/worker.py" ]; then RES="$RES/resources"; fi
unset PYMSS_STUDIO_PYTHON PYMSS_STUDIO_WORKER PYMSS_PLUGIN_BINDING PYMSS_PLUGINS_DIR _PYMSS_CLI_CORE PYTHONPATH
unset PYMSS_STUDIO_RUNTIME_ENVS_DIR PYMSS_STUDIO_ACTIVE_RUNTIME_FILE PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR PYMSS_STUDIO_BOOTSTRAP_PYTHON PYMSS_STUDIO_ALLOW_DEBUG_RUNTIME_OVERRIDE
export PYTHONHOME="$RES/python-runtime" PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
export NUMBA_CACHE_DIR="$HOME/.pymss-studio-timing-test-b48688e26/numba"
export PYMSS_PLUGIN_ENVIRONMENTS_DIR="$HOME/.pymss-studio-timing-test-b48688e26/cli-environments"
export PYMSS_SCRATCH_DIR="$HOME/.pymss-studio-timing-test-b48688e26/scratch"
mkdir -p "$NUMBA_CACHE_DIR" "$PYMSS_PLUGIN_ENVIRONMENTS_DIR" "$PYMSS_SCRATCH_DIR"
run_acceptance() {
  "$RES/python-runtime/bin/python3" -B -X utf8 "$KIT_ROOT/acceptance.py" "$1" --app "$APP"
}
