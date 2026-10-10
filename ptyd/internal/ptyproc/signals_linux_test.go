//go:build linux

package ptyproc

import (
	"bytes"
	"errors"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"testing"

	"golang.org/x/sys/unix"
)

// ignoredSignals reads the SigIgn mask out of a /proc/<pid>/status text.
func ignoredSignals(t *testing.T, status string) uint64 {
	t.Helper()
	for _, line := range strings.Split(status, "\n") {
		if rest, ok := strings.CutPrefix(line, "SigIgn:"); ok {
			mask, err := strconv.ParseUint(strings.TrimSpace(rest), 16, 64)
			if err != nil {
				t.Fatalf("SigIgn %q: %v", rest, err)
			}
			return mask
		}
	}
	t.Fatalf("no SigIgn in %q", status)
	return 0
}

func signalBit(sig syscall.Signal) uint64 { return 1 << (uint(sig) - 1) }

// A daemon set up the way ptyd sets itself up, and started the way a service manager starts it (with
// the broken pipe and the hangup ignored), starts terminal programs with no signal ignored that they
// should see, and survives a write to a standard output nobody reads. The daemon once ignored
// SIGPIPE itself, and every program in its terminals inherited that.
func TestTerminalProgramsStartWithTheBrokenPipeAtItsDefault(t *testing.T) {
	if os.Getenv("PTYPROC_SIGPIPE_CHILD") != "" {
		sigpipeChild(t)
		return
	}
	signal.Ignore(syscall.SIGHUP, syscall.SIGPIPE)
	cmd := exec.Command(os.Args[0], "-test.run=^TestTerminalProgramsStartWithTheBrokenPipeAtItsDefault$", "-test.v")
	cmd.Env = append(os.Environ(), "PTYPROC_SIGPIPE_CHILD=1")
	out, err := cmd.CombinedOutput()
	signal.Reset(syscall.SIGHUP, syscall.SIGPIPE)
	if err != nil || !strings.Contains(string(out), "--- PASS") {
		t.Fatalf("%v\n%s", err, out)
	}
}

func sigpipeChild(t *testing.T) {
	cat, err := exec.LookPath("cat")
	if err != nil {
		t.Skip("no cat")
	}
	// What the daemon does as it starts.
	signal.Ignore(syscall.SIGHUP)
	DefaultSignalsForChildren()

	p, err := Start(Spec{Path: cat, Argv: []string{"cat", "/proc/self/status"}, Env: os.Environ(), Cols: 200, Rows: 50})
	if err != nil {
		t.Fatal(err)
	}
	var status bytes.Buffer
	buf := make([]byte, 4096)
	for {
		n, err := p.Master.Read(buf)
		status.Write(buf[:n])
		if err != nil {
			break
		}
	}
	<-p.Done()
	p.Close()
	mask := ignoredSignals(t, strings.ReplaceAll(status.String(), "\r", ""))
	for _, sig := range []syscall.Signal{syscall.SIGPIPE, syscall.SIGHUP, syscall.SIGINT} {
		if mask&signalBit(sig) != 0 {
			t.Errorf("the terminal program started with SIG%s ignored (SigIgn %016x)", SignalName(sig), mask)
		}
	}

	// The daemon itself: a write to a standard output whose reader is gone is an error, not its end.
	// The runtime raises SIGPIPE only for the standard output and error, so the test puts a dead
	// pipe there for one write and then gives the real one back for its report.
	saved, err := unix.Dup(1)
	if err != nil {
		t.Fatal(err)
	}
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	r.Close()
	if err := unix.Dup2(int(w.Fd()), 1); err != nil {
		t.Fatal(err)
	}
	_, werr := os.Stdout.Write([]byte("nobody reads this\n"))
	if err := unix.Dup2(saved, 1); err != nil {
		t.Fatal(err)
	}
	unix.Close(saved)
	w.Close()
	if !errors.Is(werr, syscall.EPIPE) {
		t.Fatalf("a write to a dead standard output: %v, want EPIPE", werr)
	}
}
