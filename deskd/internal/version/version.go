// Package version names the build and the wire protocol it speaks.
package version

// Version is the build's own version. Release builds set it with
// `-ldflags "-X github.com/ascorblack/daedalus/deskd/internal/version.Version=<v>"`; a plain
// `go build` reports "dev".
var Version = "dev"

// Protocol is the version of the socket protocol. It changes only when an existing method or event
// changes meaning; additions keep it. The host refuses a protocol it does not know: a desktop it half
// understands is worse than one reported as unavailable.
const Protocol = 1
