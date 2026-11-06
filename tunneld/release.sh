#!/bin/sh
# Builds the tunneld a release carries for one platform:
#
#   tunneld/release.sh <goos> <goarch> <output>
#
# tunneld is plain Go with no C in it, so every platform is cross-compiled from any machine with the Go
# of go.mod. Its version is the release's tag when there is one, which is how a bug report names it.
set -eu

goos=$1
goarch=$2
out=$3
here=$(cd "$(dirname "$0")" && pwd)

case "$goos/$goarch" in
  linux/amd64 | linux/arm64 | windows/amd64 | darwin/amd64 | darwin/arm64) ;;
  *) echo "release.sh: no release build for $goos/$goarch" >&2; exit 2 ;;
esac

version=${GITHUB_REF_NAME:-dev}
cd "$here"
CGO_ENABLED=0 GOOS=$goos GOARCH=$goarch go build -trimpath -ldflags "-s -w -X main.version=$version" -o "$out" .
echo "release.sh: $out ($goos/$goarch, $version)" >&2
