#!/usr/bin/env bash
# A first run from the seed the package carries (desktop/seed.go), on a throwaway Linux or Mac (the
# release workflow's runner; Windows has firstrun-smoke.ps1): the launcher, started the way the application starts it, brings a
# native installation up in a folder of its own, and the run must take everything — the code, uv,
# ripgrep, the interpreter, every package — from the installation rather than from the network.
#
#   firstrun-smoke.sh LAUNCHER ROOT
#
# ROOT holds the data, the runtime and the state, and nothing outside it is written: uv is told not
# to put python on PATH, and the update check is off. The app's port and the launcher's are their
# own, so a runner with something on the usual ones does not decide the outcome.
set -euo pipefail

fail() { echo "firstrun-smoke: $*" >&2; exit 1; }

launcher=$1
root=$2
data="$root/data"
port=18770
api=18765
mkdir -p "$data/daedalus-secrets" "$root/local"
printf native >"$data/mode"
printf en >"$data/lang"
cat >"$data/.env" <<ENV
API_PORT=$api
KEYPROXY_PORT=13201
DAEDALUS_SUPERVISOR_PORT=18769
SERVICES_PORT_RANGE=18100-18119
SERVICES_PUBLIC_HOST=127.0.0.1
USD_PER_DAY=20
ENV
echo "KEYPROXY_USD_PER_DAY=20" >"$data/daedalus-secrets/keyproxy.env"

# The launcher quits when its standard input ends, as it does when the application goes away: a
# pipe from a sleep is that input, and ending the sleep is closing the window.
export DAEDALUS_LOCAL_ROOT="$root/local" DAEDALUS_UPDATE_CHECK=off
start=$(date +%s)
( sleep 1800 & echo $! >"$root/sleep.pid"; wait ) | "$launcher" --shell --data "$data" --port "$port" --mode native start >"$root/events.txt" 2>&1 &
ready=""
stage=""
for _ in $(seq 1 600); do
  now=$(curl -fsS "http://127.0.0.1:$port/api/status" 2>/dev/null | grep -o '"stage":"[a-z]*"' | head -1 || true)
  if [ -n "$now" ] && [ "$now" != "$stage" ]; then
    echo "$(( $(date +%s) - start ))s $now"
    stage=$now
  fi
  if curl -fsS -o /dev/null "http://127.0.0.1:$api/app/" 2>/dev/null; then
    ready=$(( $(date +%s) - start ))
    break
  fi
  sleep 1
done
kill "$(cat "$root/sleep.pid")" 2>/dev/null || true
# The launcher stops the agent before it exits; waiting for it is waiting for that.
wait || true

log=$(find "$root/local" -name launcher.log | head -1)
[ -n "$log" ] || fail "no launcher.log under $root/local"
[ -n "$ready" ] || { tail -50 "$log"; fail "the app never answered"; }
echo "the app answered after ${ready}s"

expect() { grep -q "$1" "$log" || { tail -60 "$log"; fail "the log never says: $1"; }; }
refuse() { if grep -q "$1" "$log"; then grep "$1" "$log" | head -5; fail "the first run did this from the network: $1"; fi; }
expect "uv .* comes with the installation"
expect "rg .* comes with the installation"
expect "daedalus comes with the installation"
expect "protocore-exp comes with the installation"
expect "installing python .* from the copy that came with the installation"
expect "building the environment from the packages that came with the installation"
refuse "downloading "
refuse "fetching "
refuse "did not take python"
refuse "were not enough"
refuse "not usable"
echo "everything came from the installation"
