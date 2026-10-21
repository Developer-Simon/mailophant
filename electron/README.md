# Electron shell for Mailophant

The shell starts the viewer's Python server (`mailviewer/`, `static/`) and shows it in an Electron window. It lives in the `electron/` folder; the viewer itself stays unchanged.

## Development

```bash
cd electron
npm install
npm start          # starts the app from source
npm test           # unit and integration tests
```

`npm start` removes `ELECTRON_RUN_AS_NODE` from the environment before Electron starts (via `scripts/start.js`). Further arguments are passed through, e.g. `npm start -- --smoke`.

## Building the runtime (Python interpreter)

Only needed for the full variant. `npm run build:runtime` downloads the python-build-standalone archive pinned in `runtime/runtime.lock.json`, checks its SHA-256 sum and installs `libpff-python` as a wheel into `electron/build/python/`.

```bash
npm run build:runtime
```

## Building packages

Two variants:

- **slim**: without an interpreter. The app needs a system Python 3.10+ with venv.
- **full**: like slim, plus a bundled Python from `build/python/`.

```bash
npm run dist:slim   # -> dist/slim/
npm run dist:full   # -> dist/full/  (run npm run build:runtime first)
```

Targets: AppImage (Linux), dmg (macOS), nsis (Windows). On Linux the full variant also builds a Flatpak bundle, which needs `flatpak-builder` plus `org.freedesktop.Platform`, `org.freedesktop.Sdk` and `org.electronjs.Electron2.BaseApp` in version 26.08 from Flathub. The configuration is in `electron-builder.yml` (slim) and `electron-builder.full.yml` (inherits from it and adds `python`). The artifacts are named `…-slim.<ext>` and `…-full.<ext>`. `dist/` and `build/` are not checked in.

Packaging for macOS and Windows only works on that operating system or in CI. Code signing and notarization are still missing.

## Icon and window

- **Icon**: the source is `assets/icon.svg` (the Mailophant logo, variant 6b with the elephant head). `assets/icon-simple.svg` is the same without the elephant and serves as the favicon and title bar icon of the web UI (as a data URI in `static/index.html`). `assets/icon.png` (1024×1024, for Linux and as the base for .icns/.ico) and `assets/icon-512.png` are rendered from `icon.svg`. electron-builder creates .icns and .ico itself from `assets/` (`directories.buildResources`). Render again after changing the SVG:

  ```bash
  cd electron
  magick -background none -density 96 assets/icon.svg -resize 1024x1024 -depth 8 PNG32:assets/icon.png
  magick assets/icon.png -resize 512x512 -depth 8 PNG32:assets/icon-512.png
  # Linux (AppImage, Flatpak): separate sizes up to 512 px, Flatpak rejects larger ones
  for s in 16 24 32 48 64 128 256 512; do
    magick -background none -density 96 assets/icon.svg -resize ${s}x${s} -depth 8 PNG32:assets/icons/${s}x${s}.png
  done
  ```

- **Window**: frameless with its own 36 px title bar. On macOS the traffic lights sit in it, on Windows and Linux the overlay buttons. The web UI styles the bar through `html.electron`. The colours in `src/main.js` (`chromeColors`) match `--sidebar` and `--label` from `static/css/app.css`; on Windows and Linux they follow the system theme.
- **Menu**: hidden; the Alt key shows it (it holds "Tools → Reinstall PST support").

## Smoke test

`--smoke` starts the server, requests `GET /` and quits the app with exit code 0 (page with `<title` and status 200) or 1. No dialogs appear; error messages go to stderr.

```bash
# full, without Python in PATH (interpreter from the package)
ELECTRON_RUN_AS_NODE= env -i DISPLAY=:0 dist/full/linux-unpacked/mailophant --smoke; echo exit=$?

# slim, without system Python: expects exit code 1 and the message "No usable Python found."
ELECTRON_RUN_AS_NODE= env -i DISPLAY=:0 dist/slim/linux-unpacked/mailophant --smoke; echo exit=$?
```

On systems without a display, run it under `xvfb-run -a` (`--ozone-platform=headless` crashes without an X display) and pass `DISPLAY` through `env -i`, as the CI does.

## Releases (CI)

`.github/workflows/release.yml` runs on every tag `v*` (e.g. `git tag v0.2.0 && git push origin v0.2.0`):

1. Python and Electron tests on `ubuntu-latest`
2. per runner (`ubuntu-latest`, `macos-latest` for arm64, `macos-15-intel` for x64, `windows-latest`): version from the tag into `package.json`, `npm ci`, `npm run build:runtime`, full package with electron-builder
3. smoke test of the Linux package with `--smoke` under `xvfb-run`, without Python in PATH
4. GitHub release for the tag with the AppImage, Flatpak bundle, two dmg files, the NSIS installer and `SHA256SUMS.txt`. Tags with a hyphen (`v0.2.0-rc.1`) are marked as pre-releases.

Every installer in the release carries a build provenance attestation (Sigstore, via `actions/attest-build-provenance`). To check that a file comes from this workflow and tag:

```bash
gh attestation verify Mailophant-0.2.0-x86_64-full.AppImage --repo <owner>/mailophant
```

"Run workflow" (`workflow_dispatch`) produces the installers only as workflow artifacts, without a release. The packages are not signed: on first launch macOS needs right-click → Open, and Windows shows SmartScreen.
