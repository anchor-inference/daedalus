#!/bin/sh
# Installs daedalus-cli and its short name, dae, into a folder on PATH. The short name is a link to
# the same binary, not a second build: whatever name it is started under, it is one program.
#
#   scripts/install.sh                      build from this checkout (needs Go) and install
#   scripts/install.sh ./daedalus-cli       install a binary already built or downloaded
#   PREFIX=/usr/local/bin scripts/install.sh
set -eu

dest=${PREFIX:-"$HOME/.local/bin"}
src=${1:-}
here=$(cd "$(dirname "$0")/.." && pwd)

if [ -z "$src" ]; then
  command -v go >/dev/null 2>&1 || { echo "install.sh: Go is needed to build; or pass a built binary" >&2; exit 1; }
  build=$(mktemp -d)
  trap 'rm -rf "$build"' EXIT
  version=$(git -C "$here" describe --tags --match 'cli-v*' 2>/dev/null | sed 's/^cli-v//' || true)
  [ -n "$version" ] || version="0.1.0-dev+$(git -C "$here" rev-parse --short HEAD 2>/dev/null || echo local)"
  (cd "$here" && CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=$version" -o "$build/daedalus-cli" ./cmd/daedalus-cli)
  src="$build/daedalus-cli"
fi

mkdir -p "$dest"
install -m 0755 "$src" "$dest/daedalus-cli"
ln -sf daedalus-cli "$dest/dae"
echo "installed $dest/daedalus-cli and $dest/dae"
case ":$PATH:" in
  *":$dest:"*) ;;
  *) echo "note: $dest is not on PATH" ;;
esac
