// Package tether makes the commands daedalus-cli runs die with it. A client killed outright
// (`kill -9`, the out-of-memory killer, a closed laptop lid that never woke) used to leave the
// commands it had started running on: nobody read their output, nothing stopped them, and the
// server, which reports the outcome of a call lost with its client as unknown, could not even say
// whether they were still going.
//
// Three mechanisms, one per kind of system:
//
//   - Linux: the kernel's parent-death signal kills each command's process-group leader the moment
//     the client dies (sidechan sets it on a tied runner), and the watchdog below ends the rest of
//     the group.
//   - macOS and the other Unixes: the watchdog alone. It is a second copy of this program holding
//     the read end of a pipe whose only writer is the client; whatever kills the client closes that
//     pipe, and the end of input is the watchdog's signal to end every group still registered.
//     Watching a pipe needs nothing from the system but a working kernel, unlike polling the parent
//     pid, which a reused pid fools, or kqueue, which would mean cgo or a second implementation.
//   - Windows: each command's job object, which kills its tree when the last handle closes; the
//     client's handle closes when it dies. Start returns nil there and nothing else is needed.
//
// The server still reports a call lost with its client as unknown: a command the client started a
// moment before dying may have finished, or done half its work, before it was ended.
package tether

import (
	"os"
)

// envChild marks the process the client starts as its watchdog. Only main and the tests that start
// a tether look at it; any other program started with it set would not know what it means.
const envChild = "DAEDALUS_CLI_TETHER"

// Child says whether this process was started as a client's watchdog, in which case the caller runs
// Watch and exits with its result instead of doing anything else.
func Child() bool { return os.Getenv(envChild) == "1" }
