#!/usr/bin/env bash
set -euo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
APP="${1:-/Applications/Pymss Studio Plugin Test.app}"
RES="$APP/Contents/Resources"
if [[ ! -f "$RES/python/worker.py" ]]; then RES="$RES/resources"; fi
PYTHON="$RES/python-runtime/bin/python3"
if [[ ! -x "$PYTHON" ]]; then echo "Install the test application first." >&2; exit 1; fi
PYTHONHOME="$RES/python-runtime" PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B "$KIT/check_gui_results.py" --app "$APP" --kit "$KIT"
