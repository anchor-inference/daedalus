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

The protocol, the view frames, the Chromium it runs and the numbers measured against it are
specified in [`docs/architecture/browser.md`](../docs/architecture/browser.md).

## Layout

| Package | What it is |
|---|---|
| `cmd/browserd` | flags, signals, shutdown |
| `internal/config` | the flags, the JSON file, and every default as a named constant |
| `internal/cdp` | the DevTools protocol over Chromium's pipe: calls, ordered sends, events |
| `internal/chrome` | finding a Chromium, its switches and preferences, starting it on a profile, ending its tree; its private memory and sandbox state from `/proc` |
| `internal/browser` | browsers, groups, tabs and control; following Chromium's targets; navigation; idle close; statistics and the memory limit |
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
    GOOS=darwin go vet ./... && GOOS=windows go vet ./... && GOOS=windows GOARCH=arm64 go vet ./...'
```

The image's Chromium is Debian's. To run the same tests against the build a release pins, mount
Playwright's `chromium-<revision>` directory and name it: `-v <dir>:/opt/pw:ro -e
BROWSERD_CHROMIUM=/opt/pw/chrome-linux64/chrome`. Both must pass; they differ in ways that matter
(the screencast of the pinned build shows the window, not an emulated viewport).

The Windows half — the debugging pipe on inherited handles (`--remote-debugging-io-pipes`) — is
compiled by the vet but has not run on a Windows machine.
