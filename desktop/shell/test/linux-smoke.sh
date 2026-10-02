#!/bin/sh
# The .deb on a throwaway machine (a container here, the CI runner in the workflow), in three steps:
#
#   linux-smoke.sh install DEB   as root: install it, check what the desktop will show
#   linux-smoke.sh run           as the user: start the application under a virtual display until
#                                its window shows the launcher's own page, then close it
#   linux-smoke.sh remove        as root: remove the package; the user's data must stay
#
# Nothing here touches anything but the package's own files and the user's Daedalus folders.
set -eu

fail() { echo "linux-smoke: $*" >&2; exit 1; }

case "${1:-}" in
install)
  deb=$2
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq "$deb" xvfb curl >/dev/null
  test -x /opt/Daedalus/daedalus || fail "no /opt/Daedalus/daedalus"
  test -x /opt/Daedalus/daedalus-desktop || fail "no launcher beside the application"
  test -f /opt/Daedalus/miniapp-dist/index.html || fail "no Mini App beside the launcher"
  /opt/Daedalus/daedalus-desktop --version
  entry=/usr/share/applications/daedalus.desktop
  grep -q '^Exec=/opt/Daedalus/daedalus %U$' "$entry" || fail "the menu entry does not start the application: $(cat "$entry")"
  grep -q '^MimeType=x-scheme-handler/daedalus;$' "$entry" || fail "daedalus:// is not registered"
  grep -q '^Terminal=false$' "$entry" || fail "the menu entry opens a terminal"
  ls /usr/share/icons/hicolor/*/apps/daedalus.png >/dev/null || fail "no icon"
  test "$(readlink -f /usr/bin/daedalus)" = /opt/Daedalus/daedalus || fail "daedalus is not on PATH"
  echo "installed: $(dpkg-query -W -f '${Package} ${Version}' daedalus)"
  ;;
run)
  # A display of its own, and the sandbox off only where a container forbids it (CONTAINER=1): the
  # runner keeps it, through the AppArmor profile the package installs.
  extra=""
  [ "${CONTAINER:-}" = 1 ] && extra="--no-sandbox"
  log=$(mktemp)
  # shellcheck disable=SC2086 # $extra is one flag or none
  DAEDALUS_UPDATE_CHECK=off xvfb-run -a /opt/Daedalus/daedalus $extra --remote-debugging-port=9333 >"$log" 2>&1 &
  pid=$!
  data="${XDG_DATA_HOME:-$HOME/.local/share}/daedalus/data"
  page=""
  for _ in $(seq 1 90); do
    # What the window shows, asked of its own debugging endpoint: the launcher's page is a loopback
    # address served by the launcher this application started.
    page=$(curl -fsS http://127.0.0.1:9333/json/list 2>/dev/null | grep -o '"url": *"http://127.0.0.1:[0-9]*/[^"]*"' | head -1 || true)
    [ -n "$page" ] && break
    sleep 1
  done
  [ -n "$page" ] || { cat "$log"; fail "the window never showed the launcher's page"; }
  echo "the window shows $page"
  url=$(echo "$page" | sed 's/.*"\(http[^"]*\)"/\1/')
  # The address is read while the window may still be on its way: the launcher answers / with a
  # redirect to the page the installation is at (/setup on a first start), and the window follows it.
  # So does this check — without -L it read the redirect's own two-line body and failed a window that
  # was fine.
  landed=$(curl -fsSL -o /dev/null -w '%{url_effective}' "$url") || fail "$url does not answer"
  curl -fsSL "$url" | grep -q 'data-page="\(setup\|progress\|status\)"' || fail "$landed is not one of the launcher's pages"
  echo "the launcher answers it with $landed"
  case "$landed" in
  */setup)
    # The wizard is served whole from the launcher — the stage, three.js, the fonts, the still for a
    # machine without WebGL — and its form answers with its own token: a post it refuses (a limit of
    # zero) proves the path without starting a second first run here.
    base=${landed%/setup}
    for asset in setup/boot.js setup/wizard.js setup/scene.js setup/vendor/three.module.min.js setup/fonts/geist-latin.woff2 setup/still.webp; do
      curl -fsS -o /dev/null "$base/assets/$asset" || fail "the setup page's $asset is not served"
    done
    token=$(curl -fsS "$landed" | sed -n 's/.*<meta name="csrf" content="\([0-9a-f]*\)".*/\1/p' | head -1)
    [ -n "$token" ] || fail "the setup page carries no token"
    refused=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-Daedalus-Desktop: $token" --data "csrf=$token&usd_per_day=0" "$landed")
    [ "$refused" = 400 ] || fail "the setup form answered $refused to a limit of zero, not 400"
    echo "the wizard's assets are served and its form checks what it is sent"
    ;;
  esac
  test -f "$data/launcher.json" || fail "no launcher.json in $data: the data is not in the per-user folder"
  ps -eo pid,args | grep -v grep | grep -q '/opt/Daedalus/daedalus-desktop --shell' || fail "the launcher is not running under the application"
  # Closed the way a desktop closes it: SIGTERM to the application's main process.
  app=$(ps -eo pid,args | awk '$2 == "/opt/Daedalus/daedalus" && $0 !~ /--type=/ { print $1; exit }')
  [ -n "$app" ] || fail "the application's process is not there"
  kill -TERM "$app"
  for _ in $(seq 1 30); do
    ps -eo args | grep -v grep | grep -q 'daedalus-desktop --shell' || break
    sleep 1
  done
  ps -eo args | grep -v grep | grep -q 'daedalus-desktop --shell' && fail "the launcher outlived the application"
  echo "closed; the launcher stopped with it"
  ;;
remove)
  user_data=$2
  apt-get remove -y -qq daedalus >/dev/null
  test ! -e /opt/Daedalus/daedalus || fail "the application is still there"
  test ! -e /usr/share/applications/daedalus.desktop || fail "the menu entry is still there"
  test -d "$user_data" || fail "removing the package took the data with it"
  echo "removed; $user_data stays"
  ;;
*)
  fail "usage: linux-smoke.sh install DEB | run | remove DATA"
  ;;
esac
