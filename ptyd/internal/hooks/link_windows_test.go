//go:build windows

package hooks

import (
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// running copies a small system program to path and starts it, so that path is an executable a
// process is running from — the daemon's situation at every start after the first. It is stopped
// before the test's directory is removed.
func running(t *testing.T, path string) {
	t.Helper()
	ping, err := exec.LookPath("ping")
	if err != nil {
		t.Skip("no ping to stand in for a running program")
	}
	body, err := os.ReadFile(ping)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, body, 0o700); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(path, "-n", "60", "127.0.0.1")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_, _ = cmd.Process.Wait()
	})
}

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// Windows refuses to delete any name of an executable a process runs from. The hook command is a
// hard link to the daemon, so the second start used to fail removing it — "Access is denied" — and
// ptyd exited every time after the first.
func TestRegistryStartsAgainWhileItsDaemonRuns(t *testing.T) {
	dir := t.TempDir()
	daemon := filepath.Join(dir, "ptyd.exe")
	running(t, daemon)
	state := filepath.Join(dir, "state")
	for start := 1; start <= 3; start++ {
		reg, err := NewRegistry(state, daemon, nil, quiet())
		if err != nil {
			t.Fatalf("start %d: %v", start, err)
		}
		if !sameFile(daemon, reg.HookCommand()) {
			t.Fatalf("start %d: %s is not the daemon", start, reg.HookCommand())
		}
		reg.Close()
	}
}

// A hook command left by another daemon binary — one an upgrade replaced — is replaced, even while
// a hook still runs from it: a running program cannot be deleted, but it can be moved aside.
func TestRegistryReplacesAnOlderHookCommandEvenWhileItRuns(t *testing.T) {
	dir := t.TempDir()
	daemon := filepath.Join(dir, "ptyd.exe")
	if err := os.WriteFile(daemon, []byte("the new daemon"), 0o700); err != nil {
		t.Fatal(err)
	}
	state := filepath.Join(dir, "state")
	if err := os.MkdirAll(filepath.Join(state, "bin"), 0o700); err != nil {
		t.Fatal(err)
	}
	running(t, filepath.Join(state, "bin", hookCommandName))
	reg, err := NewRegistry(state, daemon, nil, quiet())
	if err != nil {
		t.Fatal(err)
	}
	defer reg.Close()
	if !sameFile(daemon, reg.HookCommand()) {
		t.Fatalf("%s is still the older program", reg.HookCommand())
	}
}
