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
# own, so a runner with something on the usual ones does not decide the outcome — and the app's is
# held by another program for the whole run, so the start has to move off it (desktop/ports.go) and
# the app must answer wherever the launcher wrote it went, with that program left alone.
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

# Not Daedalus: a plain HTTP server on the port the env file names for the app.
mkdir -p "$root/foreign"
python3 -m http.server --bind 127.0.0.1 --directory "$root/foreign" "$api" >"$root/foreign.log" 2>&1 &
foreign=$!
trap 'kill "$foreign" 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -fsS -o /dev/null "http://127.0.0.1:$api/" 2>/dev/null && break
  sleep 0.2
done
curl -fsS -o /dev/null "http://127.0.0.1:$api/" || fail "the other program never listened on $api"

# The launcher quits when its standard input ends, as it does when the application goes away: a
# pipe from a sleep is that input, and ending the sleep is closing the window.
export DAEDALUS_LOCAL_ROOT="$root/local" DAEDALUS_UPDATE_CHECK=off
start=$(date +%s)
( sleep 1800 & echo $! >"$root/sleep.pid"; wait ) | "$launcher" --shell --data "$data" --port "$port" --mode native start >"$root/events.txt" 2>&1 &
launcher_pid=$!
ready=""
stage=""
for _ in $(seq 1 600); do
  now=$(curl -fsS "http://127.0.0.1:$port/api/status" 2>/dev/null | grep -o '"stage":"[a-z]*"' | head -1 || true)
  if [ -n "$now" ] && [ "$now" != "$stage" ]; then
    echo "$(( $(date +%s) - start ))s $now"
    stage=$now
  fi
  moved=$(sed -n 's/^API_PORT=//p' "$data/.env" | tail -1)
  if [ -n "$moved" ] && [ "$moved" != "$api" ] && curl -fsS -o /dev/null "http://127.0.0.1:$moved/app/" 2>/dev/null; then
    ready=$(( $(date +%s) - start ))
    break
  fi
  sleep 1
done
kill "$(cat "$root/sleep.pid")" 2>/dev/null || true
# The launcher stops the agent before it exits; waiting for it is waiting for that. Only for it: a
# bare wait would wait for the other program too, which runs until this script ends.
wait "$launcher_pid" || true

log=$(find "$root/local" -name launcher.log | head -1)
[ -n "$log" ] || fail "no launcher.log under $root/local"
[ -n "$ready" ] || { tail -50 "$log"; fail "the app never answered"; }
echo "the app answered after ${ready}s on port $moved, moved off $api"
kill -0 "$foreign" 2>/dev/null || fail "the program that held $api was stopped"
curl -fsS -o /dev/null "http://127.0.0.1:$api/" || fail "the program that held $api no longer answers"

expect() { grep -q "$1" "$log" || { tail -60 "$log"; fail "the log never says: $1"; }; }
refuse() { if grep -q "$1" "$log"; then grep "$1" "$log" | head -5; fail "the first run did this from the network: $1"; fi; }
expect "uv .* comes with the installation"
expect "rg .* comes with the installation"
expect "daedalus comes with the installation"
expect "protocore-exp comes with the installation"
expect "installing python .* from the copy that came with the installation"
expect "building the environment from the packages that came with the installation"
expect "the app's port $api is taken by another program; it moves to $moved"
refuse "downloading "
refuse "fetching "
refuse "did not take python"
refuse "were not enough"
refuse "not usable"
echo "everything came from the installation"
