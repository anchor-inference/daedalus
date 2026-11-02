package main

import (
	"os"
	"testing"

	"github.com/ascorblack/daedalus/cli/internal/tether"
)

// TestMain lets this test binary be the watchdog the commands under test start (tether.Start runs
// the program it is in again), as main does; without it the watchdog would run every test again.
func TestMain(m *testing.M) {
	if tether.Child() {
		os.Exit(tether.Watch(os.Stdin))
	}
	os.Exit(m.Run())
}
