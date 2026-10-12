package display

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestStartTakesAFreeDisplay(t *testing.T) {
	if _, err := exec.LookPath("Xvfb"); err != nil {
		if os.Getenv("BROWSERD_REQUIRE_XVFB") != "" {
			t.Fatal("no Xvfb, and the gate requires one")
		}
		t.Skip("no Xvfb here")
	}
	a, err := Start(640, 480)
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	// A second one beside it takes another number rather than failing on the first's.
	b, err := Start(640, 480)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(a.Name, ":") || a.Name == b.Name {
		t.Fatalf("displays %q and %q", a.Name, b.Name)
	}
	b.Close()
	if b.Alive() || !a.Alive() {
		t.Fatal("closing one display touched the other, or did not end it")
	}
}

func TestNoXvfbIsSaidPlainly(t *testing.T) {
	t.Setenv("PATH", filepath.Join(t.TempDir(), "empty"))
	if _, err := Start(640, 480); err != ErrNoXvfb {
		t.Fatalf("got %v", err)
	}
}
