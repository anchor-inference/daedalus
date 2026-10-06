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
