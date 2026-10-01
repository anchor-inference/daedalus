#!/bin/sh
# Builds the browserd a release carries for one platform:
#
#   browserd/release.sh <goos> <goarch> <output>
#
# browserd is plain Go with no C in it, so every platform is cross-compiled from any machine with the
# Go of go.mod, a Mac's included; only the Mac application's signing needs a Mac, and electron-builder
# does it in the release workflow.
#
# The version is the same digest of the daemon's sources the image stamps (src-<12 hex>): its own
# module and the shared packages of ptyd it builds against, hashed as deploy/Dockerfile hashes them,
# over the files .dockerignore lets through. A daemon from a release and one from an image of the
# same sources say they are the same.
set -eu

goos=$1
goarch=$2
out=$3
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")

case "$goos/$goarch" in
  linux/amd64 | linux/arm64 | windows/amd64 | windows/arm64 | darwin/amd64 | darwin/arm64) ;;
  *) echo "release.sh: no release build for $goos/$goarch" >&2; exit 2 ;;
esac

cd "$root"
if command -v sha256sum >/dev/null 2>&1; then sum=sha256sum; else sum="shasum -a 256"; fi
# shellcheck disable=SC2086 # $sum is a command and its flags
digest=$(find browserd ptyd/proto ptyd/go.mod ptyd/go.sum \( -path browserd/.cache -o -path browserd/browserd -o -name '*.test' \) -prune -o -type f -print0 \
  | LC_ALL=C sort -z | xargs -0 $sum | $sum | cut -c1-12)

cd "$here"
CGO_ENABLED=0 GOOS=$goos GOARCH=$goarch go build -trimpath \
  -ldflags "-s -w -X github.com/ascorblack/daedalus/browserd/internal/version.Version=src-$digest" \
  -o "$out" ./cmd/browserd
echo "release.sh: $out ($goos/$goarch, src-$digest)" >&2
