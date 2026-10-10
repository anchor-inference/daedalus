//go:build linux

package sidechan

import (
	"context"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A program run by exec.run starts with the broken pipe at its default, even in a daemon started with
// it ignored, so a pipeline whose reader stops early ends quietly. The daemon once ignored SIGPIPE,
// and `tail file | head -c N` put "tail: error writing 'standard output': Broken pipe" into the
// output the agent read. The ignoring is inherited only at a process's start, so the test runs
// itself again as a process started that way.
func TestExecProgramsStartWithTheBrokenPipeAtItsDefault(t *testing.T) {
	if os.Getenv("SIDECHAN_SIGPIPE_CHILD") != "" {
		execSigpipeChild(t)
		return
	}
	signal.Ignore(syscall.SIGPIPE)
	cmd := exec.Command(os.Args[0], "-test.run=^TestExecProgramsStartWithTheBrokenPipeAtItsDefault$", "-test.v")
	cmd.Env = append(os.Environ(), "SIDECHAN_SIGPIPE_CHILD=1")
	out, err := cmd.CombinedOutput()
	signal.Reset(syscall.SIGPIPE)
	if err != nil || !strings.Contains(string(out), "--- PASS") {
		t.Fatalf("%v\n%s", err, out)
	}
}

func execSigpipeChild(t *testing.T) {
	e := NewExec([]string{"sh", "cat"}, os.Environ(), t.TempDir(), quietLog())
	res, err := e.Run(context.Background(), ExecRequest{Argv: []string{"cat", "/proc/self/status"}, Timeout: 20 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	var mask uint64
	found := false
	for _, line := range strings.Split(res.Stdout, "\n") {
		if rest, ok := strings.CutPrefix(line, "SigIgn:"); ok {
			if mask, err = strconv.ParseUint(strings.TrimSpace(rest), 16, 64); err != nil {
				t.Fatalf("SigIgn %q: %v", rest, err)
			}
			found = true
		}
	}
	if !found {
		t.Fatalf("no SigIgn in %+v", res)
	}
	if mask&(1<<(uint(syscall.SIGPIPE)-1)) != 0 {
		t.Fatalf("the program started with SIGPIPE ignored (SigIgn %016x)", mask)
	}
	if _, err := exec.LookPath("yes"); err != nil {
		return
	}
	// The pipeline from the report: the writer is ended by the signal and says nothing.
	res, err = e.Run(context.Background(), ExecRequest{Argv: []string{"sh", "-c", "yes | head -c 4"}, Timeout: 20 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 0 || res.Stdout != "y\ny\n" || res.Stderr != "" {
		t.Fatalf("%+v", res)
	}
}
