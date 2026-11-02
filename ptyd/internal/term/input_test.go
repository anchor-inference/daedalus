package term

import (
	"bytes"
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
)

// syncBuffer is a writer the test can read while the writer goroutine writes.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func newTestInput(t *testing.T) (*input, *syncBuffer, *FakeClock) {
	t.Helper()
	out := &syncBuffer{}
	clock := NewFakeClock()
	in := newInput(out, clock, 10*time.Second, func() int64 { return 0 })
	go in.run()
	t.Cleanup(in.Close)
	return in, out, clock
}

// waitFor polls cond; the writer goroutine runs on its own schedule even under a fake clock.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(time.Millisecond)
	}
}

type result struct {
	r   Receipt
	err error
}

func agentAsync(in *input, text string, wait bool, timeout time.Duration) chan result {
	ch := make(chan result, 1)
	go func() {
		r, err := in.Agent(context.Background(), []byte(text), wait, timeout)
		ch <- result{r, err}
	}()
	return ch
}

func TestAgentWaitsForHumanIdle(t *testing.T) {
	in, out, clock := newTestInput(t)
	in.Human([]byte("ls"))
	waitFor(t, "human input", func() bool { return out.String() == "ls" })

	done := agentAsync(in, "AGENT", true, time.Minute)
	waitFor(t, "the writer to sleep", func() bool { return clock.Timers() == 1 })
	clock.Advance(9 * time.Second)
	time.Sleep(20 * time.Millisecond)
	if out.String() != "ls" {
		t.Fatalf("agent wrote %q before the idle time passed", out.String())
	}
	waitFor(t, "the writer to sleep again", func() bool { return clock.Timers() == 1 })
	clock.Advance(time.Second)
	r := <-done
	if r.err != nil || out.String() != "lsAGENT" {
		t.Fatalf("after the idle time: %q, %v", out.String(), r.err)
	}
	if r.r.QueuedMs != 10000 || r.r.Bytes != 5 {
		t.Fatalf("receipt %+v", r.r)
	}
}

func TestHumanKeyboardBlocksAgentUntilTimeout(t *testing.T) {
	in, out, clock := newTestInput(t)
	in.SetKeyboard(OwnerHuman, 0)
	done := agentAsync(in, "AGENT", true, 5*time.Second)
	waitFor(t, "the writer to sleep", func() bool { return clock.Timers() == 1 })
	clock.Advance(6 * time.Second)
	r := <-done
	if !errors.Is(r.err, ErrKeyboardHeld) || out.String() != "" {
		t.Fatalf("held keyboard: %q, %v", out.String(), r.err)
	}
}

func TestHumanGrantExpires(t *testing.T) {
	in, out, clock := newTestInput(t)
	in.SetKeyboard(OwnerHuman, 3*time.Second)
	done := agentAsync(in, "A", true, time.Minute)
	waitFor(t, "the writer to sleep", func() bool { return clock.Timers() == 1 })
	clock.Advance(3 * time.Second)
	if r := <-done; r.err != nil || out.String() != "A" {
		t.Fatalf("after the grant: %q, %v", out.String(), r.err)
	}
	if k := in.Keyboard(); k.Owner != OwnerAuto {
		t.Fatalf("expired grant shows as %+v", k)
	}
}

func TestAgentGrantSkipsTheWaitAndHumanTypingEndsIt(t *testing.T) {
	in, out, clock := newTestInput(t)
	in.Human([]byte("x"))
	in.SetKeyboard(OwnerAgent, 0)
	if r := <-agentAsync(in, "A", true, time.Minute); r.err != nil {
		t.Fatal(r.err)
	}
	waitFor(t, "both writes", func() bool { return out.String() == "xA" })
	in.Human([]byte("y"))
	if k := in.Keyboard(); k.Owner != OwnerAuto {
		t.Fatalf("human typing left the keyboard with %q", k.Owner)
	}
	done := agentAsync(in, "B", true, time.Minute)
	waitFor(t, "the writer to sleep", func() bool { return clock.Timers() == 1 })
	if out.String() != "xAy" {
		t.Fatalf("agent went through after human typing: %q", out.String())
	}
	clock.Advance(10 * time.Second)
	<-done
}

func TestWaitNoneGoesAtOnce(t *testing.T) {
	in, out, _ := newTestInput(t)
	in.SetKeyboard(OwnerHuman, 0)
	if r := <-agentAsync(in, "now", false, time.Second); r.err != nil || out.String() != "now" {
		t.Fatalf("wait none: %q, %v", out.String(), r.err)
	}
}

func TestHumanInputInterleavesWithLargeAgentWrite(t *testing.T) {
	block := make(chan struct{})
	w := &gatedWriter{gate: block}
	clock := NewFakeClock()
	in := newInput(w, clock, 0, func() int64 { return 0 })
	go in.run()
	defer in.Close()
	big := bytes.Repeat([]byte("a"), 3*chunkBytes)
	done := agentAsync(in, string(big), true, time.Minute)
	waitFor(t, "the first chunk", func() bool { return w.writes() == 1 })
	in.Human([]byte("H"))
	close(block)
	<-done
	got := w.String()
	i := bytes.IndexByte([]byte(got), 'H')
	if i != chunkBytes {
		t.Fatalf("human byte at %d, want right after the first chunk (%d)", i, chunkBytes)
	}
}

type gatedWriter struct {
	syncBuffer
	gate chan struct{}
	mu   sync.Mutex
	n    int
}

func (g *gatedWriter) Write(p []byte) (int, error) {
	g.mu.Lock()
	g.n++
	first := g.n == 1
	g.mu.Unlock()
	n, err := g.syncBuffer.Write(p)
	if first {
		<-g.gate
	}
	return n, err
}

func (g *gatedWriter) writes() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.n
}

func TestCloseFailsQueuedWrites(t *testing.T) {
	in, _, clock := newTestInput(t)
	in.SetKeyboard(OwnerHuman, 0)
	done := agentAsync(in, "A", true, time.Hour)
	waitFor(t, "the writer to sleep", func() bool { return clock.Timers() == 1 })
	in.Close()
	if r := <-done; !errors.Is(r.err, ErrExited) {
		t.Fatalf("closed: %v", r.err)
	}
	if _, err := in.Agent(context.Background(), []byte("x"), false, time.Second); !errors.Is(err, ErrExited) {
		t.Fatalf("after close: %v", err)
	}
}

func TestPaste(t *testing.T) {
	on := emulator.Modes{BracketedPaste: true}
	got := string(EncodePaste("echo a\x1b[201~; rm -rf x\n", on))
	if got != "\x1b[200~echo a; rm -rf x\n\x1b[201~" {
		t.Fatalf("bracketed: %q", got)
	}
	// Removing one marker must not assemble another out of the pieces around it.
	got = string(EncodePaste("\x1b[20\x1b[201~1~tail", on))
	if got != "\x1b[200~tail\x1b[201~" {
		t.Fatalf("nested marker: %q", got)
	}
	if got := string(EncodePaste("a\nb\r\nc", emulator.Modes{})); got != "a\rb\rc" {
		t.Fatalf("unbracketed: %q", got)
	}
}

func TestKeys(t *testing.T) {
	normal, app := emulator.Modes{}, emulator.Modes{AppCursor: true}
	cases := []struct {
		keys []string
		m    emulator.Modes
		want string
	}{
		{[]string{"Up", "Down", "Left", "Right", "Home", "End"}, normal, "\x1b[A\x1b[B\x1b[D\x1b[C\x1b[H\x1b[F"},
		{[]string{"Up", "Down", "Left", "Right", "Home", "End"}, app, "\x1bOA\x1bOB\x1bOD\x1bOC\x1bOH\x1bOF"},
		{[]string{"Enter", "Tab", "S-Tab", "Esc", "Backspace"}, normal, "\r\t\x1b[Z\x1b\x7f"},
		{[]string{"C-c", "C-d", "C-A", "C-[", "C-@", "C-?"}, normal, "\x03\x04\x01\x1b\x00\x7f"},
		{[]string{"M-b", "M-Enter", "M-Up"}, app, "\x1bb\x1b\r\x1b\x1bOA"},
		{[]string{"F1", "F5", "F12", "PgUp", "Delete"}, normal, "\x1bOP\x1b[15~\x1b[24~\x1b[5~\x1b[3~"},
	}
	for _, c := range cases {
		got, err := EncodeKeys(c.keys, c.m)
		if err != nil || string(got) != c.want {
			t.Errorf("%v: %q, %v; want %q", c.keys, got, err, c.want)
		}
	}
	if _, err := EncodeKeys([]string{"Hyper-x"}, normal); err == nil {
		t.Error("an unknown key was accepted")
	}
}
