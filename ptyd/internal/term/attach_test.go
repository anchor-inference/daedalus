//go:build unix

package term

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/answer"
	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/fake"
	"github.com/ascorblack/daedalus/ptyd/internal/logx"
	"github.com/ascorblack/daedalus/ptyd/internal/scan"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// sink is a client's side of an attachment: it keeps every frame, and acknowledges or not as the
// test says.
type sink struct {
	mu     sync.Mutex
	frames []wire.BrowserFrame
	closed bool
	notify chan struct{}
}

func newSink() *sink { return &sink{notify: make(chan struct{}, 1)} }

func (s *sink) Send(frame []byte) error {
	f, err := wire.DecodeBrowser(frame)
	if err != nil {
		panic(fmt.Sprintf("the daemon sent a malformed frame: %v", err))
	}
	f.Data = append([]byte(nil), f.Data...)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return io.ErrClosedPipe
	}
	s.frames = append(s.frames, f)
	select {
	case s.notify <- struct{}{}:
	default:
	}
	return nil
}

func (s *sink) Close() {
	s.mu.Lock()
	s.closed = true
	s.mu.Unlock()
}

func (s *sink) isClosed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closed
}

func (s *sink) all() []wire.BrowserFrame {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]wire.BrowserFrame(nil), s.frames...)
}

// events returns the decoded events of the given type, in order.
func (s *sink) events(typ string) []map[string]any { return s.eventsFrom(0, typ) }

// mark is a point in the frames received, for waiting on events after it.
func (s *sink) mark() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.frames)
}

func (s *sink) eventsFrom(from int, typ string) []map[string]any {
	var out []map[string]any
	for _, f := range s.all()[from:] {
		if f.Type != wire.TypeEvent {
			continue
		}
		var m map[string]any
		_ = json.Unmarshal(f.Data, &m)
		if m["type"] == typ {
			out = append(out, m)
		}
	}
	return out
}

// until waits for cond over the frames received so far.
func (s *sink) until(t *testing.T, what string, cond func(frames []wire.BrowserFrame) bool) []wire.BrowserFrame {
	t.Helper()
	deadline := time.After(10 * time.Second)
	for {
		frames := s.all()
		if cond(frames) {
			return frames
		}
		select {
		case <-s.notify:
		case <-time.After(20 * time.Millisecond):
		case <-deadline:
			t.Fatalf("waiting for %s; frames: %s", what, describe(frames))
		}
	}
}

func (s *sink) waitEvent(t *testing.T, typ string, match func(map[string]any) bool) map[string]any {
	t.Helper()
	return s.waitEventFrom(t, 0, typ, match)
}

// waitEventFrom waits for an event received after the mark from.
func (s *sink) waitEventFrom(t *testing.T, from int, typ string, match func(map[string]any) bool) map[string]any {
	t.Helper()
	var found map[string]any
	s.until(t, "event "+typ, func([]wire.BrowserFrame) bool {
		for _, e := range s.eventsFrom(from, typ) {
			if match == nil || match(e) {
				found = e
				return true
			}
		}
		return false
	})
	return found
}

// stream reassembles what the client holds: the last snapshot's offset and the OUTPUT after it,
// checking that the frames are contiguous.
func stream(t *testing.T, frames []wire.BrowserFrame) (start int64, data []byte, snapshots int) {
	t.Helper()
	next := int64(-1)
	for _, f := range frames {
		switch f.Type {
		case wire.TypeSnapshot:
			snapshots++
			start, data, next = int64(f.Seq), nil, int64(f.Seq)
		case wire.TypeOutput:
			if next >= 0 && int64(f.Seq) != next {
				t.Fatalf("OUTPUT at %d, expected %d", f.Seq, next)
			}
			if next < 0 {
				start, next = int64(f.Seq), int64(f.Seq)
			}
			data = append(data, f.Data...)
			next += int64(len(f.Data))
		}
	}
	return start, data, snapshots
}

func describe(frames []wire.BrowserFrame) string {
	var b strings.Builder
	for _, f := range frames {
		switch f.Type {
		case wire.TypeEvent:
			fmt.Fprintf(&b, "EVENT %s; ", f.Data)
		case wire.TypeOutput:
			fmt.Fprintf(&b, "OUTPUT %d+%d; ", f.Seq, len(f.Data))
		case wire.TypeSnapshot:
			fmt.Fprintf(&b, "SNAPSHOT %dx%d at %d (%d bytes); ", f.Cols, f.Rows, f.Seq, len(f.Data))
		}
	}
	return b.String()
}

func attachFrame(t *testing.T, a wire.Attach) []byte {
	t.Helper()
	b, err := wire.EncodeAttach(a)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// attachHarness is a registry with the clock and the answerer a test chooses.
type attachHarness struct {
	reg   *Registry
	fakes chan *fake.Emulator
	dir   string
	clock Clock
}

func newAttachHarness(t *testing.T, clock Clock, answer Answerer) *attachHarness {
	t.Helper()
	if clock == nil {
		clock = RealClock{}
	}
	h := &attachHarness{fakes: make(chan *fake.Emulator, 16), dir: t.TempDir(), clock: clock}
	journal, err := logx.OpenRotating(filepath.Join(h.dir, "journal.jsonl"), 1<<20, 2)
	if err != nil {
		t.Fatal(err)
	}
	h.reg = NewRegistry(Deps{
		Emulator: fake.Factory(func(e *fake.Emulator) { h.fakes <- e }),
		Events:   &recorder{}, Journal: logx.NewJournal(journal), Clock: clock,
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Answer: answer, KillGrace: time.Second,
	}, 8)
	t.Cleanup(func() {
		h.reg.Shutdown(100 * time.Millisecond)
		journal.Close()
	})
	return h
}

func (h *attachHarness) start(t *testing.T, id string, ringBytes int, argv ...string) (*Terminal, *fake.Emulator) {
	t.Helper()
	env := BuildEnv(os.Environ(), nil, map[string]string{"PS1": "$ "}, id)
	path, ok := LookPath(argv[0], env, "/")
	if !ok {
		t.Skipf("%s is not installed", argv[0])
	}
	term, err := h.reg.Create(Spec{ID: id, Path: path, Argv: argv, Cwd: h.dir, Env: env, Cols: 80, Rows: 24,
		RingBytes: ringBytes, InputIdle: 0})
	if err != nil {
		t.Fatal(err)
	}
	return term, <-h.fakes
}

func attachClient(t *testing.T, term *Terminal, o ClientOptions, a wire.Attach) (*Client, *sink) {
	t.Helper()
	s := newSink()
	c, err := term.Attach(context.Background(), o, s)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Detach)
	c.Frame(attachFrame(t, a))
	s.waitEvent(t, "hello", nil)
	return c, s
}

func waitHead(t *testing.T, term *Terminal, atLeast int64) int64 {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for term.OutputHead() < atLeast {
		if time.Now().After(deadline) {
			t.Fatalf("output head %d, waiting for %d", term.OutputHead(), atLeast)
		}
		time.Sleep(5 * time.Millisecond)
	}
	return term.OutputHead()
}

func TestHelloCarriesTheWindow(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, _ := h.start(t, "hello", 1<<20, "cat")
	_, s := attachClient(t, term, ClientOptions{Label: "laptop"}, wire.Attach{})
	hello := s.events("hello")[0]
	if hello["ack_bytes"] != float64(config.DefaultAckBytes) || hello["window_bytes"] != float64(config.DefaultWindowBytes) {
		t.Fatalf("hello %v", hello)
	}
	if hello["read_only"] != false || hello["client_id"] == "" {
		t.Fatalf("hello %v", hello)
	}
	size := hello["size"].(map[string]any)
	if size["cols"] != float64(80) || size["rows"] != float64(24) || size["owner"] != "host" {
		t.Fatalf("hello size %v", size)
	}
	if kb := hello["keyboard"].(map[string]any); kb["owner"] != "auto" || kb["until"] != nil {
		t.Fatalf("hello keyboard %v", kb)
	}
	// Nothing held yet: a fresh screen, even with no output.
	resync := s.waitEvent(t, "resync", nil)
	if resync["reason"] != "attach" {
		t.Fatalf("resync %v", resync)
	}
	frames := s.until(t, "a snapshot", func(f []wire.BrowserFrame) bool { _, _, n := stream(t, f); return n == 1 })
	if len(term.Info().Clients) != 1 || term.Info().Clients[0].Label != "laptop" {
		t.Fatalf("clients %+v", term.Info().Clients)
	}
	_ = frames
}

func TestAttachWithAValidLastSeqGetsTheTail(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, _ := h.start(t, "tail", 1<<20, "cat")
	term.HumanInput([]byte("first line\r"))
	head := waitHead(t, term, int64(len("first line\r\nfirst line\r\n")))
	all, _, _, _ := term.ReadOutput(0, 1<<20, false)
	_, s := attachClient(t, term, ClientOptions{}, wire.Attach{LastSeq: 3, HaveState: true})
	frames := s.until(t, "the tail", func(f []wire.BrowserFrame) bool {
		_, data, _ := stream(t, f)
		return len(data) >= int(head-3)
	})
	start, data, snapshots := stream(t, frames)
	if snapshots != 0 || len(s.events("resync")) != 0 {
		t.Fatalf("a snapshot for a client that can be given the tail: %s", describe(frames))
	}
	if start != 3 || !bytes.Equal(data, all[3:]) {
		t.Fatalf("tail from %d is %q, want %q", start, data, all[3:])
	}
	// And it keeps streaming.
	term.HumanInput([]byte("more\r"))
	s.until(t, "live output", func(f []wire.BrowserFrame) bool {
		_, data, _ := stream(t, f)
		return strings.Contains(string(data), "more\r\nmore")
	})
}

func TestAttachGetsASnapshotWhenTheTailCannotBeUsed(t *testing.T) {
	h := newAttachHarness(t, nil, nil)

	t.Run("resized since lastSeq", func(t *testing.T) {
		term, emu := h.start(t, "resized", 1<<20, "cat")
		emu.SetSnapshot([]byte("\x1b[Hscreen"))
		term.HumanInput([]byte("abc\r"))
		waitHead(t, term, 5)
		if err := term.Resize(100, 30, 0, 0); err != nil {
			t.Fatal(err)
		}
		_, s := attachClient(t, term, ClientOptions{}, wire.Attach{LastSeq: 3, HaveState: true})
		if r := s.waitEvent(t, "resync", nil); r["reason"] != "resized" {
			t.Fatalf("resync %v", r)
		}
		frames := s.until(t, "the snapshot", func(f []wire.BrowserFrame) bool { _, _, n := stream(t, f); return n == 1 })
		for _, f := range frames {
			if f.Type == wire.TypeSnapshot {
				if f.Cols != 100 || f.Rows != 30 || string(f.Data) != "\x1b[Hscreen" || int64(f.Seq) != term.fed.Load() {
					t.Fatalf("snapshot %dx%d at %d: %q", f.Cols, f.Rows, f.Seq, f.Data)
				}
			}
		}
	})

	t.Run("lastSeq off the ring", func(t *testing.T) {
		term, _ := h.start(t, "offring", 4096, "sh", "-c", "yes | head -c 20000; exec cat")
		waitHead(t, term, 20000)
		_, s := attachClient(t, term, ClientOptions{}, wire.Attach{LastSeq: 10, HaveState: true})
		if r := s.waitEvent(t, "resync", nil); r["reason"] != "ring" {
			t.Fatalf("resync %v", r)
		}
	})

	t.Run("no state", func(t *testing.T) {
		term, _ := h.start(t, "nostate", 1<<20, "cat")
		term.HumanInput([]byte("abc\r"))
		waitHead(t, term, 5)
		_, s := attachClient(t, term, ClientOptions{}, wire.Attach{LastSeq: 3, HaveState: false})
		if r := s.waitEvent(t, "resync", nil); r["reason"] != "attach" {
			t.Fatalf("resync %v", r)
		}
	})

	t.Run("a backlog larger than a screen is worth", func(t *testing.T) {
		term, _ := h.start(t, "backlog", 8<<20, "sh", "-c", "yes | head -c 3000000; exec cat")
		waitHead(t, term, 3000000)
		_, s := attachClient(t, term, ClientOptions{}, wire.Attach{LastSeq: 10, HaveState: true})
		if r := s.waitEvent(t, "resync", nil); r["reason"] != "backlog" {
			t.Fatalf("resync %v", r)
		}
	})
}

// A client that stops acknowledging never slows the program, receives no more than its window, and
// is given a fresh screen, not the backlog, when it acknowledges again.
func TestAStalledClientNeverHoldsTheProgram(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	const total = 16 << 20
	// The end of the output is found by a word after it, not by counting bytes: on the macOS runner the
	// terminal was twice handed more of `yes` than `head -c` passes on (722 and 2505 bytes more, `y`
	// lines to the end), so a count said the output was over while it was still arriving.
	term, _ := h.start(t, "stall", 1<<20, "sh", "-c", fmt.Sprintf("read x; yes | head -c %d; printf OUTPUT-END; exec cat", total))
	stalled, slow := attachClient(t, term, ClientOptions{Label: "stalled"}, wire.Attach{})
	fastClient, fast := attachClient(t, term, ClientOptions{Label: "fast"}, wire.Attach{})
	var ended atomic.Bool
	fastAcks := make(chan struct{})
	go func() {
		defer close(fastAcks)
		// The fast client acknowledges everything it gets, as a browser that keeps up does.
		for deadline := time.Now().Add(time.Minute); time.Now().Before(deadline); {
			fastClient.Frame(wire.EncodeAck(uint64(fastClient.sent.Load())))
			if ended.Load() && fastClient.sent.Load() >= term.OutputHead() {
				return
			}
			time.Sleep(2 * time.Millisecond)
		}
	}()
	began := time.Now()
	term.HumanInput([]byte("\r"))
	for deadline := time.Now().Add(time.Minute); ; {
		head := term.OutputHead()
		tail, _, _, _ := term.ReadOutput(max(0, head-64), 64, false)
		if bytes.HasSuffix(tail, []byte("OUTPUT-END")) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the output never ended; head %d", head)
		}
		time.Sleep(5 * time.Millisecond)
	}
	ended.Store(true)
	t.Logf("%d bytes through the PTY in %s with a stalled client attached", term.OutputHead(), time.Since(began))
	<-fastAcks
	// The daemon counts a frame as sent just before it hands the frame over, so the acknowledging
	// loop can see everything sent while the last frame is still on its way into the sink. Reading
	// the sink at that moment once found 48597 bytes missing on the Linux runner; wait for them.
	head := term.OutputHead()
	for deadline := time.Now().Add(10 * time.Second); ; {
		if _, data, _ := stream(t, fast.all()); int64(len(data)) >= head || time.Now().After(deadline) {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if _, data, n := stream(t, fast.all()); n != 1 || int64(len(data)) != head {
		t.Fatalf("the client that kept up holds %d of %d bytes after %d snapshots", len(data), head, n)
	}

	_, data, _ := stream(t, slow.all())
	if len(data) > config.DefaultWindowBytes {
		t.Fatalf("the stalled client was sent %d bytes past a %d byte window", len(data), config.DefaultWindowBytes)
	}
	before := len(slow.all())
	// It acknowledges again: a fresh screen, not the megabytes it missed.
	stalled.Frame(wire.EncodeAck(uint64(stalled.sent.Load())))
	frames := slow.until(t, "a resync", func(f []wire.BrowserFrame) bool { return len(slow.events("resync")) >= 2 })
	if r := slow.events("resync")[1]; r["reason"] != "lagged" {
		t.Fatalf("resync %v", r)
	}
	sent := 0
	for _, f := range frames[before:] {
		if f.Type == wire.TypeOutput {
			sent += len(f.Data)
		}
	}
	if sent > config.DefaultWindowBytes {
		t.Fatalf("after acknowledging, the stalled client was sent %d bytes of backlog", sent)
	}
}

func TestSizeGoesToTheLastActiveClient(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, emu := h.start(t, "owner", 1<<20, "cat")
	a, sa := attachClient(t, term, ClientOptions{Label: "a"}, wire.Attach{})
	b, sb := attachClient(t, term, ClientOptions{Label: "b"}, wire.Attach{})

	a.Frame(wire.EncodeResize(90, 30, 900, 600))
	sa.waitEvent(t, "size", func(e map[string]any) bool { return e["owner"] == "you" && e["cols"] == float64(90) })
	ma, mb := sa.mark(), sb.mark()
	b.Frame(wire.EncodeResize(120, 40, 0, 0))
	sa.waitEventFrom(t, ma, "size", func(e map[string]any) bool {
		return e["owner"] == "other" && e["cols"] == float64(120) && e["rows"] == float64(40)
	})
	sb.waitEventFrom(t, mb, "size", func(e map[string]any) bool { return e["owner"] == "you" && e["cols"] == float64(120) })
	if info := term.Info(); info.Cols != 120 || info.Rows != 40 || *info.SizeOwner != "human:"+b.ID {
		t.Fatalf("after B's resize: %dx%d owned by %v", info.Cols, info.Rows, *info.SizeOwner)
	}
	if c, r := emu.Size(); c != 120 || r != 40 {
		t.Fatalf("emulator %dx%d", c, r)
	}
	// A types: it takes the size back, and its own size is applied before its keystroke arrives.
	ma, mb = sa.mark(), sb.mark()
	a.Frame(wire.EncodeInput([]byte("x")))
	sb.waitEventFrom(t, mb, "size", func(e map[string]any) bool { return e["owner"] == "other" && e["cols"] == float64(90) })
	sa.waitEventFrom(t, ma, "size", func(e map[string]any) bool { return e["owner"] == "you" && e["cols"] == float64(90) })
	if info := term.Info(); info.Cols != 90 || info.Rows != 30 || *info.SizeOwner != "human:"+a.ID {
		t.Fatalf("after A's input: %dx%d owned by %v", info.Cols, info.Rows, *info.SizeOwner)
	}
	// The owner leaves: B, the other client with a size, gets it.
	mb = sb.mark()
	a.Detach()
	sb.waitEventFrom(t, mb, "size", func(e map[string]any) bool { return e["owner"] == "you" && e["cols"] == float64(120) })
	if info := term.Info(); info.Cols != 120 || *info.SizeOwner != "human:"+b.ID || len(info.Clients) != 1 || info.LastDetachAt == nil {
		t.Fatalf("after A left: %+v", info)
	}
	// The host resizing takes the size from every client.
	if err := term.Resize(100, 30, 0, 0); err != nil {
		t.Fatal(err)
	}
	sb.waitEvent(t, "size", func(e map[string]any) bool { return e["owner"] == "host" && e["cols"] == float64(100) })
}

func TestTooSmallASizeIsRefused(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, emu := h.start(t, "tiny", 1<<20, "cat")
	c, s := attachClient(t, term, ClientOptions{}, wire.Attach{})
	c.Frame(wire.EncodeResize(9, 5, 0, 0))
	e := s.waitEvent(t, "error", nil)
	if e["code"] != "invalid_size" {
		t.Fatalf("error %v", e)
	}
	c.Frame(wire.EncodeResize(501, 40, 0, 0))
	s.until(t, "a second refusal", func([]wire.BrowserFrame) bool { return len(s.events("error")) == 2 })
	if info := term.Info(); info.Cols != 80 || info.Rows != 24 || *info.SizeOwner != "host" {
		t.Fatalf("after refused sizes: %dx%d owned by %v", info.Cols, info.Rows, *info.SizeOwner)
	}
	if cols, rows := emu.Size(); cols != 80 || rows != 24 {
		t.Fatalf("emulator %dx%d", cols, rows)
	}
	// A refused size is not remembered either: typing does not apply it.
	c.Frame(wire.EncodeInput([]byte("x")))
	time.Sleep(100 * time.Millisecond)
	if info := term.Info(); info.Cols != 80 || *info.SizeOwner != "host" {
		t.Fatalf("typing applied a refused size: %dx%d", info.Cols, info.Rows)
	}
}

func TestReadOnlyClientsNeitherTypeNorResize(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, _ := h.start(t, "ro", 1<<20, "cat")
	viewer, sv := attachClient(t, term, ClientOptions{Kind: KindViewer}, wire.Attach{})
	reader, sr := attachClient(t, term, ClientOptions{}, wire.Attach{ReadOnly: true})
	for _, s := range []*sink{sv, sr} {
		if s.events("hello")[0]["read_only"] != true {
			t.Fatalf("hello %v", s.events("hello")[0])
		}
	}
	viewer.Frame(wire.EncodeInput([]byte("from the viewer\r")))
	reader.Frame(wire.EncodeInput([]byte("from the reader\r")))
	viewer.Frame(wire.EncodeResize(100, 30, 0, 0))
	term.HumanInput([]byte("marker\r"))
	waitOutput(t, term, "marker\r\nmarker")
	out, _, _, _ := term.ReadOutput(0, 1<<20, false)
	if strings.Contains(string(out), "from the") {
		t.Fatalf("a read-only client typed: %q", out)
	}
	if info := term.Info(); info.Cols != 80 {
		t.Fatalf("a viewer resized the terminal to %dx%d", info.Cols, info.Rows)
	}
}

func TestKeyboardGrantHoldsAgentsAndIsBroadcast(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, _ := h.start(t, "kb", 1<<20, "cat")
	_, s := attachClient(t, term, ClientOptions{}, wire.Attach{})
	term.SetKeyboard(OwnerHuman, 400*time.Millisecond)
	e := s.waitEvent(t, "keyboard", func(e map[string]any) bool { return e["owner"] == "human" })
	if e["until"] == nil {
		t.Fatalf("a grant with a ttl has no until: %v", e)
	}
	began := time.Now()
	receipt, err := term.AgentWrite(context.Background(), "text", []byte("agent\r"), Origin{Actor: "orchestrator"}, true, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if waited := time.Since(began); waited < 350*time.Millisecond {
		t.Fatalf("the agent write went through a human grant after %s", waited)
	}
	if receipt.Bytes != 6 {
		t.Fatalf("receipt %+v", receipt)
	}
	// The expiry is announced, and the agent's typing is shown and then cleared.
	s.waitEvent(t, "keyboard", func(e map[string]any) bool { return e["owner"] == "auto" && e["until"] == nil })
	s.waitEvent(t, "agent_typing", func(e map[string]any) bool { return e["actor"] == "orchestrator" && e["active"] == true })
	s.waitEvent(t, "agent_typing", func(e map[string]any) bool { return e["actor"] == "orchestrator" && e["active"] == false })
}

func TestAReplyEchoedByAClientIsDropped(t *testing.T) {
	clock := NewFakeClock()
	da1 := []byte("\x1b[?1;2c")
	reply := func(m scan.Mark, e emulator.Emulator, o answer.Owner) []byte {
		if m.Kind == scan.KindQuery {
			return da1
		}
		return nil
	}
	h := newAttachHarness(t, clock, reply)
	// The program asks, then reads its own input back as output: each answer that reaches it shows
	// up once more in the stream.
	// It asks only once the client is attached and says so: asked earlier, the query would reach
	// the client inside its first snapshot rather than as output it was shown.
	term, _ := h.start(t, "echo", 1<<20, "sh", "-c", `stty -echo; read go; printf 'ready\033[c'; exec cat -v`)
	c, s := attachClient(t, term, ClientOptions{}, wire.Attach{})
	c.Frame(wire.EncodeInput([]byte("go\n")))
	s.until(t, "the query", func(f []wire.BrowserFrame) bool {
		_, data, _ := stream(t, f)
		return bytes.Contains(data, []byte("ready\x1b[c"))
	})
	// The daemon has answered, and the client was shown the query: its own identical answer is
	// taken for an echo.
	deadline := time.Now().Add(5 * time.Second)
	for {
		term.att.mu.Lock()
		n := len(c.pending)
		term.att.mu.Unlock()
		if n == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the answer was never marked as one the client may echo")
		}
		time.Sleep(5 * time.Millisecond)
	}
	c.Frame(wire.EncodeInput(da1))
	c.Frame(wire.EncodeInput([]byte("A\n")))
	waitOutput(t, term, "A\r\n")
	out, _, _, _ := term.ReadOutput(0, 1<<20, false)
	if n := strings.Count(string(out), "^[[?1;2c"); n != 1 {
		t.Fatalf("the program read %d answers, want the daemon's only: %q", n, out)
	}
	// Three seconds later the same bytes are typing, and pass.
	clock.Advance(3 * time.Second)
	c.Frame(wire.EncodeInput(da1))
	c.Frame(wire.EncodeInput([]byte("B\n")))
	waitOutput(t, term, "B\r\n")
	out, _, _, _ = term.ReadOutput(0, 1<<20, false)
	if n := strings.Count(string(out), "^[[?1;2c"); n != 2 {
		t.Fatalf("the program read %d answers after the window, want 2: %q", n, out)
	}
}

func TestAnExitedTerminalShowsItsLastScreenAndTheExit(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, emu := h.start(t, "gone", 1<<20, "sh", "-c", "printf bye; exit 7")
	emu.SetSnapshot([]byte("bye"))
	waitDone(t, term)
	c, s := attachClient(t, term, ClientOptions{}, wire.Attach{})
	if hello := s.events("hello")[0]; hello["terminal"].(map[string]any)["status"] != "exited" {
		t.Fatalf("hello %v", hello)
	}
	exit := s.waitEvent(t, "exit", nil)
	if exit["code"] != float64(7) || exit["signal"] != nil {
		t.Fatalf("exit %v", exit)
	}
	frames := s.all()
	_, _, snapshots := stream(t, frames)
	if snapshots != 1 {
		t.Fatalf("frames %s", describe(frames))
	}
	// Still attached, and typing goes nowhere.
	c.Frame(wire.EncodeInput([]byte("x")))
	if s.isClosed() {
		t.Fatal("the channel closed at the exit")
	}
}

func TestALiveClientGetsEveryByteThenTheExit(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, _ := h.start(t, "live", 1<<20, "sh", "-c", "read x; yes | head -c 300000; exit 3")
	c, s := attachClient(t, term, ClientOptions{}, wire.Attach{})
	// Acknowledge only what was sent, and stop once the program is done: the exit must then arrive
	// on its own, with nothing else waking the client.
	go func() {
		for term.Running() {
			c.Frame(wire.EncodeAck(uint64(c.sent.Load())))
			time.Sleep(time.Millisecond)
		}
		c.Frame(wire.EncodeAck(uint64(c.sent.Load())))
	}()
	c.Frame(wire.EncodeInput([]byte("\r")))
	s.waitEvent(t, "exit", nil)
	frames := s.all()
	start, data, snapshots := stream(t, frames)
	// The ring of an exited terminal keeps only its end; the rest is checked by contiguity.
	tail, from, _, _ := term.ReadOutput(0, 1<<20, false)
	if snapshots != 1 || int64(len(data))+start != term.OutputHead() || !bytes.HasSuffix(data, tail) || from <= start {
		t.Fatalf("the client holds %d bytes from %d, the stream has %d: %s", len(data), start, term.OutputHead(), describe(frames[:min(len(frames), 12)]))
	}
	// The exit is the last frame: nothing of the output comes after it.
	last := frames[len(frames)-1]
	if last.Type != wire.TypeEvent || !strings.Contains(string(last.Data), `"exit"`) {
		t.Fatalf("last frame %s", describe(frames[len(frames)-1:]))
	}
}

func TestTitleAndModesReachClients(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, _ := h.start(t, "title", 1<<20, "sh", "-c", `read x; printf '\033]0;building\007\a'; exec cat`)
	c, s := attachClient(t, term, ClientOptions{}, wire.Attach{})
	c.Frame(wire.EncodeInput([]byte("\r")))
	s.waitEvent(t, "title", func(e map[string]any) bool { return e["title"] == "building" })
	s.waitEvent(t, "bell", nil)
	term.clientEvent("terminal.mode", ModesInfo{AltScreen: true, Mouse: true})
	e := s.waitEvent(t, "mode", nil)
	if e["alt_screen"] != true || e["mouse"] != true || e["bracketed_paste"] != false {
		t.Fatalf("mode %v", e)
	}
}

func TestASnapshotTooLargeForAFrameIsSentEmpty(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, emu := h.start(t, "huge", 1<<20, "cat")
	emu.SetSnapshot(bytes.Repeat([]byte("x"), protowire.MaxPayload))
	_, s := attachClient(t, term, ClientOptions{}, wire.Attach{})
	e := s.waitEvent(t, "error", nil)
	if e["code"] != "snapshot_too_large" {
		t.Fatalf("error %v", e)
	}
	for _, f := range s.all() {
		if f.Type == wire.TypeSnapshot && len(f.Data) != 0 {
			t.Fatalf("a %d byte snapshot", len(f.Data))
		}
	}
}

func TestClientsAreToldWhoElseIsAttached(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, _ := h.start(t, "who", 1<<20, "cat")
	_, sa := attachClient(t, term, ClientOptions{Label: "laptop", Via: "web"}, wire.Attach{})
	b, _ := attachClient(t, term, ClientOptions{Label: "phone"}, wire.Attach{})
	sa.waitEvent(t, "clients", func(e map[string]any) bool {
		others, _ := e["others"].([]any)
		return e["count"] == float64(2) && len(others) == 1 && others[0].(map[string]any)["label"] == "phone"
	})
	ma := sa.mark()
	b.Detach()
	sa.waitEventFrom(t, ma, "clients", func(e map[string]any) bool { return e["count"] == float64(1) })
}

// Clients that go away by every route leave nothing running behind them.
func TestNoGoroutineOutlivesItsClient(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	term, _ := h.start(t, "leak", 1<<20, "cat")
	settle := func() int {
		runtime.GC()
		time.Sleep(50 * time.Millisecond)
		return runtime.NumGoroutine()
	}
	before := settle()
	ctx, cancel := context.WithCancel(context.Background())
	var clients []*Client
	for i := range 12 {
		s := newSink()
		c, err := term.Attach(ctx, ClientOptions{}, s)
		if err != nil {
			t.Fatal(err)
		}
		if i%2 == 0 {
			c.Frame(attachFrame(t, wire.Attach{}))
		}
		clients = append(clients, c)
	}
	if n := len(term.Clients()); n != 12 {
		t.Fatalf("%d clients", n)
	}
	for i, c := range clients[:8] {
		if i%2 == 0 {
			c.Detach()
		} else {
			c.Closed() // the other side closed the channel
		}
	}
	cancel() // the connection of the rest ended
	for _, c := range clients {
		select {
		case <-c.Done():
		case <-time.After(5 * time.Second):
			t.Fatal("a client never finished")
		}
	}
	if n := len(term.Clients()); n != 0 {
		t.Fatalf("%d clients left", n)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		after := settle()
		if after <= before {
			break
		}
		if time.Now().After(deadline) {
			buf := make([]byte, 1<<20)
			t.Fatalf("%d goroutines before, %d after:\n%s", before, after, buf[:runtime.Stack(buf, true)])
		}
	}
	// Past the limit, attaching is refused.
	for range config.MaxClients {
		if _, err := term.Attach(context.Background(), ClientOptions{}, newSink()); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := term.Attach(context.Background(), ClientOptions{}, newSink()); err != ErrTooManyClients {
		t.Fatalf("attach past the limit: %v", err)
	}
	// Forgetting the terminal ends them all.
	term.Kill(100 * time.Millisecond)
	if err := h.reg.Forget(term.ID); err != nil {
		t.Fatal(err)
	}
	deadline = time.Now().Add(5 * time.Second)
	for len(term.Clients()) != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("%d clients of a forgotten terminal", len(term.Clients()))
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// The exit event is published a moment before the terminal reports itself exited. A client woken
// by the event alone would find it still running and wait for the next ping, twenty seconds later.
func TestTheExitArrivesWithoutAnythingElseHappening(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	for i := range 5 {
		term, _ := h.start(t, fmt.Sprintf("quiet%d", i), 1<<20, "sh", "-c", "sleep 0.2; printf x; exit 2")
		_, s := attachClient(t, term, ClientOptions{}, wire.Attach{})
		began := time.Now()
		e := s.waitEvent(t, "exit", nil)
		if e["code"] != float64(2) || time.Since(began) > 5*time.Second {
			t.Fatalf("exit %v after %s", e, time.Since(began))
		}
	}
}
