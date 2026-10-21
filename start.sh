#!/usr/bin/env bash
# Starts Mailophant.
# Usage: ./start.sh                                   Electron app (window)
#        ./start.sh --native-picker                   Electron app with the system file dialog (as in the Flatpak)
#        ./start.sh --server [--port PORT] [LIBRARY_DIR]  only the server, no window
#   Any other argument starts only the server (http://127.0.0.1:PORT/).
#   --server     start the server without further options
#   LIBRARY_DIR  optional; without it the remembered library is used, otherwise the viewer asks in the browser
#                Back up library.json, icons/, sources/ and tags.db (archive.db is only a cache).
#   Optional: with the Python module pypff, the viewer can import .pst/.ost files.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -eq 1 ] && [ "$1" = --native-picker ]; then
  export MAILOPHANT_NATIVE_PICKER=1
  shift
fi

if [ $# -eq 0 ]; then
  cd "$HERE/electron"
  [ -d node_modules ] || npm install
  exec npm start
fi

PORT=8025
LIBRARY_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --server|--no-window) ;;
    --port) PORT="${2:?--port braucht einen Wert}"; shift ;;
    --native-picker) echo "--native-picker only applies to the Electron app, not with --server" >&2; exit 1 ;;
    -*) echo "Unknown option: $1" >&2; exit 1 ;;
    *) LIBRARY_DIR="$1" ;;
  esac
  shift
done

CLI="${PROTON_EXPORT_CLI:-$HOME/prg/proton-mail-export/proton-mail-export-cli}"

args=(--port "$PORT")
[ -n "$LIBRARY_DIR" ] && args=("$LIBRARY_DIR" "${args[@]}")
[ -x "$CLI" ] && args+=(--cli "$CLI")

# The venv holds the optional pypff (Outlook import).
PY=python3
[ -x "$HERE/.venv/bin/python" ] && PY="$HERE/.venv/bin/python"
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m mailviewer "${args[@]}"
