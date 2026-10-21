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

## Flatpak repo on GitHub Pages

Until the app is on Flathub (and for pre-releases afterwards), release tags publish it to our own
Flatpak repo at https://developer-simon.github.io/mailophant/, with an install page and
`mailophant.flatpakref`. Tags like `v0.3.0` go to branch `stable`, tags with a `-` like
`v0.3.0-beta.1` to `beta` (`mailophant-beta.flatpakref`). The `flatpak-repo` job in
`.github/workflows/release.yml` runs `publish-repo.sh`: it mirrors the published repo, builds into
it, drops the debug symbols, signs, keeps the last 3 versions per branch and generates the site.

The repo is signed with the key in `repo-signing-key.asc`. Every installation trusts that key, so it
can't be swapped without breaking updates: keep a backup of the private key offline.

Setup, once:

    gh secret set FLATPAK_REPO_GPG_KEY < .secrets/flatpak-repo-private.asc   # armored, no passphrase
    gh api -X POST repos/Developer-Simon/mailophant/pages -f build_type=workflow
    gh api -X POST repos/Developer-Simon/mailophant/environments/github-pages/deployment-branch-policies \
      -f name='v*' -f type=tag

The last command lets tags deploy; by default the `github-pages` environment only accepts the
default branch.

Trying it locally (the Builder Flatpak has all the tools; `GNUPGHOME` holds the private key,
`PAGES_URL` can point to a local `python3 -m http.server` serving an earlier `site/`):

    flatpak run --env=FLATPAK_USER_DIR=$HOME/.local/share/flatpak --env=GNUPGHOME=… \
      --command=bash org.flatpak.Builder flatpak/publish-repo.sh stable flatpak/site

## After changes to electron/package-lock.json

Regenerate `generated-sources.json`:

    pipx run --spec "git+https://github.com/flatpak/flatpak-builder-tools.git#subdirectory=node" \
      flatpak-node-generator npm electron/package-lock.json -o flatpak/generated-sources.json
