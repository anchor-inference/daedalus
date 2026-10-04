#!/bin/sh
# This service alone owns the Docker socket; model tools can never reach it.
set -u
# Compose resolves sibling env files even with --no-deps. This sidecar replaces
# only daedalus, so it must neither read proxy credentials nor recreate the proxy.
export DAEDALUS_SECRETS_FILE=/dev/null
compose() {
    if [ "${DAEDALUS_ENCRYPTED_STATE_REQUIRED:-0}" = 1 ]; then
        # Without this overlay a replacement would select the old named volume. Both consumers
        # still refuse that volume through their entrypoint guard if another caller omits it.
        docker compose -f "$COMPOSE_FILE" -f "$(dirname "$COMPOSE_FILE")/compose.encrypted-state.yaml" "$@"
    else
        docker compose -f "$COMPOSE_FILE" "$@"
    fi
}
trigger=${DAEDALUS_REBUILD_TRIGGER_DIR:-/run/daedalus-rebuild}
# A build can take minutes. Availability must not expire while the worker is busy.
heartbeat() {
    while true; do
        touch "$trigger/alive" "$trigger/dependencies-alive"
        sleep 5
    done
}
heartbeat &
heartbeat_pid=$!
trap 'kill "$heartbeat_pid" 2>/dev/null || true' EXIT
stage() {
    printf '{"stage":"%s","at":%s,"restart_at":%s}\n' "$1" "$(date +%s)" "${2:-0}" > "$trigger/dependencies-$job.progress.pending"
    mv "$trigger/dependencies-$job.progress.pending" "$trigger/dependencies-$job.progress"
}
# A sidecar crash does not erase the accepted request. Rebuilding the same recipe is safe;
# replacing a container twice is preferable to reporting success for an abandoned build.
if [ -f "$trigger/dependencies-running" ] && [ ! -f "$trigger/dependencies-request" ]; then
    mv "$trigger/dependencies-running" "$trigger/dependencies-request"
fi
while true; do
    touch "$trigger/alive" "$trigger/dependencies-alive"
    if [ -f "$trigger/dependencies-request" ]; then
        job=$(cat "$trigger/dependencies-request")
        case "$job" in
            ''|*[!a-f0-9]*) rm -f "$trigger/dependencies-request"; continue ;;
        esac
        if [ "${#job}" -ne 32 ]; then
            rm -f "$trigger/dependencies-request"
            continue
        fi
        mv "$trigger/dependencies-request" "$trigger/dependencies-running"
        build_log="$trigger/dependencies-$job.log"
        stage building
        result=failed
        if compose --progress plain --env-file .env build daedalus > "$build_log" 2>&1; then
            stage restart_pending "$(($(date +%s) + 30))"
            # Every signed-in page polls the notice. Keep serving it before replacing the app.
            sleep 30
            stage restarting
            if compose --env-file .env up -d --no-build --no-deps daedalus >> "$build_log" 2>&1; then
                result=completed
            else
                result='container replacement failed; inspect rebuilder logs'
            fi
        else
            result='image build failed; the running container was not replaced; inspect rebuilder logs'
        fi
        cat "$build_log"
        printf '%s\n' "$result" > "$trigger/dependencies-$job.pending"
        mv "$trigger/dependencies-$job.pending" "$trigger/dependencies-$job.result"
        rm -f "$trigger/dependencies-running"
    elif [ -f "$trigger/terminals-request" ]; then
        # The operator's update of the terminal daemon: the terminals service is recreated from the
        # image the agent already runs, which ends every container terminal. The app asked the
        # operator, with the count, before it wrote the request; nothing here builds anything.
        job=$(cat "$trigger/terminals-request")
        rm -f "$trigger/terminals-request"
        case "$job" in
            ''|*[!a-f0-9]*) continue ;;
        esac
        if [ "${#job}" -ne 32 ]; then
            continue
        fi
        if compose --env-file .env up -d --no-build --no-deps terminals > "$trigger/terminals-$job.log" 2>&1; then
            result=completed
        else
            result='the terminals service was not recreated; inspect rebuilder logs'
        fi
        cat "$trigger/terminals-$job.log"
        printf '%s\n' "$result" > "$trigger/terminals-$job.pending"
        mv "$trigger/terminals-$job.pending" "$trigger/terminals-$job.result"
    elif [ -f "$trigger/browser-request" ]; then
        # The operator's update of the browser daemon: the browser service is recreated, which ends
        # every browser (their profiles stay on their volume). Unlike the terminals service it is
        # built first: it runs the image's `browser` target, which the agent's own rebuild never
        # builds, so without this the service would come back as the build it already was. The
        # layers it shares with the agent's image are already there; only the difference builds.
        job=$(cat "$trigger/browser-request")
        rm -f "$trigger/browser-request"
        case "$job" in
            ''|*[!a-f0-9]*) continue ;;
        esac
        if [ "${#job}" -ne 32 ]; then
            continue
        fi
        if ! compose --progress plain --env-file .env build browser > "$trigger/browser-$job.log" 2>&1; then
            result='the browser image was not built; the running browser service was left as it was; inspect rebuilder logs'
        elif compose --env-file .env up -d --no-build --no-deps browser >> "$trigger/browser-$job.log" 2>&1; then
            result=completed
        else
            result='the browser service was not recreated; inspect rebuilder logs'
        fi
        cat "$trigger/browser-$job.log"
        printf '%s\n' "$result" > "$trigger/browser-$job.pending"
        mv "$trigger/browser-$job.pending" "$trigger/browser-$job.result"
    elif [ -f "$trigger/rebuild" ]; then
        rm -f "$trigger/rebuild"
        compose --env-file .env up -d --build --no-deps daedalus || echo 'rebuild failed'
    fi
    sleep 5
done
