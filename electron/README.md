# Electron-Hülle für Mailophant

Die Hülle startet den Python-Server des Viewers (`mailviewer/`, `static/`) und zeigt ihn in einem Electron-Fenster. Sie liegt im Ordner `electron/`; der Viewer selbst bleibt unverändert.

## Entwicklung

```bash
cd electron
npm install
npm start          # startet die App aus dem Quellcode
npm test           # Unit- und Integrationstests
```

`npm start` entfernt `ELECTRON_RUN_AS_NODE` aus der Umgebung, bevor Electron startet (über `scripts/start.js`). Weitere Argumente werden durchgereicht, z. B. `npm start -- --smoke`.

## Laufzeit (Python-Interpreter) bauen

Nur für die full-Variante nötig. `npm run build:runtime` lädt das in `runtime/runtime.lock.json` gepinnte python-build-standalone-Archiv, prüft die SHA-256-Summe und installiert `libpff-python` als Wheel nach `electron/build/python/`.

```bash
npm run build:runtime
```

## Pakete bauen

Zwei Varianten:

- **slim**: ohne Interpreter. Die App braucht ein System-Python 3.10+ mit venv.
- **full**: wie slim, zusätzlich mit gebündeltem Python aus `build/python/`.

```bash
npm run dist:slim   # -> dist/slim/
npm run dist:full   # -> dist/full/  (vorher npm run build:runtime)
```

Ziele: AppImage (Linux), dmg (macOS), nsis (Windows). Die Konfiguration steht in `electron-builder.yml` (slim) und `electron-builder.full.yml` (erbt davon, ergänzt `python`). Die Artefakte tragen die Namen `…-slim.<ext>` bzw. `…-full.<ext>`. `dist/` und `build/` werden nicht eingecheckt.

Hinweis: Paketieren für macOS und Windows geht nur auf dem jeweiligen Betriebssystem bzw. in der CI. Code-Signing und Notarisierung fehlen noch.

## Icon und Fenster

- **Icon**: Quelle ist `assets/icon.svg` (Mailophant-Logo, Variante 6b mit Elefantenkopf). `assets/icon-simple.svg` ist dasselbe ohne Elefanten und dient als Favicon und Titelleisten-Icon der Web-Oberfläche (dort als data-URI in `static/index.html`). Aus `icon.svg` werden `assets/icon.png` (1024×1024, für Linux und als Basis für .icns/.ico) und `assets/icon-512.png` gerendert. electron-builder erzeugt .icns und .ico selbst aus `assets/` (`directories.buildResources`). Nach einer Änderung am SVG neu rendern:

  ```bash
  cd electron
  magick -background none -density 96 assets/icon.svg -resize 1024x1024 -depth 8 PNG32:assets/icon.png
  magick assets/icon.png -resize 512x512 -depth 8 PNG32:assets/icon-512.png
  ```

- **Fenster**: rahmenlos mit eigener, 36 px hoher Kopfzeile. Auf macOS liegt die Ampel darin, unter Windows und Linux die Overlay-Buttons. Die Web-Oberfläche gestaltet die Leiste über `html.electron`. Die Farben in `src/main.js` (`chromeColors`) entsprechen `--sidebar` und `--label` aus `static/css/app.css`; auf Windows und Linux folgen sie dem System-Theme.
- **Menü**: ausgeblendet, die Alt-Taste blendet es ein (dort liegt „Werkzeuge → PST-Unterstützung erneut installieren“).

## Rauchtest

`--smoke` startet den Server, ruft `GET /` ab, beendet die App mit Exitcode 0 (Seite mit `<title` und Status 200) bzw. 1. Es erscheinen keine Dialoge; Fehlermeldungen gehen nach stderr.

```bash
# full, ohne Python im PATH (Interpreter aus dem Paket)
ELECTRON_RUN_AS_NODE= env -i DISPLAY=:0 dist/full/linux-unpacked/mailophant --smoke; echo exit=$?

# slim, ohne System-Python: erwartet Exitcode 1 und die Meldung „Kein nutzbares Python gefunden.“
ELECTRON_RUN_AS_NODE= env -i DISPLAY=:0 dist/slim/linux-unpacked/mailophant --smoke; echo exit=$?
```

Auf Systemen ohne Display unter `xvfb-run -a` starten (`--ozone-platform=headless` stürzt ohne X-Display ab) und `DISPLAY` durch `env -i` durchreichen, wie in der CI.

## Releases (CI)

`.github/workflows/release.yml` läuft bei jedem Tag `v*` (z. B. `git tag v0.2.0 && git push origin v0.2.0`):

1. Python- und Electron-Tests auf `ubuntu-latest`
2. je Runner (`ubuntu-latest`, `macos-latest` für arm64, `macos-15-intel` für x64, `windows-latest`): Version aus dem Tag in `package.json`, `npm ci`, `npm run build:runtime`, full-Paket mit electron-builder
3. Rauchtest des Linux-Pakets mit `--smoke` unter `xvfb-run`, ohne Python im PATH
4. GitHub-Release zum Tag mit AppImage, zwei dmg, NSIS-Installer und `SHA256SUMS.txt`. Tags mit Bindestrich (`v0.2.0-rc.1`) werden als Vorabversion markiert.

Über „Run workflow“ (`workflow_dispatch`) entstehen die Installer nur als Workflow-Artefakte, ohne Release. Die Pakete sind nicht signiert: macOS verlangt beim ersten Start Rechtsklick → Öffnen, Windows zeigt SmartScreen.
