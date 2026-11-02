package ax

import "time"

// What the tests of package ax_test reach inside the package. They live outside it because they run
// the fake tool, which imports this package.
var (
	EnvelopeSupported = envelopeSupported
	IsHelperOf        = isHelperOf
)

// SetKillGrace shortens the wait between a call's deadline and the kill, and returns the old one.
func SetKillGrace(d time.Duration) time.Duration {
	old := killGrace
	killGrace = d
	return old
}
