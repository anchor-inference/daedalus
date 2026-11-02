#!/bin/sh
# Installs daedalus-cli and its short name, dae, into a folder on PATH. The short name is a link to
# the same binary, not a second build: whatever name it is started under, it is one program.
#
#   curl -fsSL https://raw.githubusercontent.com/anchor-inference/daedalus/main/cli/scripts/install.sh | sh
#
#   install.sh                          the newest release's build for this machine (the default)
#   install.sh --tag desktop-v0.23.0    that release's build instead
#   install.sh --from-source            build from the checkout this script is in (needs Go)
#   install.sh ./daedalus-cli           a binary already built or downloaded
#   PREFIX=/usr/local/bin install.sh    another folder than ~/.local/bin
#
# The client is released with the desktop application: every desktop-vX.Y.Z release carries
# daedalus-cli-<os>-<arch>.tar.gz for Linux and macOS (and a .zip for Windows, which install.ps1
# takes), listed in the release's SHA256SUMS. This picks the newest such release exactly as the
# desktop installer does, checks the release's signature over its tag and SHA256SUMS by the
# project's release key, then the archive against SHA256SUMS, and only then unpacks and installs.
# The signature proves who published the release; the checksum catches a broken download.
#
# DAEDALUS_RELEASES_API and DAEDALUS_DOWNLOAD_BASE point it at another release source (a local
# fixture); it says so when they are set.
set -eu

# The release keys this installer trusts: signify public keys (the base64 line), one per line, the
# same as desktop/install.sh and the launcher (desktop/signing.go, desktop/SIGNING.md): the project's
# release key, id df535393fa2485a1 (minisign shows it as A18524FA935353DF). This script, arriving by
# `curl | sh` over TLS unsigned, cannot vouch for itself: compare the key fingerprint it prints with
# the one published outside GitHub (SIGNING.md).
release_keys="RWTfU1OT+iSFoaxGzNfGzkwHdVs2o8WmnCzBUo/LBUw2L4ssGN4xYx/2"

repo="${DAEDALUS_REPO:-anchor-inference/daedalus}"
api="${DAEDALUS_RELEASES_API:-https://api.github.com/repos/$repo}"
downloads="${DAEDALUS_DOWNLOAD_BASE:-https://github.com/$repo/releases/download}"
dest=${PREFIX:-"$HOME/.local/bin"}

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
  say "usage: install.sh [--tag desktop-vX.Y.Z | --from-source | <built binary>]" >&2
  exit 2
}

mode=release
tag=""
src=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from-source) mode=source ;;
    --tag)
      [ $# -ge 2 ] || usage
      tag=$2
      shift
      ;;
    -h | --help) usage ;;
    -*) usage ;;
    *)
      [ -z "$src" ] || usage
      src=$1
      mode=binary
      ;;
  esac
  shift
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT INT TERM

# place <binary>: the one step every way in ends with.
place() {
  mkdir -p "$dest"
  install -m 0755 "$1" "$dest/daedalus-cli"
  ln -sf daedalus-cli "$dest/dae"
  say "installed $dest/daedalus-cli and $dest/dae"
  "$dest/daedalus-cli" version 2>/dev/null || true
  case ":$PATH:" in
    *":$dest:"*) ;;
    *) say "note: $dest is not on PATH" ;;
  esac
}

if [ "$mode" = binary ]; then
  [ -f "$src" ] || fail "$src is not a file"
  place "$src"
  exit 0
fi

if [ "$mode" = source ]; then
  # The checkout is the folder this script sits in, so this works only from a clone, never from a
  # pipe: there is nothing to build there.
  case "$0" in
    */*) here=$(cd "$(dirname "$0")/.." && pwd) ;;
    *) fail "--from-source builds the checkout this script is in; run it as cli/scripts/install.sh from a clone" ;;
  esac
  [ -f "$here/cmd/daedalus-cli/main.go" ] || fail "$here is not daedalus-cli's source folder"
  command -v go >/dev/null 2>&1 || fail "Go is needed to build from source (https://go.dev/dl); or install a release without --from-source"
  version=$(git -C "$here" describe --tags --match 'desktop-v*' 2>/dev/null | sed 's/^desktop-v//' || true)
  [ -n "$version" ] || version="0.1.0-dev+$(git -C "$here" rev-parse --short HEAD 2>/dev/null || echo local)"
  (cd "$here" && CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=$version" -o "$work/daedalus-cli" ./cmd/daedalus-cli)
  place "$work/daedalus-cli"
  exit 0
fi

command -v curl >/dev/null 2>&1 || fail "curl is needed to download the release"
if [ -n "${DAEDALUS_RELEASES_API:-}${DAEDALUS_DOWNLOAD_BASE:-}" ]; then
  say "Using a release source other than GitHub's: ${api} / ${downloads}"
fi

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  *) fail "this script covers Linux and macOS; on Windows run install.ps1 (see cli/README.md)" ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  arm64 | aarch64) arch=arm64 ;;
  *) fail "there is no build for $(uname -m); build it with --from-source" ;;
esac
asset="daedalus-cli-$os-$arch.tar.gz"

if [ -n "$tag" ]; then
  printf '%s\n' "$tag" | grep -Eq '^desktop-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' ||
    fail "$tag is not a desktop-vX.Y.Z tag"
else
  # The newest release whose tag names a desktop version, chosen exactly as desktop/install.sh and
  # the launcher choose: a plain desktop-vX.Y.Z tag, not a draft, not a prerelease, the highest
  # version rather than the first in the listing (which is ordered by date). The listing is JSON
  # and this has no JSON parser: it is cut at commas and braces, and tag_name, draft and prerelease
  # are read from the stretch between two braces, where a release object keeps them.
  say "Looking for the newest release of ${repo}..."
  listing="$(curl -fsSL "$api/releases?per_page=30")" || fail "could not read the list of releases of $repo"
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
  [ -n "$tag" ] || fail "no desktop-v* release found in $repo"
fi
base="$downloads/$tag"

# fetch <url> <file> <what>: retries on any error, then says whether the file is missing or the
# network failed.
fetch() {
  err="$(curl -fL --retry 4 --retry-delay 2 --retry-all-errors --progress-bar -o "$2" "$1" 2>&1 >/dev/null)" && return 0
  case "$err" in
    *"error: 404"*) fail "$tag has no $3 (see https://github.com/$repo/releases/tag/$tag)" ;;
    *) fail "could not download $3 (${err##*curl: }); check the network and run the installer again" ;;
  esac
}

say "Downloading $asset from ${tag}..."
fetch "$base/$asset" "$work/$asset" "$asset"
fetch "$base/SHA256SUMS" "$work/SHA256SUMS" "SHA256SUMS"
sig_err="$(curl -fsSL --retry 4 --retry-delay 2 -o "$work/SHA256SUMS.sig" "$base/SHA256SUMS.sig" 2>&1)" || {
  rm -f "$work/SHA256SUMS.sig"
  case "$sig_err" in
    *"error: 404"*) ;;
    *) fail "could not download SHA256SUMS.sig (${sig_err##*curl: }); not installing an unverified release" ;;
  esac
}

# verify_release: SHA256SUMS.sig must be a signature by one of release_keys over
# "daedalus-release <tag>\n" + SHA256SUMS, the message desktop/sign-release.sh signs. OpenSSL 3 does
# the Ed25519 check; the key id in the signature picks the key. The same steps as desktop/install.sh.
verify_release() {
  [ -f "$work/SHA256SUMS.sig" ] || fail "$tag has no SHA256SUMS.sig, and this installer only installs signed releases; nothing was installed"
  command -v openssl >/dev/null 2>&1 || fail "checking the release's signature needs OpenSSL 3, which is not here; nothing was installed (desktop/SIGNING.md shows the check by hand)"
  printf 'daedalus-release %s\n' "$tag" >"$work/message"
  cat "$work/SHA256SUMS" >>"$work/message"
  # The first line that is not the untrusted comment, and only that one: minisign follows it with a
  # trusted comment and a second signature, which are not this signature.
  awk '!/^untrusted comment:/ && NF { print; exit }' "$work/SHA256SUMS.sig" | tr -d ' \r\n' >"$work/sig.b64"
  openssl base64 -d -A -in "$work/sig.b64" -out "$work/sig.raw" 2>/dev/null || fail "SHA256SUMS.sig is not a signature; nothing was installed"
  [ "$(wc -c <"$work/sig.raw" | tr -d ' ')" = 74 ] || fail "SHA256SUMS.sig is not an Ed25519 signature; nothing was installed"
  [ "$(dd if="$work/sig.raw" bs=1 count=2 2>/dev/null)" = "Ed" ] || fail "SHA256SUMS.sig is a prehashed signature; releases are signed with minisign -S -l (desktop/SIGNING.md); nothing was installed"
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
  [ -f "$work/verified" ] || fail "SHA256SUMS.sig does not verify with this installer's release key for $tag; nothing was installed"
  say "The release's signature verifies (key $sig_id), for $tag."
}

say "Release key fingerprint (SHA-256 of the key line): $(printf '%s' "$release_keys" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -d' ' -f1)"
verify_release

if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$work/$asset" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$work/$asset" | cut -d' ' -f1)"
else
  fail "neither sha256sum nor shasum is available to check the download"
fi
expected="$(grep "  $asset\$" "$work/SHA256SUMS" | cut -d' ' -f1)"
[ -n "$expected" ] || fail "SHA256SUMS does not mention $asset; nothing was installed"
[ "$actual" = "$expected" ] || fail "the download does not match its checksum; nothing was installed"
say "Checksum matches."

mkdir "$work/unpacked"
tar -xzf "$work/$asset" -C "$work/unpacked"
if [ ! -f "$work/unpacked/daedalus-cli" ] || [ -L "$work/unpacked/daedalus-cli" ]; then
  fail "$asset holds no daedalus-cli; nothing was installed"
fi
place "$work/unpacked/daedalus-cli"
