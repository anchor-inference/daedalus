#!/bin/sh
# Builds daedalus-cli for the six release targets and packs each into the archive the install
# scripts download, then writes their SHA256SUMS:
#
#   cli/scripts/package.sh <version> <out folder>
#
#   daedalus-cli-linux-amd64.tar.gz     daedalus-cli + LICENSE
#   daedalus-cli-linux-arm64.tar.gz
#   daedalus-cli-darwin-amd64.tar.gz
#   daedalus-cli-darwin-arm64.tar.gz
#   daedalus-cli-windows-amd64.zip      daedalus-cli.exe + LICENSE
#   daedalus-cli-windows-arm64.zip
#
# The release workflow (.github/workflows/cli.yml, called by desktop.yml on a desktop-v* tag) runs
# exactly this, and the release puts the six archives into its own SHA256SUMS beside the desktop
# files, so the one signature over that file covers them too. A tarball, not a zip, for Linux and
# macOS: it keeps the executable's mode. The archives are flat - the files at the top, no folder -
# and every file in them carries the commit's time and no owner, so the same commit packs into the
# same bytes on any machine with GNU tar, gzip and zip.
set -eu

[ $# -eq 2 ] || { echo "usage: $0 <version> <out folder>" >&2; exit 2; }
version=$1
here=$(cd "$(dirname "$0")/.." && pwd)
licence="$here/../LICENSE"
[ -f "$licence" ] || { echo "package.sh: no LICENSE at the repository's root" >&2; exit 1; }
for tool in go tar gzip zip sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "package.sh: $tool is needed" >&2; exit 1; }
done
epoch=${SOURCE_DATE_EPOCH:-$(git -C "$here" log -1 --format=%ct 2>/dev/null || date +%s)}
mkdir -p "$2"
out=$(cd "$2" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT INT TERM

for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64 windows/arm64; do
  os=${target%/*}
  arch=${target#*/}
  stage="$work/$os-$arch"
  mkdir -p "$stage"
  exe=daedalus-cli
  [ "$os" = windows ] && exe=daedalus-cli.exe
  (cd "$here" && CGO_ENABLED=0 GOOS=$os GOARCH=$arch go build -trimpath -ldflags "-s -w -X main.version=$version" \
    -o "$stage/$exe" ./cmd/daedalus-cli)
  cp "$licence" "$stage/LICENSE"
  chmod 0755 "$stage/$exe"
  chmod 0644 "$stage/LICENSE"
  touch -d "@$epoch" "$stage/$exe" "$stage/LICENSE"
  name="daedalus-cli-$os-$arch"
  rm -f "$out/$name.zip" "$out/$name.tar.gz"
  if [ "$os" = windows ]; then
    (cd "$stage" && TZ=UTC zip -q -X "$out/$name.zip" "$exe" LICENSE)
  else
    tar -C "$stage" --owner=0 --group=0 --numeric-owner -cf - "$exe" LICENSE | gzip -n -9 >"$out/$name.tar.gz"
  fi
done

# Written outside the folder first, as the release does: a checksum file cannot cover itself.
(cd "$out" && sha256sum daedalus-cli-*.tar.gz daedalus-cli-*.zip >"$work/SHA256SUMS")
mv "$work/SHA256SUMS" "$out/SHA256SUMS"
cat "$out/SHA256SUMS"
