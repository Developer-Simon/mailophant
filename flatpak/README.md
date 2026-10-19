# Flatpak

Manifest for Flathub. App ID `io.github.developer_simon.mailophant`: Flatpak only allows `-` in the last
part of the ID, hence `_` instead of `-` in the GitHub name. The electron-builder `appId` stays unchanged.

Inside the Flatpak the app uses the runtime's Python (`MAILOPHANT_PYTHON=/usr/bin/python3`, no venv);
pypff is built from the sdist and lives in `/app`.

The app has no filesystem access. With `MAILOPHANT_NATIVE_PICKER=1` it picks the library, sources and
.pst files in the system dialog; the document portal makes them visible under `/run/user/<uid>/doc/…`
(`mailviewer/portal.py`). SQLite WAL doesn't work there, so the databases use a rollback journal; a
library from the AppImage version is switched over once when it's opened. The host path is what gets displayed.

## Building locally

Once:

    flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak install --user flathub org.flatpak.Builder org.freedesktop.Sdk//26.08 \
      org.freedesktop.Sdk.Extension.node24//26.08 org.electronjs.Electron2.BaseApp//26.08

Build, install, run (in the `flatpak/` folder):

    flatpak run org.flatpak.Builder --user --force-clean --disable-rofiles-fuse --repo=repo \
      build-dir io.github.developer_simon.mailophant.yml
    flatpak install --user --reinstall ./repo io.github.developer_simon.mailophant
    flatpak run io.github.developer_simon.mailophant

Lint like Flathub does:

    flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest io.github.developer_simon.mailophant.yml
    flatpak run --command=flatpak-builder-lint org.flatpak.Builder repo repo

Locally, the linters also report `appstream-external-screenshot-url` and
`appstream-screenshots-not-mirrored-in-ostree`; on Flathub the build mirrors the screenshots itself.

## After changes to electron/package-lock.json

Regenerate `generated-sources.json`:

    pipx run --spec "git+https://github.com/flatpak/flatpak-builder-tools.git#subdirectory=node" \
      flatpak-node-generator npm electron/package-lock.json -o flatpak/generated-sources.json
