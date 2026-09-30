#!/usr/bin/env bash
# Local sites for the container's browser: forwards what the browser's container sends to one of this
# server's own addresses to the server's loopback, so a dev server that listens on 127.0.0.1 only
# (vite, next, most of them by default) opens in the agent's browser as http://localhost:<port>.
#
# Without it the browser reaches the Docker host only on ports bound to every interface. With it, the
# network wall inside the browser decides alone: [browser] local_sites in the settings says whether
# such a port is refused, asked about or open, and the installation's own ports (the API, the key
# proxy, the daemons) are refused whatever it says.
#
# Only the browser's own network is forwarded: a DNAT rule on its bridge, and route_localnet on that
# bridge alone (the kernel otherwise drops a packet from outside addressed to 127.0.0.1). Nothing
# else on the server, and no other container, gains a way to its loopback.
#
#   sudo bash deploy/browser-host-loopback.sh apply     add the rule (again: nothing changes)
#   sudo bash deploy/browser-host-loopback.sh remove    take it away
#   sudo bash deploy/browser-host-loopback.sh install   a systemd timer that applies it every minute,
#                                                       since the bridge is made anew with the network
#   sudo bash deploy/browser-host-loopback.sh uninstall remove the timer and the rule
#   bash deploy/browser-host-loopback.sh status         the bridge, and whether the rule is there
#
# DAEDALUS_BROWSER_NETWORK names the browser's compose network (default deploy_browser).
set -euo pipefail
NETWORK="${DAEDALUS_BROWSER_NETWORK:-deploy_browser}"
TAG="daedalus-browser-loopback"
UNIT="daedalus-browser-loopback"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

fail() { printf 'browser loopback: %s\n' "$*" >&2; exit 1; }

bridge() {
  local id name
  id="$(docker network inspect -f '{{.Id}}' "$NETWORK" 2>/dev/null)" || return 1
  name="$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' "$NETWORK" 2>/dev/null || true)"
  if [ -n "$name" ] && [ "$name" != "<no value>" ]; then printf '%s\n' "$name"; else printf 'br-%s\n' "${id:0:12}"; fi
}

rule() { printf '%s\n' -i "$1" -p tcp -m addrtype --dst-type LOCAL -m comment --comment "$TAG" -j DNAT --to-destination 127.0.0.1; }

# Every rule this script ever added, on whichever bridge it was then: a network made anew has a new
# bridge, and the old rule would otherwise stay behind naming an interface that is gone.
stale() { iptables -t nat -S PREROUTING | grep -- "--comment $TAG" | sed 's/^-A /-D /' || true; }

apply() {
  local br
  br="$(bridge)" || { echo "no network $NETWORK yet: nothing to forward"; return 0; }
  [ -d "/proc/sys/net/ipv4/conf/$br" ] || { echo "no bridge $br yet: nothing to forward"; return 0; }
  while read -r old; do
    [ -n "$old" ] || continue
    case "$old" in *"-i $br "*) continue ;; esac
    eval "iptables -t nat $old"
  done < <(stale)
  sysctl -q -w "net.ipv4.conf.$br.route_localnet=1"
  mapfile -t args < <(rule "$br")
  iptables -t nat -C PREROUTING "${args[@]}" 2>/dev/null || iptables -t nat -I PREROUTING 1 "${args[@]}"
}

remove() {
  while read -r old; do
    [ -n "$old" ] && eval "iptables -t nat $old"
  done < <(stale)
  local br
  if br="$(bridge)" && [ -d "/proc/sys/net/ipv4/conf/$br" ]; then
    sysctl -q -w "net.ipv4.conf.$br.route_localnet=0"
  fi
}

case "${1:-status}" in
  apply) [ "$(id -u)" = 0 ] || fail "run it with sudo"; apply ;;
  remove) [ "$(id -u)" = 0 ] || fail "run it with sudo"; remove ;;
  install)
    [ "$(id -u)" = 0 ] || fail "run it with sudo"
    cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=Forward the Daedalus browser's local sites to this server's loopback
After=docker.service
[Service]
Type=oneshot
Environment=DAEDALUS_BROWSER_NETWORK=$NETWORK
ExecStart=/usr/bin/env bash $SELF apply
EOF
    cat > "/etc/systemd/system/$UNIT.timer" <<EOF
[Unit]
Description=Keep the Daedalus browser's local sites forwarded
[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$UNIT.timer"
    systemctl start "$UNIT.service"
    ;;
  uninstall)
    [ "$(id -u)" = 0 ] || fail "run it with sudo"
    systemctl disable --now "$UNIT.timer" 2>/dev/null || true
    rm -f "/etc/systemd/system/$UNIT.service" "/etc/systemd/system/$UNIT.timer"
    systemctl daemon-reload
    remove
    ;;
  status)
    br="$(bridge)" || fail "no network $NETWORK"
    echo "bridge $br, route_localnet $(cat "/proc/sys/net/ipv4/conf/$br/route_localnet" 2>/dev/null || echo '?')"
    if sudo -n iptables -t nat -S PREROUTING 2>/dev/null | grep -q -- "-i $br .*--comment $TAG"; then echo "forwarded"; else echo "not forwarded"; fi
    ;;
  *) fail "say apply, remove, install, uninstall or status" ;;
esac
