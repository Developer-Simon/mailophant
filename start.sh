#!/usr/bin/env bash
# Startet den Mail-Backup-Viewer und öffnet ihn im Browser.
# Usage: ./start.sh [--no-window] [--port PORT] [EXPORT_DIR]
#   --no-window  Browser nicht automatisch öffnen
#   EXPORT_DIR   optional; ohne Angabe gilt die gemerkte Auswahl, sonst fragt der Viewer im Browser
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OPEN=1
PORT=8025
EXPORT_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-window) OPEN=0 ;;
    --port) PORT="${2:?--port braucht einen Wert}"; shift ;;
    -*) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    *) EXPORT_DIR="$1" ;;
  esac
  shift
done

CLI="${PROTON_EXPORT_CLI:-$HOME/prg/proton-mail-export/proton-mail-export-cli}"

args=(--port "$PORT")
[ -n "$EXPORT_DIR" ] && args=("$EXPORT_DIR" "${args[@]}")
[ -x "$CLI" ] && args+=(--cli "$CLI")

if [ "$OPEN" = 1 ]; then
  (sleep 1 && { xdg-open "http://127.0.0.1:$PORT/" >/dev/null 2>&1 || true; }) &
fi
exec python3 "$HERE/server.py" "${args[@]}"
