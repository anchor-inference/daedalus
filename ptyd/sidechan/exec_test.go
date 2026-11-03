//go:build unix

package sidechan

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestExecRefusesWhatIsNotAllowed(t *testing.T) {
	e := NewExec(nil, os.Environ(), t.TempDir(), quietLog())
	for _, argv := range [][]string{{"sh", "-c", "true"}, {"/bin/sh", "-c", "true"}} {
		if _, err := e.Run(context.Background(), ExecRequest{Argv: argv}); !errors.Is(err, ErrForbidden) {
			t.Errorf("%v: %v, want forbidden", argv, err)
		}
	}
	if _, err := e.Run(context.Background(), ExecRequest{Argv: []string{"no-such-program-here"}}); !errors.Is(err, ErrNotFound) {
		t.Errorf("a missing program: %v", err)
	}
	if _, err := e.Run(context.Background(), ExecRequest{}); !errors.Is(err, ErrInvalid) {
		t.Errorf("no argv: %v", err)
	}
	if _, err := e.Run(context.Background(), ExecRequest{Argv: []string{"git"}, Cwd: "relative"}); !errors.Is(err, ErrInvalid) {
		t.Errorf("a relative cwd: %v", err)
	}
}

// An open runner, daedalus-cli's, runs a program no list names, and still refuses what is not there.
func TestOpenExecHasNoProgramList(t *testing.T) {
	e := NewOpenExec(os.Environ(), t.TempDir(), quietLog())
	res, err := e.Run(context.Background(), ExecRequest{Argv: []string{"sh", "-c", "echo open"}})
	if err != nil || strings.TrimSpace(res.Stdout) != "open" {
		t.Fatalf("%+v, %v", res, err)
	}
	if e.Allowed() != nil {
		t.Errorf("an open runner reported a list: %v", e.Allowed())
	}
	if _, err := e.Run(context.Background(), ExecRequest{Argv: []string{"no-such-program-here"}}); !errors.Is(err, ErrNotFound) {
		t.Errorf("a missing program: %v", err)
	}
}

// A caller that asked for the output as it comes gets all of it, while the result still carries it.
func TestExecStreamsOutput(t *testing.T) {
	e := NewOpenExec(os.Environ(), t.TempDir(), quietLog())
	var seen lockedBuffer
	res, err := e.Run(context.Background(), ExecRequest{Argv: []string{"sh", "-c", "echo one; echo two >&2"}, Output: &seen})
	if err != nil {
		t.Fatal(err)
	}
	if got := seen.String(); !strings.Contains(got, "one") || !strings.Contains(got, "two") {
		t.Errorf("streamed %q", got)
	}
	if strings.TrimSpace(res.Stdout) != "one" || strings.TrimSpace(res.Stderr) != "two" {
		t.Errorf("result %+v", res)
	}
}

type lockedBuffer struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// bash is on the list because a Daedalus agent working in a host folder runs every command and every
// file read through it; the daemon refusing it is an agent on the host that cannot do anything.
func TestExecRunsBash(t *testing.T) {
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("no bash on this machine")
	}
	e := NewExec(nil, os.Environ(), t.TempDir(), quietLog())
	res, err := e.Run(context.Background(), ExecRequest{Argv: []string{"bash", "-c", "exec 2>&1; echo out; echo err >&2"}, Timeout: 20 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 0 || res.Stdout != "out\nerr\n" {
		t.Fatalf("%+v", res)
	}
}

func TestExecRunsGit(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("no git here")
	}
	e := NewExec(nil, os.Environ(), t.TempDir(), quietLog())
	res, err := e.Run(context.Background(), ExecRequest{Argv: []string{"git", "--version"}})
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 0 || !strings.HasPrefix(res.Stdout, "git version ") || res.TimedOut || res.Truncated {
		t.Fatalf("%+v", res)
	}
	// A failing call is a result, not an error: the exit code and stderr are what the caller needs.
	res, err = e.Run(context.Background(), ExecRequest{Argv: []string{"git", "no-such-subcommand"}})
	if err != nil || res.ExitCode == 0 || res.Stderr == "" {
		t.Fatalf("%+v %v", res, err)
	}
}

func TestExecOutputEnvironmentAndStdin(t *testing.T) {
	home := t.TempDir()
	e := NewExec([]string{"sh"}, append(os.Environ(), "CLAUDECODE=1", "DAEDALUS_TERMINAL_ID=outer"), home, quietLog())
	res, err := e.Run(context.Background(), ExecRequest{
		Argv:  []string{"sh", "-c", `printf '%s|%s|%s|%s|' "$HOME" "$EXTRA" "${CLAUDECODE-unset}" "${DAEDALUS_TERMINAL_ID-unset}"; cat; echo oops >&2; exit 3`},
		Env:   map[string]string{"EXTRA": "x"},
		Stdin: []byte("fed"),
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Stdout != home+"|x|unset|unset|fed" || res.Stderr != "oops\n" || res.ExitCode != 3 {
		t.Fatalf("%+v", res)
	}
	// Output beyond max_output is counted away, and the program still finishes.
	res, err = e.Run(context.Background(), ExecRequest{Argv: []string{"sh", "-c", "head -c 300000 /dev/zero | tr '\\0' a; echo done >&2"}, MaxOutput: 1000})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Stdout) != 1000 || !res.Truncated || res.Stderr != "done\n" || res.ExitCode != 0 {
		t.Fatalf("stdout %d bytes, %+v", len(res.Stdout), res.Stderr)
	}
}

// The timeout ends the whole process group: a background child as well as the program.
func TestExecTimeoutKillsTheGroup(t *testing.T) {
	dir := t.TempDir()
	pidFile := filepath.Join(dir, "child")
	e := NewExec([]string{"sh"}, os.Environ(), dir, quietLog())
	start := time.Now()
	res, err := e.Run(context.Background(), ExecRequest{
		Argv:    []string{"sh", "-c", `trap '' HUP; sleep 60 & echo $! > ` + pidFile + `; wait`},
		Timeout: 300 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.TimedOut || res.ExitCode != -1 || res.Signal != "KILL" {
		t.Fatalf("%+v", res)
	}
	if took := time.Since(start); took > 10*time.Second {
		t.Fatalf("took %s", took)
	}
	data, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatal(err)
	}
	pid, _ := strconv.Atoi(strings.TrimSpace(string(data)))
	deadline := time.Now().Add(5 * time.Second)
	for syscall.Kill(pid, 0) == nil {
		if time.Now().After(deadline) {
			t.Fatalf("the background child %d outlived the timeout", pid)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestExecCancelledCallEndsTheProgram(t *testing.T) {
	e := NewExec([]string{"sh"}, os.Environ(), t.TempDir(), quietLog())
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(200*time.Millisecond, cancel)
	start := time.Now()
	res, err := e.Run(ctx, ExecRequest{Argv: []string{"sh", "-c", "sleep 60"}, Timeout: time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode == 0 || time.Since(start) > 10*time.Second {
		t.Fatalf("%+v after %s", res, time.Since(start))
	}
}

func TestFitReplyMeasuresEncoded(t *testing.T) {
	// Control characters are six bytes each once encoded: the budget counts that.
	a, b := strings.Repeat("\x01", 1000), "short"
	if !fitReply(&a, &b, 2000) {
		t.Fatal("nothing was cut")
	}
	if encodedLen(a)+encodedLen(b) > 2000 || b != "short" {
		t.Fatalf("%d + %d", encodedLen(a), encodedLen(b))
	}
}

// A program still running at its hand-over time is kept, not ended: the call answers with what it
// wrote so far, and the log holds that and everything after, to the program's own end.
func TestExecHandOverKeepsTheProgramAndLogsAllOfItsOutput(t *testing.T) {
	e := NewOpenExec(os.Environ(), t.TempDir(), quietLog())
	var log lockedBuffer
	res, job, err := e.RunOrHandOver(context.Background(), ExecRequest{Argv: []string{"sh", "-c", "echo early; sleep 0.6; echo late; exit 3"}},
		HandOver{After: 200 * time.Millisecond, Log: &log})
	if err != nil {
		t.Fatal(err)
	}
	if !res.HandedOver || job == nil || res.Pid != job.Pid || !strings.Contains(res.Stdout, "early") || strings.Contains(res.Stdout, "late") {
		t.Fatalf("%+v %v", res, job)
	}
	if syscall.Kill(job.Pid, 0) != nil {
		t.Fatal("the program did not go on")
	}
	select {
	case <-job.Done():
	case <-time.After(10 * time.Second):
		t.Fatal("the job never ended")
	}
	if end := job.Result(); end.ExitCode != 3 {
		t.Fatalf("%+v", end)
	}
	if got := log.String(); !strings.Contains(got, "early") || !strings.Contains(got, "late") || strings.Count(got, "early") != 1 {
		t.Fatalf("log %q", got)
	}
}

func TestExecHandOverEndsInTimeLikeRun(t *testing.T) {
	e := NewOpenExec(os.Environ(), t.TempDir(), quietLog())
	var log lockedBuffer
	res, job, err := e.RunOrHandOver(context.Background(), ExecRequest{Argv: []string{"sh", "-c", "echo quick"}}, HandOver{After: 5 * time.Second, Log: &log})
	if err != nil {
		t.Fatal(err)
	}
	if job != nil || res.HandedOver || res.ExitCode != 0 || strings.TrimSpace(res.Stdout) != "quick" || log.String() != "" {
		t.Fatalf("%+v %v %q", res, job, log.String())
	}
}

func TestExecHandOverAtOnceAndStopEndsTheGroup(t *testing.T) {
	dir := t.TempDir()
	pidFile := filepath.Join(dir, "child")
	e := NewOpenExec(os.Environ(), dir, quietLog())
	var log lockedBuffer
	_, job, err := e.RunOrHandOver(context.Background(), ExecRequest{Argv: []string{"sh", "-c", "sleep 60 & echo $! > " + pidFile + "; wait"}}, HandOver{Log: &log})
	if err != nil || job == nil {
		t.Fatal(err, job)
	}
	var child int
	deadline := time.Now().Add(5 * time.Second)
	for child == 0 && time.Now().Before(deadline) {
		data, _ := os.ReadFile(pidFile)
		child, _ = strconv.Atoi(strings.TrimSpace(string(data)))
		time.Sleep(20 * time.Millisecond)
	}
	job.Stop()
	if end := job.Result(); end.ExitCode != -1 || end.Signal == "" {
		t.Fatalf("%+v", end)
	}
	for syscall.Kill(child, 0) == nil {
		if time.Now().After(deadline) {
			t.Fatalf("the background child %d outlived the stop", child)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// Cancelled before its hand-over time, the call still ends the program: only the time running out
// keeps one.
func TestExecHandOverCancelledBeforeItsTimeEndsTheProgram(t *testing.T) {
	e := NewOpenExec(os.Environ(), t.TempDir(), quietLog())
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(200*time.Millisecond, cancel)
	var log lockedBuffer
	res, job, err := e.RunOrHandOver(ctx, ExecRequest{Argv: []string{"sh", "-c", "sleep 60"}}, HandOver{After: time.Minute, Log: &log})
	if err != nil {
		t.Fatal(err)
	}
	if job != nil || res.HandedOver || res.ExitCode == 0 {
		t.Fatalf("%+v %v", res, job)
	}
}
