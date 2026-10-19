#!/bin/sh
# Launcher inside the Flatpak: zypak replaces the Chromium sandbox, which doesn't work in Flatpak.
# When started from a VS Code terminal, Electron would otherwise run as plain Node.
unset ELECTRON_RUN_AS_NODE
export TMPDIR="${XDG_RUNTIME_DIR}/app/${FLATPAK_ID}"
exec zypak-wrapper /app/mailophant/mailophant "$@"
