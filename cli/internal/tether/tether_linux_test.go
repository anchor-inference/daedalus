package tether

import (
	"syscall"
	"testing"
	"time"
)

// On Linux the kernel ends a tied command's leader by itself, with no watchdog at all: the case of a
// watchdog that could not start, or was killed together with its client.
func TestOnLinuxTheLeaderDiesWithItsClientWithoutAWatchdog(t *testing.T) {
	client, pids := startClient(t, "leader-only", "echo $$ > leader; exec sleep 300", "leader")
	if err := client.Process.Signal(syscall.SIGKILL); err != nil {
		t.Fatal(err)
	}
	waitGone(t, "the command", pids[0], 5*time.Second)
}
