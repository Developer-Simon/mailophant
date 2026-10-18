#!/usr/bin/env bash
# Startet Mailophant.
# Usage: ./start.sh                                   Electron-App (Fenster)
#        ./start.sh --server [--port PORT] [LIBRARY_DIR]  nur der Server, ohne Fenster
#   Sobald ein Argument übergeben wird, startet nur der Server (http://127.0.0.1:PORT/).
#   --server     Server ohne weitere Optionen starten
#   LIBRARY_DIR  optional; ohne Angabe gilt die gemerkte Bibliothek, sonst fragt der Viewer im Browser
#                Zu sichern sind library.json, icons/, sources/ und tags.db (archive.db ist nur ein Cache).
#   Optional: Mit dem Python-Modul pypff lassen sich .pst/.ost-Dateien im Viewer importieren.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
    -*) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    *) LIBRARY_DIR="$1" ;;
  esac
  shift
done

CLI="${PROTON_EXPORT_CLI:-$HOME/prg/proton-mail-export/proton-mail-export-cli}"

args=(--port "$PORT")
[ -n "$LIBRARY_DIR" ] && args=("$LIBRARY_DIR" "${args[@]}")
[ -x "$CLI" ] && args+=(--cli "$CLI")

# Das venv enthält das optionale pypff (Outlook-Import).
PY=python3
[ -x "$HERE/.venv/bin/python" ] && PY="$HERE/.venv/bin/python"
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m mailviewer "${args[@]}"
