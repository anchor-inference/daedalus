//go:build cgo && unix

package term

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/answer"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/production"
	"github.com/ascorblack/daedalus/ptyd/internal/procenv"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

// screenRegistry runs terminals the way the daemon does: the production emulator and the answerer.
func screenRegistry(t *testing.T) (*Registry, string) {
	t.Helper()
	reg := NewRegistry(Deps{
		Emulator: production.Factory, Answer: answer.Reply, Events: &recorder{}, Clock: RealClock{},
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)), KillGrace: 200 * time.Millisecond,
	}, 8)
	t.Cleanup(func() { reg.Shutdown(200 * time.Millisecond) })
	return reg, t.TempDir()
}

func startIn(t *testing.T, reg *Registry, dir, id string, cols, rows int, script string) *Terminal {
	t.Helper()
	env := procenv.BuildEnv(os.Environ(), nil, map[string]string{"LC_ALL": "C.UTF-8"}, id)
	sh, ok := procenv.LookPath("sh", env, "/")
	if !ok {
		t.Skip("no sh")
	}
	term, err := reg.Create(Spec{ID: id, Path: sh, Argv: []string{"sh", "-c", script}, Cwd: dir, Env: env,
		Cols: cols, Rows: rows, RingBytes: 1 << 20})
	if err != nil {
		t.Fatal(err)
	}
	return term
}

// A program that asks its terminal questions and waits for the answers gets them with nobody
// attached: the device attributes, the cursor position and the kitty keyboard flags.
func TestQueriesAreAnsweredWithNoClient(t *testing.T) {
	if _, err := os.Stat("/bin/stty"); err != nil {
		if _, err := os.Stat("/usr/bin/stty"); err != nil {
			t.Skip("no stty")
		}
	}
	reg, dir := screenRegistry(t)
	// Raw mode, so the replies arrive unechoed and without waiting for a newline; head then waits for
	// exactly the bytes of the three replies, and would wait forever for a missing one.
	term := startIn(t, reg, dir, "asks", 80, 24, `stty raw -echo
printf '\033[3;5H\033[c\033[6n\033[?u'
r=$(head -c 18 | od -An -c | tr -s ' \n' ' ')
stty sane
printf '\nGOT%s\n' "$r"`)
	out := waitOutput(t, term, "GOT")
	waitDone(t, term)
	got := out[strings.Index(out, "GOT"):]
	// ESC [ ? 1 ; 2 c, ESC [ 3 ; 5 R, ESC [ ? 0 u
	want := `GOT 033 [ ? 1 ; 2 c 033 [ 3 ; 5 R 033 [ ? 0 u`
	if !strings.HasPrefix(strings.TrimSpace(got), want) {
		t.Fatalf("replies %q, want %q", got, want)
	}
}

// The text of a coloured listing is the names, whatever the colours.
func TestReadScreenTextOfAColouredListing(t *testing.T) {
	reg, dir := screenRegistry(t)
	for _, name := range []string{"alpha.txt", "beta.sh"} {
		if err := os.WriteFile(filepath.Join(dir, name), nil, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Mkdir(filepath.Join(dir, "gamma"), 0o755); err != nil {
		t.Fatal(err)
	}
	term := startIn(t, reg, dir, "ls", 80, 24, `ls --color=always -1 2>/dev/null || ls -1`)
	waitDone(t, term)
	s, err := term.ReadScreen(ScreenText, 0, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"alpha.txt", "beta.sh", "gamma"}
	if strings.Join(s.Lines, "|") != strings.Join(want, "|") {
		t.Fatalf("lines %q, want %q", s.Lines, want)
	}
	if s.Seq != term.OutputHead() {
		t.Fatalf("screen at %d, output head %d", s.Seq, term.OutputHead())
	}
	runs, err := term.ReadScreen(ScreenRuns, 0, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(runs.Runs) < 3 || len(runs.Runs[2]) == 0 || runs.Runs[2][0].T != "gamma" {
		t.Fatalf("runs %+v", runs.Runs)
	}
}

func TestSnapshotContinuesAtItsOffset(t *testing.T) {
	reg, dir := screenRegistry(t)
	term := startIn(t, reg, dir, "snap", 40, 10, `i=0; while [ $i -lt 30 ]; do echo "line $i"; i=$((i+1)); done`)
	waitDone(t, term)
	data, info, seq, err := term.Snapshot(5, 0)
	if err != nil {
		t.Fatal(err)
	}
	if seq != term.OutputHead() || info.Cols != 40 || info.Rows != 10 {
		t.Fatalf("seq %d (head %d), %dx%d", seq, term.OutputHead(), info.Cols, info.Rows)
	}
	f := production.Factory(emulator.Options{Cols: 40, Rows: 10, ScrollbackLines: 100, ScrollbackBytes: 1 << 20})
	defer f.Close()
	f.Feed(data)
	_, first := f.History()
	text := f.Text(first, first+100)
	if len(text) != 14 || text[0] != "line 16" || text[13] != "line 29" {
		t.Fatalf("restored %q", text)
	}
	// Cut down to fit: a tiny limit leaves no history, but the screen.
	small, sinfo, _, _ := term.Snapshot(5, 200)
	if len(small) > len(data) || sinfo.FirstAbsRow <= info.FirstAbsRow {
		t.Fatalf("a snapshot over the limit kept its history (%d bytes, first row %d)", len(small), sinfo.FirstAbsRow)
	}
}

func TestWaitFor(t *testing.T) {
	reg, dir := screenRegistry(t)
	term := startIn(t, reg, dir, "wait", 80, 24, `echo starting; sleep 0.3; echo "ready: 42"; sleep 0.6; echo done; sleep 30`)
	ctx := context.Background()

	r, err := term.WaitFor(ctx, WaitSpec{Regex: regexp.MustCompile(`ready: (\d+)`), SinceSeq: -1, Timeout: 5 * time.Second})
	if err != nil || r.Matched != "regex" || r.Match != "ready: 42" {
		t.Fatalf("screen regex: %+v %v", r, err)
	}
	r, err = term.WaitFor(ctx, WaitSpec{Regex: regexp.MustCompile(`done`), Output: true, SinceSeq: 0, Timeout: 5 * time.Second})
	if err != nil || r.Matched != "regex" || r.Match != "done" {
		t.Fatalf("output regex: %+v %v", r, err)
	}
	began := time.Now()
	r, err = term.WaitFor(ctx, WaitSpec{Idle: 300 * time.Millisecond, SinceSeq: -1, Timeout: 5 * time.Second})
	if err != nil || r.Matched != "idle" || time.Since(began) < 300*time.Millisecond {
		t.Fatalf("idle: %+v %v after %s", r, err, time.Since(began))
	}
	r, err = term.WaitFor(ctx, WaitSpec{Regex: regexp.MustCompile(`never`), SinceSeq: -1, Timeout: 200 * time.Millisecond})
	if err != nil || r.Matched != "timeout" {
		t.Fatalf("timeout: %+v %v", r, err)
	}
	go func() {
		time.Sleep(100 * time.Millisecond)
		term.Kill(100 * time.Millisecond)
	}()
	r, err = term.WaitFor(ctx, WaitSpec{Regex: regexp.MustCompile(`never`), SinceSeq: -1, Timeout: 10 * time.Second})
	if err != nil || r.Matched != "exited" {
		t.Fatalf("exit: %+v %v", r, err)
	}
}

// The colours a program asks for are those of the browser that owns the screen.
func TestThemeAnswersColourQueries(t *testing.T) {
	reg, dir := screenRegistry(t)
	term := startIn(t, reg, dir, "colour", 80, 24, `stty raw -echo; sleep 0.3
printf '\033]11;?\033\\'
r=$(head -c 25 | od -An -c | tr -s ' \n' ' ')
stty sane; printf '\nGOT%s\n' "$r"`)
	// The browser that owns the screen says what its colours are when it attaches.
	attachClient(t, term, ClientOptions{}, wire.Attach{Theme: &wire.Theme{FG: "#010203", BG: "rgb(171, 205, 239)"}})
	out := waitOutput(t, term, "GOT")
	if !strings.Contains(out, "a b a b / c d c d / e f e f") {
		t.Fatalf("reply %q", out[strings.Index(out, "GOT"):])
	}
}

func TestPreviewEndsAtTheCursor(t *testing.T) {
	reg, dir := screenRegistry(t)
	term := startIn(t, reg, dir, "preview", 40, 10, `printf 'one\ntwo\n\033[31mthree\033[0m\n$ '; sleep 30`)
	waitOutput(t, term, "$ ")
	var rows [][]emulator.Run
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		rows = term.Preview(2)
		if len(rows) == 2 && len(rows[1]) > 0 && rows[1][0].T == "$" {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if len(rows) != 2 || rows[0][0].T != "three" || rows[0][0].FG != 2 || rows[1][0].T != "$" {
		t.Fatalf("preview %+v", rows)
	}
}
