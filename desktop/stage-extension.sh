#!/usr/bin/env bash
# Stages the Daedalus Chrome extension the application carries: built from extension/, with a
# SHA256SUMS over every file. The launcher copies it into the operator's local state only after
# checking each file against that list and the manifest's key against the pinned extension id
# (desktop/userbrowser_extension.go); the list itself travels inside the signed release.
#
#   desktop/stage-extension.sh <out-dir>
#
# A tree without extension/ stages a folder with no SHA256SUMS in it, which the launcher reads as
# "this build carries no extension" rather than as a damaged one.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:?usage: stage-extension.sh <out-dir>}"

rm -rf "$out"
mkdir -p "$out"
if [ ! -f "$root/extension/package.json" ]; then
  echo "this tree has no extension/; the application carries none" > "$out/NOT-BUILT"
  exit 0
fi
(cd "$root/extension" && npm ci --no-audit --no-fund && npm run build)
cp -R "$root/extension/dist/." "$out/"
test -f "$out/manifest.json"
(
  cd "$out"
  find . -type f ! -name SHA256SUMS -print | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r file; do
    sha256sum "$file"
  done > SHA256SUMS
)
echo "staged the extension: $(wc -l < "$out/SHA256SUMS") files"
