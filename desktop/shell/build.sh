#!/usr/bin/env bash
# Builds the desktop application on this machine, for trying it out:
#
#   desktop/shell/build.sh linux     the .deb, the AppImage and the unpacked folder, x86-64
#   desktop/shell/build.sh windows   the installer and the unpacked folder, under Wine in a container
#
# It stages what the application carries beside the shell — the launcher and browserd built from
# this tree (CGO off, so from any machine), the Mini App built from miniapp/ — and runs
# electron-builder. ptyd is left out: it needs Zig, and the application runs without it (host
# terminals are then unavailable). The release workflow stages every piece; this is for a quick look.
# macOS needs a Mac (the .dmg and the signature are Apple's tools); the workflow builds it.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
target="${1:-linux}"
version="${VERSION:-0.0.0}"

case "$target" in
  linux) goos=linux; stage="$here/stage/linux-x64"; exe="" ;;
  windows) goos=windows; stage="$here/stage/win-x64"; exe=".exe" ;;
  *) echo "usage: build.sh linux|windows" >&2; exit 2 ;;
esac

rm -rf "$stage" "$here/stage/miniapp-dist"
mkdir -p "$stage"
(cd "$root/desktop" && CGO_ENABLED=0 GOOS=$goos GOARCH=amd64 go build -trimpath -ldflags "-s -w -X main.version=desktop-v$version" -o "$stage/daedalus-desktop$exe" .)
(cd "$root/browserd" && CGO_ENABLED=0 GOOS=$goos GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o "$stage/browserd$exe" ./cmd/browserd)
(cd "$root/miniapp" && npm ci --no-audit --no-fund && npm run build)
cp -R "$root/miniapp/dist" "$here/stage/miniapp-dist"
cp "$root/docs/brand/avatar-bot.png" "$here/build/icon.png"

cd "$here"
npm ci --no-audit --no-fund
if [ "$target" = linux ]; then
  npx electron-builder --linux deb AppImage dir --x64 -c.extraMetadata.version="$version" --publish never
else
  # rcedit, which puts the icon and the version into Daedalus.exe, is a Windows program.
  docker run --rm -v "$here":/project -w /project -u "$(id -u):$(id -g)" -e HOME=/tmp/home \
    electronuserland/builder:wine \
    sh -c "mkdir -p /tmp/home && npx electron-builder --win nsis dir --x64 -c.extraMetadata.version=$version --publish never"
fi
ls -lh dist
