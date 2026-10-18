#!/usr/bin/env bash
# Startet den Mail-Backup-Viewer und öffnet ihn im Browser.
# Usage: ./start.sh [--no-window] [--port PORT] [LIBRARY_DIR]
#   --no-window  Browser nicht automatisch öffnen
#   LIBRARY_DIR  optional; ohne Angabe gilt die gemerkte Bibliothek, sonst fragt der Viewer im Browser
#                Zu sichern sind library.json, icons/, sources/ und tags.db (archive.db ist nur ein Cache).
#   Optional: Mit dem Python-Modul pypff lassen sich .pst/.ost-Dateien im Viewer importieren.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OPEN=1
PORT=8025
LIBRARY_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-window) OPEN=0 ;;
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

if [ "$OPEN" = 1 ]; then
  (sleep 1 && { xdg-open "http://127.0.0.1:$PORT/" >/dev/null 2>&1 || true; }) &
fi
# Das venv enthält das optionale pypff (Outlook-Import).
PY=python3
[ -x "$HERE/.venv/bin/python" ] && PY="$HERE/.venv/bin/python"
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m mailviewer "${args[@]}"
