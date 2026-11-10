//go:build unix

package sidechan

import (
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// collected is a stream's output as the tests read it.
type collected struct {
	mu       sync.Mutex
	out, err strings.Builder
}

func (c *collected) output() StreamOutput {
	return StreamOutput{
		Out: func(p []byte) error { c.mu.Lock(); defer c.mu.Unlock(); c.out.Write(p); return nil },
		Err: func(p []byte) error { c.mu.Lock(); defer c.mu.Unlock(); c.err.Write(p); return nil },
	}
}

func (c *collected) text() (string, string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.out.String(), c.err.String()
}

func alive(pid int) bool { return syscall.Kill(pid, 0) == nil }

func waitDead(t *testing.T, pid int, what string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for alive(pid) {
		if time.Now().After(deadline) {
			t.Fatalf("%s (pid %d) is still running", what, pid)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestStreamBridgesStdinAndBothOutputs(t *testing.T) {
	e := NewExec([]string{"sh"}, os.Environ(), t.TempDir(), quietLog())
	var got collected
	s, err := e.Stream(StreamRequest{Argv: []string{"sh", "-c", `while read l; do echo "o:$l"; echo "e:$l" >&2; done`}}, got.output())
	if err != nil {
		t.Fatal(err)
	}
	if e.Streams() != 1 {
		t.Fatalf("%d streams", e.Streams())
	}
	if err := s.Write([]byte("one\ntwo\n")); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		out, errOut := got.text()
		if out == "o:one\no:two\n" && errOut == "e:one\ne:two\n" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("out %q err %q", out, errOut)
		}
		time.Sleep(10 * time.Millisecond)
	}
	start := time.Now()
	s.End()
	// The loop ends at the end of its input, so no hangup was needed.
	if res := s.Result(); res.ExitCode != 0 || time.Since(start) > streamGrace {
		t.Fatalf("%+v after %s", res, time.Since(start))
	}
	if e.Streams() != 0 {
		t.Fatalf("%d streams after the end", e.Streams())
	}
}

func TestStreamRefusesWhatExecRunRefuses(t *testing.T) {
	e := NewExec(nil, os.Environ(), t.TempDir(), quietLog())
	if _, err := e.Stream(StreamRequest{Argv: []string{"sh", "-c", "cat"}}, StreamOutput{}); !errors.Is(err, ErrForbidden) {
		t.Errorf("sh: %v", err)
	}
	if _, err := e.Stream(StreamRequest{Argv: []string{"no-such-program-here"}}, StreamOutput{}); !errors.Is(err, ErrNotFound) {
		t.Errorf("a missing program: %v", err)
	}
	if _, err := e.Stream(StreamRequest{Argv: []string{"bash"}, Cwd: "relative"}, StreamOutput{}); !errors.Is(err, ErrInvalid) {
		t.Errorf("a relative cwd: %v", err)
	}
	if e.Streams() != 0 || len(e.streams) != 0 {
		t.Fatalf("a refused stream holds a slot: %d %d", e.Streams(), len(e.streams))
	}
}

// A program that exits by itself takes what it left in its group with it.
func TestAStreamThatExitsLeavesNothingBehind(t *testing.T) {
	dir := t.TempDir()
	pidFile := filepath.Join(dir, "child")
	e := NewExec([]string{"sh"}, os.Environ(), dir, quietLog())
	s, err := e.Stream(StreamRequest{Argv: []string{"sh", "-c", `sleep 60 >/dev/null 2>&1 & echo $! > ` + pidFile + `; exit 4`}}, StreamOutput{})
	if err != nil {
		t.Fatal(err)
	}
	if res := s.Result(); res.ExitCode != 4 {
		t.Fatalf("%+v", res)
	}
	data, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatal(err)
	}
	pid, _ := strconv.Atoi(strings.TrimSpace(string(data)))
	waitDead(t, pid, "the child left behind")
}

// The daemon's shutdown ends every stream, the stubborn ones too.
func TestEndStreamsEndsThemAll(t *testing.T) {
	e := NewExec([]string{"sh"}, os.Environ(), t.TempDir(), quietLog())
	var pids []int
	for _, script := range []string{"cat", `trap '' HUP; while :; do sleep 0.1; done`} {
		s, err := e.Stream(StreamRequest{Argv: []string{"sh", "-c", script}}, StreamOutput{})
		if err != nil {
			t.Fatal(err)
		}
		pids = append(pids, s.Pid)
	}
	start := time.Now()
	e.EndStreams()
	if took := time.Since(start); took > streamGrace+killGrace+3*time.Second {
		t.Fatalf("took %s", took)
	}
	for _, pid := range pids {
		waitDead(t, pid, "a streamed program")
	}
	if e.Streams() != 0 {
		t.Fatalf("%d streams left", e.Streams())
	}
}
