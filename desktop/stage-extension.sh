#!/usr/bin/env bash
# Stages the Daedalus Chrome extension the application carries: the release zip of
# github.com/anchor-inference/daedalus-chrome-extension named by desktop/extension.lock, unpacked.
# The extension is built and tested in its own repository; this tree only pins which release it
# ships, so there is one source for its code and one build of the artifact.
#
#   desktop/stage-extension.sh <out-dir>
#
# The zip is refused unless its sha256 is the lock's, and its own SHA256SUMS must then match every
# file and list nothing more. The launcher checks that list again, and the manifest's key against the
# pinned id, before it copies the folder where Chrome loads it from (desktop/userbrowser_extension.go);
# the list itself travels inside the signed release.
#
# DAEDALUS_EXTENSION_ZIP=<file> takes the zip from disk instead, for a build without the network. It
# is held to the same digest: an unreleased build is tried with the launcher's
# DAEDALUS_BROWSER_EXTENSION=<unpacked folder>, not by staging it into a package.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:?usage: stage-extension.sh <out-dir>}"
lock="$root/desktop/extension.lock"

version="$(sed -n 's/^version=//p' "$lock")"
want="$(sed -n 's/^sha256=//p' "$lock")"
if [ -z "$version" ] || [ "${#want}" -ne 64 ]; then
  echo "$lock needs a version= and a sha256= line" >&2
  exit 1
fi

# The Mac runner has shasum and no sha256sum; both read and write the same list format.
if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$@"; }
else
  sha256() { shasum -a 256 "$@"; }
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
zip="$work/extension.zip"
if [ -n "${DAEDALUS_EXTENSION_ZIP:-}" ]; then
  cp "$DAEDALUS_EXTENSION_ZIP" "$zip"
else
  url="https://github.com/anchor-inference/daedalus-chrome-extension/releases/download/v$version/daedalus-chrome-extension-$version.zip"
  curl -fsSL --retry 3 -o "$zip" "$url"
fi
got="$(sha256 "$zip" | cut -d' ' -f1)"
if [ "$got" != "$want" ]; then
  echo "the extension zip's sha256 is $got; desktop/extension.lock pins $want for $version" >&2
  exit 1
fi

rm -rf "$out"
mkdir -p "$out"
# Windows' Git Bash may come without unzip; its runner has 7-Zip, which the packaging uses already.
if command -v unzip >/dev/null 2>&1; then
  unzip -q "$zip" -d "$out"
else
  7z x -bd -y "-o$out" "$zip" >/dev/null
fi
(
  cd "$out"
  test -f manifest.json
  sha256 -c SHA256SUMS >/dev/null
  # A file the list leaves out would be refused by the launcher at the operator's first start;
  # refusing it here keeps such a zip out of a release instead.
  listed="$(sed 's/^[0-9a-f]*  //' SHA256SUMS | LC_ALL=C sort)"
  present="$(find . -type f ! -name SHA256SUMS -print | sed 's|^\./||' | LC_ALL=C sort)"
  if [ "$listed" != "$present" ]; then
    echo "the extension's SHA256SUMS and its files differ" >&2
    exit 1
  fi
)
echo "staged the extension $version: $(wc -l < "$out/SHA256SUMS" | tr -d ' ') files"
