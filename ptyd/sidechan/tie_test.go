//go:build unix

package sidechan

import (
	"context"
	"os"
	"strconv"
	"sync"
	"syscall"
	"testing"
)

// recordingTether keeps what a runner told it, in order.
type recordingTether struct {
	mu     sync.Mutex
	events []string
	pid    int
	pgid   int
}

func (r *recordingTether) Hold(pid int) func() {
	pgid, _ := syscall.Getpgid(pid)
	r.mu.Lock()
	r.events = append(r.events, "hold")
	r.pid, r.pgid = pid, pgid
	r.mu.Unlock()
	return func() {
		r.mu.Lock()
		r.events = append(r.events, "release")
		r.mu.Unlock()
	}
}

// A tied runner names each program's group to its tether while the program runs, and lets it go
// once the program has ended: the group the tether ends when the runner dies is the program's own,
// and a finished program is never ended again under a pid that may since belong to another.
func TestATiedRunnerHoldsEachProgramsGroupWhileItRuns(t *testing.T) {
	e := NewOpenExec(os.Environ(), t.TempDir(), quietLog())
	tether := &recordingTether{}
	e.Tie(tether)
	res, err := e.Run(context.Background(), ExecRequest{Argv: []string{"sh", "-c", "echo $$"}, Cwd: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	tether.mu.Lock()
	defer tether.mu.Unlock()
	if len(tether.events) != 2 || tether.events[0] != "hold" || tether.events[1] != "release" {
		t.Fatalf("tether saw %v, want one hold and then one release", tether.events)
	}
	if got := res.Stdout; got != strconv.Itoa(tether.pid)+"\n" {
		t.Fatalf("held pid %d, the program said it was %q", tether.pid, got)
	}
	if tether.pgid != tether.pid {
		t.Fatalf("the held program leads group %d, not its own (%d)", tether.pgid, tether.pid)
	}
}

// An untied runner tells nobody: ptyd's programs keep outliving a daemon restart.
func TestAnUntiedRunnerStartsProgramsAsBefore(t *testing.T) {
	e := NewOpenExec(os.Environ(), t.TempDir(), quietLog())
	res, err := e.Run(context.Background(), ExecRequest{Argv: []string{"sh", "-c", "exit 3"}, Cwd: t.TempDir()})
	if err != nil || res.ExitCode != 3 {
		t.Fatalf("got %+v, %v", res, err)
	}
}
