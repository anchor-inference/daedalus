//go:build unix

package browser

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// fakeManager is a manager whose Chromium is the shell script body. The script answers the
// manager's --version probe on its own, so that only the browser it starts runs body.
func fakeManager(t *testing.T, body string, budget time.Duration) *Manager {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "chrome")
	script := "#!/bin/sh\nif [ \"$1\" = --version ]; then echo Chromium 1.0.0.0; exit 0; fi\n" + body + "\n"
	if err := os.WriteFile(path, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{StateDir: filepath.Join(dir, "state"), Limits: config.DefaultLimits(),
		Chromium: config.Chromium{Path: path, StartTimeout: budget}}
	return New(Deps{Config: cfg, Log: slog.New(slog.NewTextHandler(io.Discard, nil))})
}

func startFailure(t *testing.T, m *Manager) *wire.Error {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	_, _, _, err := m.Open(ctx, OpenParams{GroupID: "g", Profile: "p"})
	we, ok := err.(*wire.Error)
	if !ok || we.Code != wire.CodeUnsupported {
		t.Fatalf("open: %v", err)
	}
	if n := len(m.Browsers()); n != 0 {
		t.Fatalf("%d browsers left after a failed start", n)
	}
	return we
}

// A Chromium that dies on start is answered with its own last words and exit code, at once.
func TestOpenSaysWhyChromiumExited(t *testing.T) {
	m := fakeManager(t, `echo "chrome: error while loading shared libraries: libnss3.so" >&2; exit 127`, time.Minute)
	begin := time.Now()
	we := startFailure(t, m)
	if took := time.Since(begin); took > 10*time.Second {
		t.Fatalf("an exited browser was waited for %s", took)
	}
	if !strings.Contains(we.Message, "exited with code 127") || !strings.Contains(we.Message, "libnss3.so") {
		t.Fatalf("message: %s", we.Message)
	}
}

// A Chromium that never answers is reported as such when its budget ends, with what it printed, and
// nothing of it is left running.
func TestOpenSaysChromiumDidNotAnswer(t *testing.T) {
	pidFile := filepath.Join(t.TempDir(), "pid")
	m := fakeManager(t, `echo $$ > `+pidFile+`; echo "waiting for something" >&2; exec sleep 60`, 300*time.Millisecond)
	we := startFailure(t, m)
	if !strings.Contains(we.Message, "did not answer Browser.getVersion on its pipe within") ||
		!strings.Contains(we.Message, "waiting for something") {
		t.Fatalf("message: %s", we.Message)
	}
	data, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatal(err)
	}
	stat, err := os.ReadFile("/proc/" + strings.TrimSpace(string(data)) + "/stat")
	if err == nil {
		// A zombie is gone too: only its parent's bookkeeping is left.
		if i := strings.LastIndexByte(string(stat), ')'); i < 0 || !strings.HasPrefix(strings.TrimSpace(string(stat[i+1:])), "Z") {
			t.Fatalf("the silent browser still runs: %s", stat)
		}
	}
}
