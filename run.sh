#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -t 1 && "${AURORA_IN_TERMINAL:-}" != "1" ]] && command -v konsole >/dev/null 2>&1; then
    exec env AURORA_IN_TERMINAL=1 \
        konsole --workdir "$APP_DIR" -e bash "$APP_DIR/run.sh"
fi

cd "$APP_DIR"

if command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON="python"
else
    echo "Greska: Python nije pronadjen."
    echo "Instaliraj Python 3.10 ili noviji."
    exit 1
fi

if "$PYTHON" - <<'PY'
import importlib.util
import sys

missing = [
    module
    for module in ("PyQt6", "httpx", "requests")
    if importlib.util.find_spec(module) is None
]
sys.exit(1 if missing else 0)
PY
then
    echo "Requirements su vec instalirani."
else
    echo "Provjeravam i instaliram requirements..."
    "$PYTHON" -m pip install --break-system-packages -r requirements.txt
fi

echo "Pokrecem Aurora IPTV..."
"$PYTHON" main.py
