package selftest

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/desk"
)

// privateDisplay makes the test start its own virtual display, whatever display the machine running
// the tests has: a test must never open a window on, let alone drive, the operator's own screen.
func privateDisplay(t *testing.T) {
	t.Helper()
	t.Setenv("DISPLAY", "")
	t.Setenv("WAYLAND_DISPLAY", "")
	t.Setenv("XDG_SESSION_TYPE", "")
	if _, err := exec.LookPath("Xvfb"); err != nil {
		if os.Getenv("DESKD_REQUIRE_X11") != "" {
			t.Fatal("no Xvfb, and DESKD_REQUIRE_X11 is set")
		}
		t.Skip("no Xvfb: the X11 tests run in toolchain.Dockerfile's image")
	}
}

func TestTheSelftestPassesOnAVirtualDisplay(t *testing.T) {
	privateDisplay(t)
	var out bytes.Buffer
	code := Run(context.Background(), &out, false)
	t.Log("\n" + out.String())
	if code != 0 {
		t.Fatalf("the selftest failed")
	}
	if !strings.Contains(out.String(), "0 failed") || !strings.Contains(out.String(), "virtual display") {
		t.Fatalf("report:\n%s", out.String())
	}
}

// xdotool sends its input through XTest from a client of its own: to the daemon that is input it did
// not make, exactly as the operator's hand is.
func TestAnotherClientsInputPausesTheAgent(t *testing.T) {
	privateDisplay(t)
	if _, err := exec.LookPath("xdotool"); err != nil {
		t.Skip("no xdotool")
	}
	cleanup, _, err := prepare()
	if err != nil {
		t.Fatal(err)
	}
	defer cleanup()
	ctx := context.Background()
	be, err := openBackend(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer be.Close()
	d, err := startDaemon(be, false)
	if err != nil {
		t.Fatal(err)
	}
	defer d.stop()
	w, err := opener(ctx, "deskd test", windowW, windowH)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	if err := d.operator.Call(ctx, "events.subscribe", map[string]any{}, nil); err != nil {
		t.Fatal(err)
	}
	grant := func() {
		t.Helper()
		if err := d.operator.Call(ctx, "lease.grant", map[string]any{"session": session,
			"scope": map[string]any{"kind": "window", "window": w.ID()}}, nil); err != nil {
			t.Fatal(err)
		}
		// Past the grace the daemon gives its own input.
		time.Sleep(500 * time.Millisecond)
	}
	wait := func() error {
		return d.host.Call(ctx, "act", map[string]any{"session": session, "action": "wait", "ms": 1}, nil)
	}
	for _, tc := range []struct {
		name string
		args []string
	}{
		{"a key alone, seen through the idle time", []string{"key", "shift"}},
		{"the pointer, seen where the daemon did not leave it", []string{"mousemove", "7", "9"}},
	} {
		grant()
		if err := wait(); err != nil {
			t.Fatalf("%s: before: %v", tc.name, err)
		}
		if out, err := exec.Command("xdotool", tc.args...).CombinedOutput(); err != nil {
			t.Fatalf("%s: xdotool: %v %s", tc.name, err, out)
		}
		if _, err := d.operator.WaitEvent("human.input", "", 3*time.Second); err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if err := wait(); codeOf(err) != desk.CodeHumanDriving {
			t.Fatalf("%s: while the operator drives: %v", tc.name, err)
		}
		if err := d.operator.Call(ctx, "resume", nil, nil); err != nil {
			t.Fatal(err)
		}
		if err := wait(); err != nil {
			t.Fatalf("%s: after resume: %v", tc.name, err)
		}
	}
}
