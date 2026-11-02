package ptytest

import (
	"fmt"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// buildGateprog builds the gate program once per test into the test's own directory.
func buildGateprog(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "gateprog")
	out, err := exec.Command("go", "build", "-o", path, "./gateprog").CombinedOutput()
	if err != nil {
		t.Fatalf("build gateprog: %v\n%s", err, out)
	}
	return path
}

// withPreamble runs program after printing lines of shell output, so the run starts with a full
// screen and a scrollback, as it does for a person.
func withPreamble(lines int, program string, args ...string) *exec.Cmd {
	script := fmt.Sprintf("i=1; while [ $i -le %d ]; do echo before-$i; i=$((i+1)); done; exec \"$0\" \"$@\"", lines)
	return exec.Command("sh", append([]string{"-c", script, program}, args...)...)
}

func countBefore(lines []string) int {
	kept := 0
	for _, line := range lines {
		if strings.HasPrefix(line, "before-") {
			kept++
		}
	}
	return kept
}

// The fullscreen renderer draws every streamed frame inside a synchronized update, clears the whole
// screen only when the window size changes, and leaves a whole frame after a resize.
func TestFullscreenDrawsInsideSynchronizedUpdates(t *testing.T) {
	if testing.Short() {
		t.Skip("starts a program in a pty")
	}
	program := buildGateprog(t)
	term, err := Start(t, exec.Command(program, "-events=60", "-delay=10ms", "-quit-after=300ms"), 100, 30)
	if err != nil {
		t.Fatal(err)
	}
	whole := func(screen string) bool { return GateFrameProblem(screen) == "" }
	if err := term.WaitUntil(whole, 5*time.Second, "a whole first frame"); err != nil {
		t.Fatal(err)
	}
	if err := term.Resize(80, 24); err != nil {
		t.Fatal(err)
	}
	narrowAndWhole := func(screen string) bool {
		lines := strings.Split(screen, "\n")
		return len(lines) == 24 && whole(screen) && strings.HasPrefix(strings.TrimSpace(lines[len(lines)-2]), "╰")
	}
	if err := term.WaitUntil(narrowAndWhole, 5*time.Second, "a whole frame at 80x24"); err != nil {
		t.Fatal(err)
	}
	if err := term.Wait(10 * time.Second); err != nil {
		t.Fatalf("gateprog: %v", err)
	}

	recording := term.Recording()
	stats := Analyze(recording)
	t.Log(stats)
	if stats.SteadySyncShare < 0.95 {
		t.Errorf("only %.3f of the drawing was inside synchronized updates", stats.SteadySyncShare)
	}
	if stats.AltScreenEnters != 1 || stats.AltScreenLeaves != 1 {
		t.Errorf("alternate screen entered %d and left %d times, want once each", stats.AltScreenEnters, stats.AltScreenLeaves)
	}
	// The first clear is the program's first frame; any later one must follow a resize closely,
	// since a clear while streaming is a visible flash.
	for _, at := range stats.FullClearTimes[min(1, len(stats.FullClearTimes)):] {
		explained := false
		for _, resize := range recording.Resizes {
			if at >= resize.At && at-resize.At < time.Second {
				explained = true
			}
		}
		if !explained {
			t.Errorf("full clear at %s with no resize before it (resizes %v)", at, recording.Resizes)
		}
	}
}

// Leaving the alternate screen gives back the shell output that was there before, untouched.
func TestFullscreenKeepsScrollback(t *testing.T) {
	if testing.Short() {
		t.Skip("starts a program in a pty")
	}
	program := buildGateprog(t)
	term, err := Start(t, withPreamble(50, program, "-events=40", "-delay=5ms", "-quit-after=200ms"), 100, 30)
	if err != nil {
		t.Fatal(err)
	}
	if err := term.WaitUntil(func(s string) bool { return GateFrameProblem(s) == "" }, 5*time.Second, "a whole frame"); err != nil {
		t.Fatal(err)
	}
	if !term.AltScreen() {
		t.Error("the program is not on the alternate screen")
	}
	if err := term.Wait(10 * time.Second); err != nil {
		t.Fatalf("gateprog: %v", err)
	}
	if kept := countBefore(term.History()); kept != 50 {
		t.Errorf("%d of 50 earlier shell lines are left after exit", kept)
	}
}

// In tmux, which does not offer synchronized output, no frame read back while the answer streams is
// missing its input box or status line, and the pane's history keeps the shell output from before.
func TestTmuxFullscreenHasNoIncompleteFrames(t *testing.T) {
	if testing.Short() || !TmuxAvailable() {
		t.Skip("needs tmux")
	}
	program := buildGateprog(t)
	session, err := NewTmux(100, 30)
	if err != nil {
		t.Skip(err)
	}
	defer session.Kill()
	for i := 1; i <= 20; i++ {
		if err := session.Type(fmt.Sprintf("echo before-%02d", i), true); err != nil {
			t.Fatal(err)
		}
	}
	time.Sleep(200 * time.Millisecond)
	if err := session.Type(program+" -events=150 -delay=10ms -quit-after=500ms; echo gate-exited", true); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(15 * time.Second)
	settled, polls := false, 0
	for time.Now().Before(deadline) {
		screen, err := session.Capture()
		if err != nil {
			t.Fatal(err)
		}
		// The marker counts on a line of its own: the typed command line contains it too.
		if strings.Contains("\n"+screen, "\ngate-exited\n") {
			break
		}
		problem := GateFrameProblem(screen)
		switch {
		case !settled:
			settled = problem == ""
		case problem != "" && strings.Contains(screen, "Pondering"):
			t.Fatalf("incomplete frame while streaming: %s\n%s", problem, screen)
		case problem == "":
			polls++
		}
		time.Sleep(5 * time.Millisecond)
	}
	if polls < 20 {
		t.Fatalf("only %d whole frames were read back; the replay did not run", polls)
	}
	time.Sleep(300 * time.Millisecond)
	history, err := session.History()
	if err != nil {
		t.Fatal(err)
	}
	for i := 1; i <= 20; i++ {
		if !strings.Contains(history, fmt.Sprintf("before-%02d\n", i)) {
			t.Errorf("before-%02d is gone from the pane history", i)
		}
	}
}

func TestAnalyzeCountsSequences(t *testing.T) {
	recording := Recording{Cols: 80, Rows: 24, Chunks: []Chunk{
		{At: 0, Data: []byte("\x1b[?1049h\x1b[2J\x1b[?2026hab")},
		// A sequence cut across two reads counts once.
		{At: time.Second, Data: []byte("c\x1b[")},
		{At: 2 * time.Second, Data: []byte("K\x1b[?2026lplain\x1b[2J\x1b[?1049l")},
	}}
	stats := Analyze(recording)
	if stats.Erases != 3 || stats.FullClears != 2 || stats.FullClearsAfterFirst != 1 {
		t.Errorf("erases=%d full=%d after_first=%d, want 3, 2, 1", stats.Erases, stats.FullClears, stats.FullClearsAfterFirst)
	}
	if stats.SyncFrames != 1 || stats.AltScreenEnters != 1 || stats.AltScreenLeaves != 1 {
		t.Errorf("sync=%d alt=%d/%d", stats.SyncFrames, stats.AltScreenEnters, stats.AltScreenLeaves)
	}
	if stats.SteadySyncShare != 1 {
		t.Errorf("steady sync share %.3f, want 1", stats.SteadySyncShare)
	}
	if stats.Duration != 2*time.Second || stats.ErasesPerSecond != 1.5 {
		t.Errorf("duration %s rate %.2f", stats.Duration, stats.ErasesPerSecond)
	}
}

func TestKeySequence(t *testing.T) {
	cases := map[string]string{"enter": "\r", "ctrl+c": "\x03", "ctrl+o": "\x0f", "shift+tab": "\x1b[Z", "alt+b": "\x1bb", "hello": "hello"}
	for name, want := range cases {
		if got := KeySequence(name); got != want {
			t.Errorf("KeySequence(%q) = %q, want %q", name, got, want)
		}
	}
}

func TestIncompleteTail(t *testing.T) {
	full := []byte("a●")
	for cut := 0; cut <= 3; cut++ {
		data := full[:len(full)-cut]
		want := 0
		if cut > 0 && cut < 3 {
			want = 3 - cut
		}
		if got := incompleteTail(data); got != want {
			t.Errorf("cut %d: incompleteTail = %d, want %d", cut, got, want)
		}
	}
}
