package tui_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/tui/ptytest"
)

// These tests run the real program, the demo command on the fake backend, in a pseudo-terminal and
// read the screen back through a virtual terminal: the keys arrive as bytes, the frames leave as
// escape sequences, and nothing of the model is touched directly.

var (
	demoOnce sync.Once
	demoDir  string
	demoPath string
	demoErr  error
)

// TestMain removes the demo binary the pty tests share once every test has run.
func TestMain(m *testing.M) {
	code := m.Run()
	if demoDir != "" {
		_ = os.RemoveAll(demoDir)
	}
	os.Exit(code)
}

func buildDemo(t *testing.T) string {
	t.Helper()
	if testing.Short() {
		t.Skip("builds and runs the demo in a pty")
	}
	demoOnce.Do(func() {
		dir, err := os.MkdirTemp("", "tui-demo-")
		if err != nil {
			demoErr = err
			return
		}
		demoDir = dir
		demoPath = filepath.Join(dir, "demo")
		out, err := exec.Command("go", "build", "-o", demoPath, "./demo").CombinedOutput()
		if err != nil {
			demoErr = err
			t.Logf("%s", out)
		}
	})
	if demoErr != nil {
		t.Fatal(demoErr)
	}
	return demoPath
}

func start(t *testing.T, cols, rows int, args ...string) *ptytest.Term {
	t.Helper()
	cmd := exec.Command(buildDemo(t), append([]string{"-cwd", "/work/project", "-background", "dark", "-delay", "5ms"}, args...)...)
	cmd.Env = append(cmd.Environ(), "LANG=C.UTF-8", "TERM=xterm-256color", "NO_COLOR=")
	term, err := ptytest.Start(t, cmd, cols, rows)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		// TUI_CAST_DIR keeps an asciicast of each run, for looking at what a test saw.
		if dir := os.Getenv("TUI_CAST_DIR"); dir != "" {
			_ = term.WriteCast(filepath.Join(dir, t.Name()+".cast"))
		}
		_ = term.Close()
	})
	if err := term.WaitFor("Welcome to Daedalus", 5*time.Second); err != nil {
		t.Fatal(err)
	}
	return term
}

func wait(t *testing.T, term *ptytest.Term, want string) {
	t.Helper()
	if err := term.WaitFor(want, 5*time.Second); err != nil {
		t.Fatalf("%v\n%s", err, term.Screen())
	}
}

func TestPtyPromptStreamsAnAnswerInFullscreen(t *testing.T) {
	term := start(t, 100, 40)
	if !term.AltScreen() {
		t.Fatal("fullscreen is the default and did not take the alternate screen")
	}
	_ = term.Write("explain the store")
	_ = term.Keys("enter")
	wait(t, term, "one cursor per session")
	wait(t, term, "Read(internal/store/store.go)")
	lines := term.Lines()
	if !strings.HasPrefix(strings.TrimSpace(lines[len(lines)-3]), "╰") {
		t.Fatalf("the prompt box is not pinned at the bottom:\n%s", term.Screen())
	}
}

func TestPtyPermissionPromptAnsweredWithANumber(t *testing.T) {
	term := start(t, 100, 40)
	_ = term.Write("run the tests")
	_ = term.Keys("enter")
	wait(t, term, "Do you want to proceed?")
	wait(t, term, "2. Yes, and don't ask again for npm test *")
	_ = term.Write("1")
	wait(t, term, "All 19 tests pass.")
}

func TestPtyShiftTabCyclesTheGuardIndicator(t *testing.T) {
	term := start(t, 100, 30)
	wait(t, term, "⏵⏵ auto-accept on")
	_ = term.Keys("shift+tab")
	wait(t, term, "⏸ plan mode on")
	_ = term.Keys("shift+tab")
	wait(t, term, "⏸ ask before acting")
}

func TestPtyEscInterruptsAndDoubleCtrlCExits(t *testing.T) {
	cmd := exec.Command(buildDemo(t), "-cwd", "/work/project", "-background", "dark", "-delay", "40ms")
	cmd.Env = append(cmd.Environ(), "LANG=C.UTF-8", "TERM=xterm-256color")
	term, err := ptytest.Start(t, cmd, 100, 30)
	if err != nil {
		t.Fatal(err)
	}
	defer term.Close()
	wait(t, term, "Welcome to Daedalus")
	_ = term.Write("explain the store")
	_ = term.Keys("enter")
	wait(t, term, "esc to interrupt")
	_ = term.Keys("esc")
	wait(t, term, "Interrupted · What should Daedalus do instead?")
	_ = term.Keys("ctrl+c")
	wait(t, term, "Press Ctrl-C again to exit")
	_ = term.Keys("ctrl+c")
	if err := term.Wait(5 * time.Second); err != nil {
		t.Fatalf("the program did not exit: %v", err)
	}
	if term.AltScreen() {
		t.Fatal("the alternate screen was left on after exit")
	}
}

func TestPtyResizeKeepsAWholeFrame(t *testing.T) {
	term := start(t, 120, 40)
	_ = term.Write("show me long output")
	_ = term.Keys("enter")
	wait(t, term, "+34 lines (ctrl+o to expand)")
	_ = term.Resize(70, 22)
	whole := func(screen string) bool {
		lines := strings.Split(screen, "\n")
		return len(lines) == 22 && strings.HasPrefix(strings.TrimSpace(lines[19]), "╰") && strings.Contains(screen, "context left")
	}
	if err := term.WaitUntil(whole, 5*time.Second, "a whole 70x22 frame"); err != nil {
		t.Fatalf("%v\n%s", err, term.Screen())
	}
}

func TestPtyPaletteAndTranscriptView(t *testing.T) {
	term := start(t, 100, 30)
	_ = term.Write("/")
	wait(t, term, "/compact [focus]")
	// No esc before ctrl+u: a terminal sends esc and the next key as one alt-chord when they arrive
	// together, which a test writing both at once always does.
	_ = term.Keys("ctrl+u")
	_ = term.Write("show me long output")
	_ = term.Keys("enter")
	wait(t, term, "All 37 packages pass")
	_ = term.Keys("ctrl+o")
	wait(t, term, "Transcript")
	wait(t, term, "pkg37")
	_ = term.Write("q")
	wait(t, term, "(ctrl+o to expand)")
}

func TestPtyReconnectingPausesTheTurn(t *testing.T) {
	term := start(t, 110, 30, "-delay", "30ms")
	_ = term.Write("what if the connection drops")
	_ = term.Keys("enter")
	wait(t, term, "Reconnecting… turn paused")
	wait(t, term, "Back after the drop")
}

func TestPtyInlineModeKeepsTheShellHistory(t *testing.T) {
	script := `i=1; while [ $i -le 30 ]; do echo before-$i; i=$((i+1)); done; exec "$0" "$@"`
	cmd := exec.Command("sh", "-c", script, buildDemo(t), "-cwd", "/work/project", "-background", "dark", "-delay", "5ms", "-inline")
	cmd.Env = append(cmd.Environ(), "LANG=C.UTF-8", "TERM=xterm-256color")
	term, err := ptytest.Start(t, cmd, 100, 30)
	if err != nil {
		t.Fatal(err)
	}
	defer term.Close()
	wait(t, term, "Welcome to Daedalus")
	if term.AltScreen() {
		t.Fatal("inline mode took the alternate screen")
	}
	_ = term.Write("explain the store")
	_ = term.Keys("enter")
	wait(t, term, "negative cursor replays from zero")
	_ = term.Keys("ctrl+c", "ctrl+c")
	_ = term.Wait(5 * time.Second)
	history := strings.Join(term.History(), "\n")
	for _, want := range []string{"before-30", "Welcome to Daedalus", "one cursor per session"} {
		if !strings.Contains(history, want) {
			t.Errorf("the scrollback lost %q:\n%s", want, history)
		}
	}
}
