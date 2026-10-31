#!/usr/bin/env bash
set -euo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
APP="${1:-/Applications/Pymss Studio Plugin Test.app}"
RES="$APP/Contents/Resources"
if [[ ! -f "$RES/python/worker.py" ]]; then RES="$RES/resources"; fi
PYTHON="$RES/python-runtime/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  echo "Install Pymss Studio Plugin Test.app in /Applications, then run this command again." >&2
  exit 1
fi
REPORT="$HOME/Library/Logs/PymssStudioPluginTest/$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$REPORT"
PYTHONHOME="$RES/python-runtime" PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B "$KIT/verify.py" \
  --app "$APP" --kit "$KIT" --report "$REPORT" 2>&1 | tee "$REPORT/console.log"
echo "Verification completed. Report: $REPORT"
