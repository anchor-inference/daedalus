# browserd

The browser daemon. It owns Chromium on the agent's profiles — one browser per profile, started on
the first request and closed when idle — and keeps, for each agent owner, a group of tabs in it with
who controls them. It streams live views of its tabs as screencast frames with per-viewer flow
control, and takes a person's input while they drive. The Daedalus host reaches it over an
authenticated local socket, with the framing, handshake and event log of `ptyd`, whose shared
packages (`ptyd/proto`) this module builds against; the daemon knows nothing about sessions,
projects or policies.

```sh
go build ./cmd/browserd
./browserd serve --env container --run-dir /tmp/browserd-run --state-dir /tmp/browserd-state
./browserd version
```

With `--backend attach` it launches no browser and drives the operator's own Chrome through the
Daedalus extension instead, which reaches it over a relay: `browserd bridge`, Chrome's native
messaging host, on the same machine, or the host's WebSocket on a server.

The protocol, the view frames, the Chromium it runs and the numbers measured against it are
specified in [`docs/architecture/browser.md`](../docs/architecture/browser.md).

## Layout

| Package | What it is |
|---|---|
| `cmd/browserd` | flags, signals, shutdown |
| `internal/config` | the flags, the JSON file, and every default as a named constant |
| `internal/cdp` | the DevTools protocol over Chromium's pipe: calls, ordered sends, events |
| `internal/chrome` | finding a Chromium, its switches and preferences, starting it on a profile, ending its tree; its private memory and sandbox state from `/proc` |
| `internal/display` | the virtual X display (Xvfb) a headed browser draws on where the daemon has no display |
| `internal/human` | plans of a hand's input: the pointer's eased, jittered travel, the pauses, the wheel's notches, the delays between keys, each within a budget |
| `internal/browser` | browsers, groups, tabs and control; following Chromium's targets; navigation; idle close; statistics and the memory limit. What it does above a page goes through a `Backend`: `launch` (Chromium of its own) or `attach` (the operator's Chrome over a relay) |
| `internal/relay` | the relay to the Daedalus extension: its frames (`testdata/frames.json`, shared with the extension's tests), the one relay a daemon has and its liveness, the allowlist, and the DevTools connection over it; `relaytest` is a fake of the extension fronting a real Chromium |
| `internal/bridge` | `browserd bridge`: Chrome's native messaging on stdin and stdout, the extension's origin, and the user daemon's run directory |
| `internal/view` | live views: one screencast per watched tab, its pacing, each client's mailbox and acknowledgements, thumbnails, a person's input |
| `internal/page` | what the agent reads and does on a page: the outline with refs, the readable text and the search from `js/page.js` (run in an isolated world the page cannot reach, and in one of each frame of another site), masked screenshots, actions as real input, waiting, dialogs, downloads and uploads, and the moments the operator is needed |
| `internal/sensitive` | which consequences of an action the operator approves: the word lists in English and Russian and the classification |
| `internal/rpc` | the methods; `testdata/site` is the local site the tests browse, `testdata/golden` the outlines of five of its pages, one of them in two ways (`go test ./internal/rpc -run Golden -update` rewrites them); `BROWSERD_MEASURE=1 go test ./internal/rpc -run Measure -v` times the snapshot on them, on long synthetic pages and on pages saved in `BROWSERD_MEASURE_DIR` |
| `internal/wire` | the view frames; `testdata/frames.json` is shared byte for byte with the app |

## Testing

The gate runs in the Go image named by `go.mod` plus a Chromium (`toolchain.Dockerfile`), as a
non-root user with the seccomp profile Chromium's sandbox needs (the one the compose service gets),
with the race detector and vets for macOS and Windows. The tests drive real Chromium and browse only a
local site; `BROWSERD_REQUIRE_CHROMIUM` makes a missing browser a failure rather than
a skip. The container is capped, as every Chromium run on a shared machine should be:

```sh
docker build -q -t browserd-toolchain -f toolchain.Dockerfile .
docker run --rm --memory 6g --memory-swap 6g --security-opt seccomp=unconfined --user "$(id -u):$(id -g)" \
  -v "$PWD/..":/src -w /src/browserd -e HOME=/tmp -e GOCACHE=/src/browserd/.cache/go-build \
  -e GOMODCACHE=/src/browserd/.cache/go-mod -e GOFLAGS=-buildvcs=false -e BROWSERD_REQUIRE_CHROMIUM=1 \
  browserd-toolchain sh -c 'test -z "$(gofmt -l cmd internal)" && go vet ./... && go test -race ./... &&
    BROWSERD_BACKEND=attach go test -race ./internal/rpc &&
    GOOS=darwin go vet ./... && GOOS=windows go vet ./... && GOOS=windows GOARCH=arm64 go vet ./...'
```

`BROWSERD_BACKEND=attach` runs the rpc suite a second time against the attach backend: the daemon
drives a fake of the extension (`internal/relay/relaytest`) that fronts the same Chromium and gives
the daemon only what the extension gives it — a debuggee per tab, flat child sessions under it, and
the browser-level operations as calls — with the relay's allowlist enforced. A test of what only a
browser of the daemon's own does (the browser cap, throwaway contexts, cookie import, idle close,
the sandbox, the wall's per-browser proxy, the forced screen-position script) skips there and says
why.

The same tests run headed, with windows on a virtual display the daemon starts, as the container
runs them: add `-e BROWSERD_TEST_MODE=headed` (the image has Xvfb).

The image's Chromium is Debian's. To run the same tests against the build a release pins, mount
Playwright's `chromium-<revision>` directory and name it: `-v <dir>:/opt/pw:ro -e
BROWSERD_CHROMIUM=/opt/pw/chrome-linux64/chrome`. Both must pass; they differ in ways that matter
(the screencast of the pinned build shows the window, not an emulated viewport).

The Windows half — the debugging pipe on inherited handles (`--remote-debugging-io-pipes`) — is
compiled by the vet but has not run on a Windows machine.
