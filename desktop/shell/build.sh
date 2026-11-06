#!/usr/bin/env bash
# Builds the desktop application on this machine, for trying it out:
#
#   desktop/shell/build.sh linux     the .deb, the AppImage and the unpacked folder, x86-64
#   desktop/shell/build.sh windows   the installer and the unpacked folder, under Wine in a container
#
# It stages what the application carries beside the shell — the launcher, browserd and deskd built
# from this tree (CGO off, so from any machine), the Mini App built from miniapp/, the Chrome
# extension's release pinned by desktop/extension.lock (downloaded, or DAEDALUS_EXTENSION_ZIP= for a
# build without the network), agent-desktop's release pinned by desktop/agent-desktop.lock, checked as
# the release checks it (desktop/stage-agent-desktop.sh says how to build without the network or with
# a gh too old to check provenance) — and runs electron-builder. ptyd is left out unless PTYD= names one
# built by ptyd/release.sh for the same system: it needs Zig, and the application runs without it
# (host terminals are then unavailable). So is the seed unless SEED= names a folder made by
# `daedalus-desktop make-seed`: without one the first run downloads, as a launcher built from source
# does. The release workflow stages every piece; this is for a quick look.
# macOS needs a Mac (the .dmg and the signature are Apple's tools); the workflow builds it.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
target="${1:-linux}"
version="${VERSION:-0.0.0}"

case "$target" in
  linux) goos=linux; stage="$here/stage/linux-x64"; seed="$here/stage/seed-linux-x64"; platform=linux-amd64; exe="" ;;
  windows) goos=windows; stage="$here/stage/win-x64"; seed="$here/stage/seed-win-x64"; platform=windows-amd64; exe=".exe" ;;
  *) echo "usage: build.sh linux|windows" >&2; exit 2 ;;
esac

rm -rf "$stage" "$seed" "$here/stage/miniapp-dist" "$here/stage/notices"
mkdir -p "$stage" "$seed"
if [ -n "${SEED:-}" ]; then
  cp -R "$SEED/manifest.json" "$SEED/code" "$SEED/$platform" "$seed/"
fi
(cd "$root/desktop" && CGO_ENABLED=0 GOOS=$goos GOARCH=amd64 go build -trimpath -ldflags "-s -w -X main.version=desktop-v$version" -o "$stage/daedalus-desktop$exe" .)
(cd "$root/browserd" && CGO_ENABLED=0 GOOS=$goos GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o "$stage/browserd$exe" ./cmd/browserd)
(cd "$root/tunneld" && CGO_ENABLED=0 GOOS=$goos GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o "$stage/tunneld$exe" .)
(cd "$root/deskd" && CGO_ENABLED=0 GOOS=$goos GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o "$stage/deskd$exe" ./cmd/deskd)
if [ -n "${PTYD:-}" ]; then
  cp "$PTYD" "$stage/ptyd$exe"
fi
(cd "$root/miniapp" && npm ci --no-audit --no-fund && npm run build)
cp -R "$root/miniapp/dist" "$here/stage/miniapp-dist"
"$root/desktop/stage-extension.sh" "$here/stage/browser-extension"
"$root/desktop/stage-agent-desktop.sh" "$here/stage/agent-desktop" "$platform"
cp "$here/stage/agent-desktop/$platform"/* "$stage/"
mkdir -p "$here/stage/notices"
cp "$here/stage/agent-desktop/THIRD_PARTY_NOTICES.txt" "$here/stage/notices/"
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
