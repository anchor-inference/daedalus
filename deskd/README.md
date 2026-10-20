# deskd

The desktop daemon of the native application. It captures the operator's screens, lists the windows
and the apps behind them, and sends synthetic mouse and keyboard input for the Daedalus host, within a
lease only the operator can grant, over an authenticated local socket. The framing, handshake and
event log are `ptyd`'s (`ptyd/proto`), which this module builds against. The daemon refuses input to
Daedalus's own windows, on a locked screen and outside the lease's scope, pauses the moment the
operator touches the machine, and keeps an audit of metadata only.

```sh
go build ./cmd/deskd
./deskd serve --run-dir /tmp/deskd-run --state-dir /tmp/deskd-state
./deskd serve --backend fake --run-dir /tmp/deskd-run --state-dir /tmp/deskd-state   # a desktop in memory
./deskd selftest
```

The protocol, the coordinates, the roles and the rules are specified in
[`docs/architecture/computer.md`](../docs/architecture/computer.md).

## Layout

| Package | What it is |
|---|---|
| `cmd/deskd` | flags, signals, shutdown, the selftest command |
| `internal/geom` | the arithmetic between screenshot pixels and the desktop, pure, with the traps in its table |
| `internal/keys` | key names in every dialect, parsed to one chord; how text is cut up before it is typed |
| `internal/shot` | the downscale and the encoding of a capture |
| `internal/backend` | what a system's desktop offers; `x11`, `win` and `quartz` are the systems, `fake` one in memory |
| `internal/native` | the backend of the system the binary was built for |
| `internal/desk` | the rules: the lease and its scope, screenshot ids, the operator's hand, the stop, the limits, the protected apps |
| `internal/audit` | the metadata-only audit |
| `internal/rpc` | the methods and the roles |
| `internal/selftest` | `deskd selftest`: the daemon drives a window of its own and nothing else |

## Testing

The gate runs in the Go image named by `go.mod` plus a virtual X display and xdotool
(`toolchain.Dockerfile`). The X11 tests start their own Xvfb whatever display the machine has, open
their own window and drive nothing else; xdotool's input stands in for the operator's hand.
`DESKD_REQUIRE_X11` makes a missing Xvfb a failure rather than a skip. The container is capped:

```sh
docker build -q -t deskd-toolchain -f toolchain.Dockerfile .
docker run --rm --memory 4g --memory-swap 4g --user "$(id -u):$(id -g)" \
  -v "$PWD/..":/src -w /src/deskd -e HOME=/tmp -e GOCACHE=/src/deskd/.cache/go-build \
  -e GOMODCACHE=/src/deskd/.cache/go-mod -e GOFLAGS=-buildvcs=false -e DESKD_REQUIRE_X11=1 \
  deskd-toolchain sh -c 'test -z "$(gofmt -l cmd internal)" && go vet ./... && go test -race ./... &&
    GOOS=windows go vet ./... && GOOS=windows GOARCH=arm64 go vet ./... &&
    GOOS=darwin go vet ./... && GOOS=darwin GOARCH=arm64 go vet ./...'
```

The Windows backend is compiled by the vet but has not run on a Windows machine. The macOS backend
needs cgo and the macOS SDK: the vet here covers its pure helpers and the build without cgo, and its
Objective-C has not been compiled; build it on a Mac with `CGO_ENABLED=1 go build ./cmd/deskd`, then
run `deskd selftest` there.
