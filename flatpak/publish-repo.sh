#!/usr/bin/env bash
# Builds the app into the Flatpak repo on GitHub Pages and writes the whole site to deploy.
# The published repo is mirrored first, so earlier versions stay: updates and static deltas need them.
#
#   publish-repo.sh stable|beta [site-dir]
#
# Signs with the key from the current GnuPG home, which has to match repo-signing-key.asc.
# Needs flatpak, flatpak-builder, ostree, gpg and curl; locally they all come with org.flatpak.Builder,
# see README.md.
set -euo pipefail

branch=$1
site=$(realpath -m "${2:-site}")
here=$(dirname "$(realpath "$0")")
app_id=io.github.developer_simon.mailophant
pages_url=${PAGES_URL:-https://developer-simon.github.io/mailophant}
keep=${KEEP_VERSIONS:-3} # versions per branch; GitHub Pages sites may be 1 GB at most

case $branch in
  stable|beta) ;;
  *) echo "branch must be stable or beta, not '$branch'" >&2; exit 1 ;;
esac

key_file=$here/repo-signing-key.asc
fingerprint=$(gpg --show-keys --with-colons "$key_file" | awk -F: '$1 == "fpr" { print $10; exit }')
if ! gpg --list-secret-keys "$fingerprint" >/dev/null 2>&1; then
  echo "secret key $fingerprint (repo-signing-key.asc) is not in the GnuPG home" >&2
  exit 1
fi
gpg_key=$(gpg --dearmor <"$key_file" | base64 -w0)

rm -rf "$site"
mkdir -p "$site"
repo=$site/repo
ostree init --repo="$repo" --mode=archive-z2

# A 404 means nothing is published yet. Any other failure stops here: starting over would cut
# existing installations off from updates.
status=$(curl -sS -o /dev/null -w '%{http_code}' "$pages_url/repo/config")
case $status in
  200)
    ostree remote add --repo="$repo" --gpg-import="$key_file" published "$pages_url/repo"
    ostree pull --repo="$repo" --mirror --depth=$((keep - 1)) published
    ostree remote delete --repo="$repo" published
    ;;
  404) echo "Nothing published at $pages_url yet, starting a new repo" ;;
  *) echo "$pages_url/repo/config answered HTTP $status" >&2; exit 1 ;;
esac

flatpak-builder --user --force-clean --disable-rofiles-fuse --default-branch="$branch" \
  --repo="$repo" --gpg-sign="$fingerprint" "$here/build-dir" "$here/$app_id.yml"

# Nobody installs the debug symbols from here, they would only fill up the site.
for ref in $(ostree refs --repo="$repo" | grep -F "runtime/$app_id.Debug/" || true); do
  ostree refs --repo="$repo" --delete "$ref"
done

flatpak build-update-repo --gpg-sign="$fingerprint" --title=Mailophant --default-branch=stable \
  --generate-static-deltas --prune --prune-depth=$((keep - 1)) "$repo"

install -m644 "$here/../electron/assets/icons/128x128.png" "$site/icon.png"

cat >"$site/mailophant.flatpakrepo" <<EOF
[Flatpak Repo]
Title=Mailophant
Url=$pages_url/repo/
Homepage=https://github.com/Developer-Simon/mailophant
Comment=Read and archive your mail backups
Icon=$pages_url/icon.png
GPGKey=$gpg_key
EOF

# One .flatpakref per branch that exists; installing it adds the remote 'mailophant' and pulls the
# runtime from Flathub.
has_beta=false
for b in stable beta; do
  ostree refs --repo="$repo" | grep -qxF "app/$app_id/x86_64/$b" || continue
  [ "$b" = beta ] && has_beta=true
  name=mailophant; title=Mailophant
  [ "$b" = beta ] && name=mailophant-beta && title="Mailophant (beta)"
  cat >"$site/$name.flatpakref" <<EOF
[Flatpak Ref]
Name=$app_id
Branch=$b
Title=$title
Url=$pages_url/repo/
SuggestRemoteName=mailophant
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
IsRuntime=false
Homepage=https://github.com/Developer-Simon/mailophant
Icon=$pages_url/icon.png
GPGKey=$gpg_key
EOF
done

# The install page; the beta section only appears once there is a beta.
sed -e "s|@PAGES_URL@|$pages_url|g" -e "s|@FINGERPRINT@|$fingerprint|g" "$here/pages/index.html" |
  if $has_beta; then cat; else sed '/<!-- beta -->/,/<!-- \/beta -->/d'; fi >"$site/index.html"

ostree refs --repo="$repo"
du -sh "$site"
