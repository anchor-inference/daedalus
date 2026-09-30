#!/bin/sh
# Installs the Daedalus desktop launcher into a folder of its own:
#
#   curl -fsSL https://raw.githubusercontent.com/anchor-inference/daedalus/main/desktop/install.sh | sh
#
# It takes the newest desktop-v* release, downloads the archive for this machine, checks it against
# the release's SHA256SUMS, and unpacks it into ./Daedalus (or $DAEDALUS_DIR). The launcher then makes
# what the installation owns - the checkouts, the keys, the database - inside that same folder, and,
# natively, the downloaded runtime and this machine's local state (logs, the browser profiles)
# outside it: under ~/.cache and ~/.local/state on Linux, ~/Library/Application Support on macOS.
# `daedalus-desktop uninstall` removes those two; removing the folder then removes the rest.
#
# Run it again over an installation that already has data and it never replaces anything itself.
# A launcher that has `daedalus-desktop upgrade` is handed over to. One too old for that (v0.12.0
# and before) is upgraded by the launcher this script just downloaded and checked, as a bridge
# (`upgrade --bridge`): the same yes, the same protection of the data first - a kept whole copy of the
# data folder on Linux with ext4, a checked backup elsewhere - with the old launcher's files kept
# aside, before anything is replaced, and the same rollback. Where that cannot be
# promised (Docker mode, for now) nothing is changed.
#
# What this checks is the release's signature over its tag and SHA256SUMS, by the project's release
# key, and then that the archive matches SHA256SUMS: the first proves who published the release, the
# second catches a broken download.
#
# DAEDALUS_RELEASES_API and DAEDALUS_DOWNLOAD_BASE point it at another release source (a local
# fixture, in desktop/upgrade-smoke.sh); it says so when they are set.
#
# Nothing here clears a quarantine attribute, and nothing needs to: a file fetched with curl is not
# quarantined in the first place, so the app opens on macOS whether or not the release was signed
# by a Developer ID certificate.
set -eu

# The release keys this installer trusts: signify public keys (the base64 line), one per line, the
# same ones compiled into the launcher (desktop/signing.go, desktop/SIGNING.md): the project's release
# key, id df535393fa2485a1 (minisign shows it as A18524FA935353DF). A release is installed only if
# its SHA256SUMS.sig verifies - over "daedalus-release <tag>" and SHA256SUMS together - before
# anything from it is unpacked or run; without a verifier on this machine the installer refuses.
#
# What this cannot do: vouch for itself. This script arrives by `curl | sh` over TLS, unsigned; a
# forged copy could leave the key out. Checking it end to end needs the key's fingerprint from a
# channel GitHub does not control (SIGNING.md, the README), compared with the one this prints.
release_keys="RWTfU1OT+iSFoaxGzNfGzkwHdVs2o8WmnCzBUo/LBUw2L4ssGN4xYx/2"

repo="${DAEDALUS_REPO:-anchor-inference/daedalus}"
dir="${DAEDALUS_DIR:-./Daedalus}"
api="${DAEDALUS_RELEASES_API:-https://api.github.com/repos/$repo}"
downloads="${DAEDALUS_DOWNLOAD_BASE:-https://github.com/$repo/releases/download}"

say() { printf '%s\n' "$*"; }
fail() { printf '%s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "curl is needed to download the release."
if [ -n "${DAEDALUS_RELEASES_API:-}${DAEDALUS_DOWNLOAD_BASE:-}" ]; then
  say "Using a release source other than GitHub's: ${api} / ${downloads}"
fi

case "$(uname -s)" in
  Darwin) platform="macos" ;;
  Linux) platform="linux" ;;
  *) fail "This installer covers macOS and Linux. On Windows, download the zip from https://github.com/$repo/releases and unpack it with Expand-Archive." ;;
esac

case "$(uname -m)" in
  x86_64 | amd64) arch="amd64" ;;
  arm64 | aarch64) arch="arm64" ;;
  *) fail "There is no build for $(uname -m)." ;;
esac

# The macOS release is one universal bundle for both kinds of Mac; Linux is one tarball per
# architecture, holding the executable with its mode, because a tarball keeps a mode and a zip of a
# bare file effectively does not.
if [ "$platform" = "macos" ]; then
  asset="Daedalus-macOS.zip"
else
  asset="daedalus-desktop-linux-$arch.tar.gz"
fi

# The newest release whose tag names the launcher, chosen exactly as the launcher's own check
# chooses (release.go): a plain desktop-vX.Y.Z tag with no leading zeros, not a draft, not a prerelease, and the highest
# version - not the first in the listing, which is ordered by date and puts a backport of an older
# line above a newer release. The listing is JSON and this has no JSON parser: it is cut at commas
# and at braces, and "tag_name", "draft" and "prerelease" are read from the stretch between two
# braces, in whatever order they come - in a release object they sit between the nested objects
# (author, assets), never inside them. Escaped quotes (inside a release's text) never match.
say "Looking for the newest desktop release of ${repo}..."
listing="$(curl -fsSL "$api/releases?per_page=30")" || fail "Could not read the list of releases of $repo."
tag="$(printf '%s\n' "$listing" | tr ',' '\n' | awk '
  function value(text) { sub(/^[^:]*:[[:space:]]*"/, "", text); sub(/".*$/, "", text); return text }
  function flush() {
    if (tag ~ /^desktop-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/ && draft != "true" && pre != "true") {
      split(substr(tag, 10), v, ".")
      print v[1], v[2], v[3], tag
    }
    tag = ""; draft = ""; pre = ""
  }
  function field(text) {
    if (text ~ /^[[:space:]]*"tag_name"[[:space:]]*:/) tag = value(text)
    else if (text ~ /^[[:space:]]*"draft"[[:space:]]*:/) draft = (text ~ /true/) ? "true" : "false"
    else if (text ~ /^[[:space:]]*"prerelease"[[:space:]]*:/) pre = (text ~ /true/) ? "true" : "false"
  }
  { n = split($0, parts, /[{}]/); for (i = 1; i <= n; i++) { if (i > 1) flush(); field(parts[i]) } }
  END { flush() }
' | sort -k1,1n -k2,2n -k3,3n | tail -n 1 | awk '{ print $4 }')"
[ -n "$tag" ] || fail "No desktop-v* release found in $repo."

base="$downloads/$tag"

mkdir -p "$dir"
target="$(cd "$dir" && pwd)"
if [ "$platform" = "macos" ]; then
  launcher="$target/Daedalus.app/Contents/MacOS/daedalus-desktop"
else
  launcher="$target/daedalus-desktop"
fi

# An installation with data is upgraded by its own launcher when that launcher knows how: it asks,
# protects the data (a kept copy or a checked backup), and rolls back on failure. The question needs a terminal,
# and under `curl | sh` standard input is the script, so it is asked on /dev/tty.
if [ -d "$target/data" ] && [ -x "$launcher" ] && "$launcher" --help 2>/dev/null | grep -q '^  upgrade '; then
  say "An installation with data is already in ${target}; handing over to its launcher's upgrade."
  if (exec </dev/tty) 2>/dev/null; then
    exec "$launcher" upgrade --data "$target/data" </dev/tty
  fi
  fail "Run this in a terminal:  '$launcher' upgrade --data '$target/data'"
fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT INT TERM

# Both downloads retry: a name that fails to resolve for a second, or a connection that drops, is
# the ordinary weather of a laptop on a VPN, and the message tells a missing file apart from that.
fetch() {
  # fetch <url> <file> <what>: retries on any error, then says which of the two kinds it was.
  err="$(curl -fL --retry 4 --retry-delay 2 --retry-all-errors --progress-bar -o "$2" "$1" 2>&1 >/dev/null)" && return 0
  case "$err" in
    *"error: 404"*) fail "$tag has no $3. See https://github.com/$repo/releases/tag/$tag." ;;
    *) fail "Could not download $3 (${err##*curl: }). Check the network and run the installer again." ;;
  esac
}

say "Downloading $asset from ${tag}..."
fetch "$base/$asset" "$work/$asset" "$asset"
fetch "$base/SHA256SUMS" "$work/SHA256SUMS" "SHA256SUMS"

# The signature: required, checked first, and left beside SHA256SUMS, where the bridge below checks
# it again. Retried on network errors only: a release without a signature answers 404 at once, and
# verify_release then refuses it.
sig_err="$(curl -fsSL --retry 4 --retry-delay 2 -o "$work/SHA256SUMS.sig" "$base/SHA256SUMS.sig" 2>&1)" || {
  rm -f "$work/SHA256SUMS.sig"
  case "$sig_err" in
    *"error: 404"*) ;;
    *) fail "Could not download SHA256SUMS.sig (${sig_err##*curl: }); not installing an unverified release." ;;
  esac
}

# verify_release <tag>: SHA256SUMS.sig must be a signature by one of release_keys over
# "daedalus-release <tag>\n" + SHA256SUMS. OpenSSL 3 does the Ed25519 check; the key id in the
# signature picks the key. Anything missing or wrong is a refusal.
verify_release() {
  [ -f "$work/SHA256SUMS.sig" ] || fail "$1 has no SHA256SUMS.sig, and this installer only installs signed releases. Nothing was installed."
  command -v openssl >/dev/null 2>&1 || fail "Checking the release's signature needs OpenSSL 3, which is not here. Nothing was installed; see SIGNING.md for checking it by hand."
  printf 'daedalus-release %s\n' "$1" >"$work/message"
  cat "$work/SHA256SUMS" >>"$work/message"
  # The first line that is not the untrusted comment, and only that one: minisign follows it with a
  # trusted comment and a second signature over that comment, and reading those in as well turned a
  # good signature into base64 that does not decode.
  awk '!/^untrusted comment:/ && NF { print; exit }' "$work/SHA256SUMS.sig" | tr -d ' \r\n' >"$work/sig.b64"
  openssl base64 -d -A -in "$work/sig.b64" -out "$work/sig.raw" 2>/dev/null || fail "SHA256SUMS.sig is not a signature. Nothing was installed."
  [ "$(wc -c <"$work/sig.raw" | tr -d ' ')" = 74 ] || fail "SHA256SUMS.sig is not an Ed25519 signature. Nothing was installed."
  # "Ed" is a signature over the message itself; minisign's default "ED" is over a hash of it, which
  # this check cannot verify; say which it is rather than that the signature is wrong.
  [ "$(dd if="$work/sig.raw" bs=1 count=2 2>/dev/null)" = "Ed" ] || fail "SHA256SUMS.sig is a prehashed signature; releases are signed with minisign -S -l (SIGNING.md). Nothing was installed."
  sig_id="$(dd if="$work/sig.raw" bs=1 skip=2 count=8 2>/dev/null | od -An -tx1 | tr -d ' \n')"
  dd if="$work/sig.raw" bs=1 skip=10 count=64 of="$work/sig.bin" 2>/dev/null
  printf '%s\n' "$release_keys" | while IFS= read -r key; do
    [ -n "$key" ] || continue
    printf '%s' "$key" | openssl base64 -d -A -out "$work/key.raw" 2>/dev/null || continue
    [ "$(wc -c <"$work/key.raw" | tr -d ' ')" = 42 ] || continue
    [ "$(dd if="$work/key.raw" bs=1 skip=2 count=8 2>/dev/null | od -An -tx1 | tr -d ' \n')" = "$sig_id" ] || continue
    # The DER prefix of an Ed25519 SubjectPublicKeyInfo, then the 32 key bytes.
    printf '\060\052\060\005\006\003\053\145\160\003\041\000' >"$work/key.der"
    dd if="$work/key.raw" bs=1 skip=10 count=32 2>/dev/null >>"$work/key.der"
    if openssl pkeyutl -verify -pubin -keyform DER -inkey "$work/key.der" -rawin -in "$work/message" -sigfile "$work/sig.bin" >/dev/null 2>&1; then
      echo verified >"$work/verified"
    fi
    break
  done
  [ -f "$work/verified" ] || fail "SHA256SUMS.sig does not verify with this installer's release key for $1. Nothing was installed."
  say "The release's signature verifies (key $sig_id), for $1."
}

# Compare this with the fingerprint published outside GitHub (SIGNING.md, the README): it is the only
# check on this script itself.
say "Release key fingerprint (SHA-256 of the key line): $(printf '%s' "$release_keys" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -d' ' -f1)"
verify_release "$tag"

# The checksum is the whole reason a release publishes SHA256SUMS: a download that was truncated or
# tampered with in transit is caught here rather than by a launcher that fails to run.
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$work/$asset" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$work/$asset" | cut -d' ' -f1)"
else
  fail "Neither sha256sum nor shasum is available to check the download."
fi
expected="$(grep "  $asset\$" "$work/SHA256SUMS" | cut -d' ' -f1)"
[ -n "$expected" ] || fail "SHA256SUMS does not mention $asset."
[ "$actual" = "$expected" ] || fail "The download does not match its checksum; not installing it."
say "Checksum matches."

# launcher_running <path>: whether a process is running that program. By what the process runs, not
# by its command line: a launcher started as ./daedalus-desktop from its folder has no absolute path
# in its command line, and matching the path there (pgrep -f) missed it and let the bridge replace a
# running launcher's files. Linux names each process's program in /proc/<pid>/exe; macOS has no
# /proc, and lsof names the program a process maps as its text.
launcher_running() {
  want="$(cd "$(dirname "$1")" && pwd -P)/$(basename "$1")"
  if [ -d /proc/self ]; then
    for exe in /proc/[0-9]*/exe; do
      running="$(readlink "$exe" 2>/dev/null)" || continue
      [ "${running% (deleted)}" = "$want" ] && return 0
    done
    return 1
  fi
  for pid in $(pgrep -x "$(basename "$1")" 2>/dev/null); do
    lsof -a -p "$pid" -d txt -Fn 2>/dev/null | grep -qxF "n$want" && return 0
  done
  pgrep -f "$want" >/dev/null 2>&1
}

# The same folder again, under a launcher too old to have `upgrade` (v0.12.0 and before): this
# script does not replace anything itself. The launcher just downloaded and checked does the whole
# upgrade as a bridge - it asks, protects the data and keeps the old launcher's files aside before
# replacing anything, starts the stack on the new code, and puts both back if that fails. When it cannot go ahead (Docker mode, a launcher still running, no yes) it changes
# nothing, and neither does this script.
if [ -d "$target/data" ]; then
  [ -e "$launcher" ] || fail "${target}/data exists but there is no launcher beside it; not touching it."
  if launcher_running "$launcher"; then
    fail "The launcher in ${target} is running; close it and run this again. Nothing was changed."
  fi
  stage="$work/stage"
  mkdir -p "$stage"
  if [ "$platform" = "macos" ]; then
    ditto -x -k "$work/$asset" "$stage"
    bridge="$stage/Daedalus.app/Contents/MacOS/daedalus-desktop"
  else
    tar -xzf "$work/$asset" -C "$stage"
    bridge="$stage/daedalus-desktop"
  fi
  [ -x "$bridge" ] || fail "The archive has no launcher; nothing was changed."
  say "An installation with data is already in ${target}, under a launcher that predates upgrade."
  say "The ${tag} launcher will upgrade it, keeping the data and the launcher from before so that a failure puts both back."
  yes=""
  if [ "${DAEDALUS_UPGRADE_YES:-}" = "1" ]; then yes="--yes"; fi
  status=0
  if [ -n "$yes" ]; then
    "$bridge" upgrade --bridge --yes --root "$target" --data "$target/data" --archive "$work/$asset" --sums "$work/SHA256SUMS" </dev/null || status=$?
  elif (exec </dev/tty) 2>/dev/null; then
    "$bridge" upgrade --bridge --root "$target" --data "$target/data" --archive "$work/$asset" --sums "$work/SHA256SUMS" </dev/tty || status=$?
  else
    fail "The upgrade asks for a yes, and there is no terminal to ask on. Run this in a terminal (or with DAEDALUS_UPGRADE_YES=1). Nothing was changed."
  fi
  exit "$status"
fi

# A launcher with no data beside it has never been run: nothing to back up. Its files are still
# moved aside, not deleted.
if [ -e "$launcher" ]; then
  kept="$target/.daedalus-upgrade/installer-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$kept"
  for item in Daedalus.app daedalus-desktop ptyd browserd miniapp-dist; do
    if [ -e "$target/$item" ]; then mv "$target/$item" "$kept/"; fi
  done
  say "The previous launcher's files are in ${kept}."
fi

if [ "$platform" = "macos" ]; then
  # ditto, not unzip: the bundle carries symlinks and the signature's own extended attributes, and
  # unzip drops both - which leaves an app macOS refuses as damaged.
  ditto -x -k "$work/$asset" "$target"
  say ""
  say "Installed $tag into $target."
  say "Open it:  open '$target/Daedalus.app'"
  say "Or double-click Daedalus in that folder. Docker Desktop must be installed and running."
else
  tar -xzf "$work/$asset" -C "$target"
  chmod +x "$target/daedalus-desktop"
  if [ -f "$target/ptyd" ]; then chmod +x "$target/ptyd"; fi
  say ""
  say "Installed $tag into $target."
  say "Run it:  cd '$target' && ./daedalus-desktop"
  say "Docker Engine with the compose plugin must be installed and running."
fi
say "The launcher makes the checkouts, the keys and the data inside that folder, and natively keeps its"
say "downloaded runtime and local state outside it; \`daedalus-desktop uninstall\` removes those."
