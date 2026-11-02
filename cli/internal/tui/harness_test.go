package tui

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
)

var update = flag.Bool("update", false, "rewrite the golden frames")

// testClock is a clock tests move by hand, so elapsed times in frames are exact.
type testClock struct{ at time.Time }

func (c *testClock) now() time.Time          { return c.at }
func (c *testClock) advance(d time.Duration) { c.at = c.at.Add(d) }

var testEpoch = time.Date(2026, 11, 2, 14, 30, 0, 0, time.UTC)

type harness struct {
	t     *testing.T
	m     *Model
	fake  *Fake
	clock *testClock
}

// newHarness builds a model on the fake with a fixed clock and size, already holding an open session.
func newHarness(t *testing.T, width, height int, configure ...func(*Options)) *harness {
	t.Helper()
	clock := &testClock{at: testEpoch}
	fake := NewFake("/work/project")
	fake.Now = clock.now
	dark := true
	// Everything a frame could take from the machine is pinned: the clock, the background, colour and
	// character set, and the platform whose markers are drawn. The golden frames are one set for
	// every runner, so nothing in them may depend on where the test runs.
	options := Options{Cwd: "/work/project", Store: &MemoryStore{}, Now: clock.now, Dark: &dark, Platform: "linux"}
	for _, c := range configure {
		c(&options)
	}
	m := New(fake, options)
	m.manual = true
	h := &harness{t: t, m: m, fake: fake, clock: clock}
	h.send(tea.WindowSizeMsg{Width: width, Height: height})
	session, err := fake.Start(m.ctx, options.Start)
	if err != nil {
		t.Fatal(err)
	}
	h.send(startedMsg{session: session})
	// Start announces the link as online from a goroutine; take it now so it cannot land later.
	h.pumpUntil(func(e Event) bool { _, ok := e.(ConnectionChanged); return ok })
	return h
}

// send delivers one message and runs whatever commands it returns, feeding their messages back.
func (h *harness) send(msg tea.Msg) {
	h.t.Helper()
	_, cmd := h.m.Update(msg)
	h.run(cmd, 0)
}

func (h *harness) run(cmd tea.Cmd, depth int) {
	if cmd == nil || depth > 20 {
		return
	}
	msg := cmd()
	switch msg := msg.(type) {
	case nil:
	case tea.BatchMsg:
		for _, c := range msg {
			h.run(c, depth+1)
		}
	case tea.QuitMsg:
		h.m.quitting = true
	default:
		if isProgramMsg(msg) {
			return
		}
		_, next := h.m.Update(msg)
		h.run(next, depth+1)
	}
}

// isProgramMsg reports messages meant for the Bubble Tea program itself (raw output, clipboard,
// screen clears), which a test has no terminal to deliver to.
func isProgramMsg(msg tea.Msg) bool {
	name := reflect.TypeOf(msg).String()
	return strings.HasPrefix(name, "tea.") && !strings.Contains(name, "KeyPress")
}

func (h *harness) key(names ...string) {
	h.t.Helper()
	for _, name := range names {
		h.send(keyMsg(name))
	}
}

func (h *harness) typeText(s string) {
	h.t.Helper()
	for _, r := range s {
		h.send(tea.KeyPressMsg{Code: r, Text: string(r)})
	}
}

// keyMsg builds the key press a terminal would deliver for a name like "enter" or "ctrl+c".
func keyMsg(name string) tea.KeyPressMsg {
	named := map[string]rune{
		"enter": tea.KeyEnter, "esc": tea.KeyEscape, "tab": tea.KeyTab, "backspace": tea.KeyBackspace,
		"up": tea.KeyUp, "down": tea.KeyDown, "left": tea.KeyLeft, "right": tea.KeyRight,
		"pgup": tea.KeyPgUp, "pgdown": tea.KeyPgDown, "space": tea.KeySpace, "end": tea.KeyEnd,
	}
	var mod tea.KeyMod
	base := name
	for {
		switch {
		case strings.HasPrefix(base, "ctrl+"):
			mod |= tea.ModCtrl
			base = strings.TrimPrefix(base, "ctrl+")
			continue
		case strings.HasPrefix(base, "shift+"):
			mod |= tea.ModShift
			base = strings.TrimPrefix(base, "shift+")
			continue
		case strings.HasPrefix(base, "alt+"):
			mod |= tea.ModAlt
			base = strings.TrimPrefix(base, "alt+")
			continue
		}
		break
	}
	if code, ok := named[base]; ok {
		k := tea.KeyPressMsg{Code: code, Mod: mod}
		if code == tea.KeySpace && mod == 0 {
			k.Text = " "
		}
		return k
	}
	r := []rune(base)[0]
	k := tea.KeyPressMsg{Code: r, Mod: mod}
	if mod == 0 {
		k.Text = base
	}
	return k
}

// pumpUntil applies the fake's events until one satisfies done, failing after six seconds.
func (h *harness) pumpUntil(done func(Event) bool) {
	h.t.Helper()
	deadline := time.After(6 * time.Second)
	for {
		select {
		case e := <-h.fake.events:
			h.send(eventMsg{e})
			if done(e) {
				return
			}
		case <-deadline:
			h.t.Fatal("timed out waiting for an event")
		}
	}
}

func (h *harness) pumpTurn() {
	h.t.Helper()
	h.pumpUntil(func(e Event) bool { _, ok := e.(TurnEnded); return ok })
}

func (h *harness) pumpTo(kind Event) {
	h.t.Helper()
	want := reflect.TypeOf(kind)
	h.pumpUntil(func(e Event) bool { return reflect.TypeOf(e) == want })
}

// apply feeds events straight into the model, as the backend would deliver them.
func (h *harness) apply(events ...Event) {
	h.t.Helper()
	for _, e := range events {
		h.send(eventMsg{e})
	}
}

func (h *harness) frame() string {
	return h.m.View().Content
}

func (h *harness) plain() string {
	lines := strings.Split(ansi.Strip(h.frame()), "\n")
	for i, line := range lines {
		lines[i] = strings.TrimRight(line, " ")
	}
	return strings.Join(lines, "\n")
}

// timestamps scrubs clock readings a frame may carry, so a golden frame does not depend on when it
// was rendered even if a clock slips through.
var timestamps = regexp.MustCompile(`\b\d{2}:\d{2}:\d{2}\b`)

// golden compares the plain frame with testdata/<name>.golden, rewriting it under -update.
func (h *harness) golden(name string) {
	h.t.Helper()
	if on, _ := strconv.ParseBool(os.Getenv("RUNEWIDTH_EASTASIAN")); on {
		// The width tables read this once, when the process starts, and count ambiguous characters
		// such as ● as two cells; no test can set it back. The frames are drawn for the default.
		h.t.Skip("RUNEWIDTH_EASTASIAN changes cell widths for the whole process")
	}
	got := timestamps.ReplaceAllString(h.plain(), "hh:mm:ss") + "\n"
	path := filepath.Join("testdata", name+".golden")
	if *update {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			h.t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			h.t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		h.t.Fatalf("missing golden frame %s (run go test -update): %v", path, err)
	}
	// .gitattributes keeps the frames LF in every checkout; a checkout made before that rule, or by
	// a tool that ignores it, must still compare as the same text.
	wantText := strings.ReplaceAll(string(want), "\r\n", "\n")
	if wantText != got {
		h.t.Errorf("frame %s differs from the golden frame: %s\n--- want\n%s\n--- got\n%s", name, firstDifference(wantText, got), wantText, got)
	}
}

// firstDifference names the first line two frames disagree on, quoted so an invisible difference
// (a carriage return, a different marker, a trailing space) shows in a CI log.
func firstDifference(want, got string) string {
	wl, gl := strings.Split(want, "\n"), strings.Split(got, "\n")
	for i := 0; i < max(len(wl), len(gl)); i++ {
		var w, g string
		if i < len(wl) {
			w = wl[i]
		}
		if i < len(gl) {
			g = gl[i]
		}
		if w != g {
			return fmt.Sprintf("line %d: want %q, got %q", i+1, w, g)
		}
	}
	return "no line differs"
}

func (h *harness) contains(want string) {
	h.t.Helper()
	if !strings.Contains(h.plain(), want) {
		h.t.Fatalf("frame lacks %q:\n%s", want, h.plain())
	}
}

func (h *harness) lacks(unwanted string) {
	h.t.Helper()
	if strings.Contains(h.plain(), unwanted) {
		h.t.Fatalf("frame has %q:\n%s", unwanted, h.plain())
	}
}

func pasteMsg(s string) tea.PasteMsg { return tea.PasteMsg{Content: s} }

func writeFile(t *testing.T, dir, name string) {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}
