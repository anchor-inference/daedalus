// Package term is one terminal: a program on a PTY, the output ring, the scanner, the emulator, and
// the writer that arbitrates between humans and agents. And the registry of all of them.
package term

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/answer"
	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/containment"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/logx"
	"github.com/ascorblack/daedalus/ptyd/internal/ptyproc"
	"github.com/ascorblack/daedalus/ptyd/internal/ring"
	"github.com/ascorblack/daedalus/ptyd/internal/scan"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

// readBytes is one read from the PTY.
const readBytes = 32 << 10

// emulatorQueue bounds the chunks waiting for the emulator. When it is full the reader waits, the
// kernel's PTY buffer fills, and the program blocks on its next write: a hostile or merely fast
// stream slows its own program down instead of growing the daemon.
const emulatorQueue = 32

// drainAfterExit is how long output is still read after the program exits. A background job that
// keeps the PTY open would otherwise keep a finished terminal "running" forever; closing the master
// hangs it up, as closing a terminal window does.
const drainAfterExit = 500 * time.Millisecond

// Publisher receives the terminal's events. The daemon passes its debouncer.
type Publisher interface {
	Publish(typ, terminalID string, data any)
	Flush(terminalID string)
}

// Answerer produces the reply to a terminal query from the emulator's state and what is known of the
// person whose screen sets the size, or nil to stay silent. A build without one answers nothing.
type Answerer func(q scan.Mark, e emulator.Emulator, o answer.Owner) []byte

// Deps are what every terminal shares.
type Deps struct {
	Emulator  emulator.Factory
	Events    Publisher
	Journal   *logx.Journal
	Clock     Clock
	Log       *slog.Logger
	Answer    Answerer
	KillGrace time.Duration
}

// Spec is a terminal about to start, already validated and resolved.
type Spec struct {
	ID          string
	Path        string   // the resolved executable
	Argv        []string // as it will be passed, argv[0] included
	Cwd         string
	CwdFallback bool
	Env         []string
	Cols, Rows  int
	Title       string
	RingBytes   int
	LogPath     string // an on-disk copy of the output, or "" for none
	InputIdle   time.Duration
	LaunchID    string
	Labels      map[string]string
	Shell       string // the shell, when the terminal runs the login shell
	// Nonce is the launch's shell-integration nonce: only shell marks that carry it are believed.
	// Empty, no mark is. Integration names the integration the launch loaded, or "".
	Nonce       string
	Integration string
	// Sandbox is a program wrapped in bubblewrap: Path and Argv are bubblewrap's, and Program is
	// what it runs, which is what the terminal reports as its argv.
	Sandbox     bool
	Program     []string
	Containment containment.Handle
}

// ModesInfo is the part of the emulator's modes that clients and adapters act on.
type ModesInfo struct {
	AltScreen      bool `json:"alt_screen"`
	BracketedPaste bool `json:"bracketed_paste"`
	Mouse          bool `json:"mouse"`
	AppCursor      bool `json:"app_cursor"`
	MouseMode      int  `json:"mouse_mode,omitempty"`
	KittyFlags     int  `json:"kitty_flags,omitempty"`
}

func modesInfo(m emulator.Modes) ModesInfo {
	return ModesInfo{AltScreen: m.AltScreen, BracketedPaste: m.BracketedPaste, Mouse: m.Mouse.Mode != 0,
		AppCursor: m.AppCursor, MouseMode: m.Mouse.Mode, KittyFlags: m.KittyFlags}
}

// Command is the last command shell integration reported.
type Command struct {
	Command  string    `json:"command,omitempty"`
	ExitCode *int      `json:"exit_code"`
	At       time.Time `json:"at"`
}

// Terminal is one running (or exited, and not yet forgotten) terminal.
type Terminal struct {
	ID          string
	Pid         int
	Argv        []string
	Shell       string
	CreatedAt   time.Time
	LaunchID    string
	Labels      map[string]string
	Sandbox     bool
	cwdFallback bool

	deps        Deps
	proc        *ptyproc.Proc
	containment containment.Handle
	ring        *ring.Ring
	in          *input
	disk        *logx.Rotating

	vt     chan vtRequest
	vtQuit chan struct{}
	vtOnce sync.Once

	readerDone chan struct{}
	done       chan struct{} // closed once the exit is recorded and published

	att    attachments
	sizeMu sync.Mutex   // serialises size changes, which wait for the emulator
	fed    atomic.Int64 // the output offset the emulator has consumed, which a snapshot is taken at
	// theme is the viewer's theme last handed to the emulator; the emulator goroutine's own.
	theme wire.Theme

	mu            sync.Mutex
	title         string
	cwd           string
	cols, rows    int
	pxW, pxH      int
	status        string
	exit          ptyproc.Exit
	exitedAt      time.Time
	lastOutput    time.Time
	lastInput     time.Time
	lastResizeSeq int64
	sizeOwner     string
	modes         ModesInfo
	lastCommand   *Command
	cmds          commandLog
}

type vtRequest struct {
	data  []byte
	marks []scan.Mark
	base  int64 // the output offset of data[0]
	fn    func(emulator.Emulator)
	done  chan struct{}
}

// Start spawns spec and begins reading its output.
func Start(spec Spec, deps Deps) (*Terminal, error) {
	var disk *logx.Rotating
	if spec.LogPath != "" {
		var err error
		if disk, err = logx.OpenRotating(spec.LogPath, config.LogToDiskBytes, 2); err != nil {
			return nil, err
		}
	}
	tag := "DAEDALUS_TERMINAL_ID=" + spec.ID
	program := ptyproc.Spec{Path: spec.Path, Argv: spec.Argv, Dir: spec.Cwd, Env: spec.Env,
		Cols: spec.Cols, Rows: spec.Rows, Tag: tag, Wrapped: spec.Sandbox}
	if spec.Containment != nil {
		program.UseContainment, program.ContainmentFD = true, spec.Containment.FD()
	}
	proc, err := ptyproc.Start(program)
	if err != nil {
		if disk != nil {
			disk.Close()
		}
		return nil, err
	}
	now := deps.Clock.Now().UTC()
	argv := spec.Argv
	if spec.Program != nil {
		argv = spec.Program
	}
	t := &Terminal{
		ID: spec.ID, Pid: proc.Pid, Argv: argv, Sandbox: spec.Sandbox, Shell: spec.Shell, CreatedAt: now, LaunchID: spec.LaunchID,
		Labels: spec.Labels, cwdFallback: spec.CwdFallback,
		deps: deps, proc: proc, containment: spec.Containment, ring: ring.New(spec.RingBytes), disk: disk,
		vt: make(chan vtRequest, emulatorQueue), vtQuit: make(chan struct{}),
		readerDone: make(chan struct{}), done: make(chan struct{}),
		title: spec.Title, cwd: spec.Cwd, cols: spec.Cols, rows: spec.Rows, status: "running", sizeOwner: "host",
		cmds: commandLog{nonce: spec.Nonce, integration: spec.Integration},
	}
	t.in = newInput(proc.Master, deps.Clock, spec.InputIdle, t.ring.Head)
	t.in.onDelivered = t.delivered
	t.in.onKeyboard = t.keyboardChanged
	t.deps.Events = attachPublisher{inner: deps.Events, t: t}
	emu := deps.Emulator(emulator.Options{Cols: spec.Cols, Rows: spec.Rows, ScrollbackLines: config.ScrollbackLines,
		ScrollbackBytes: config.ScrollbackBytes, GraphemeClusters: true})
	// Published before the reader starts, so no event of the terminal's output can precede it.
	deps.Events.Publish("terminal.created", t.ID, map[string]any{
		"pid": t.Pid, "argv": t.Argv, "cwd": spec.Cwd, "labels": t.Labels, "launch_id": t.LaunchID,
	})
	go t.emulate(emu)
	go t.in.run()
	go t.read()
	go t.supervise()
	return t, nil
}

// read is the reader goroutine: PTY → scanner (clamps and marks) → ring → emulator queue. It never
// waits for a client; it waits only for the emulator, whose queue is bounded.
func (t *Terminal) read() {
	defer close(t.readerDone)
	sc := scan.New()
	buf := make([]byte, readBytes)
	for {
		n, err := t.proc.Master.Read(buf)
		if n > 0 {
			out, marks, ok := t.scan(sc, buf[:n])
			if !ok {
				// The scanner's state is suspect after a panic; start over with a fresh one. The chunk
				// is lost, which is better than passing on bytes nobody clamped.
				sc = scan.New()
				continue
			}
			if len(out) > 0 || len(marks) > 0 {
				base := t.ring.Head()
				t.ring.Write(out)
				if t.disk != nil {
					_, _ = t.disk.Write(out)
				}
				t.mu.Lock()
				t.lastOutput = t.deps.Clock.Now().UTC()
				t.mu.Unlock()
				t.att.notify()
				req := vtRequest{data: append([]byte(nil), out...), base: base}
				if len(marks) > 0 {
					req.marks = append([]scan.Mark(nil), marks...)
				}
				select {
				case t.vt <- req:
				case <-t.vtQuit:
				}
			}
		}
		if err != nil {
			return
		}
	}
}

// scan runs the scanner, turning a panic into a logged, dropped chunk. The scanner reads bytes any
// program can write; a bug in it must cost one chunk of one terminal, not the daemon and every
// terminal it holds.
func (t *Terminal) scan(sc *scan.Scanner, p []byte) (out []byte, marks []scan.Mark, ok bool) {
	defer func() {
		if r := recover(); r != nil {
			t.deps.Log.Error("scanner panicked; chunk dropped", "terminal", t.ID, "panic", fmt.Sprint(r))
			ok = false
		}
	}()
	out, marks = sc.Scan(p)
	return out, marks, true
}

// emulate is the only goroutine that touches the emulator. Feeds, resizes and questions are served
// in the order they were asked, which makes every answer consistent with an exact output offset.
func (t *Terminal) emulate(e emulator.Emulator) {
	defer e.Close()
	before := e.Modes()
	for {
		select {
		case <-t.vtQuit:
			return
		case req := <-t.vt:
			before = t.serve(e, req, before)
		}
	}
}

// serve handles one request on the emulator goroutine. A panic in the emulator or in a question
// asked of it is logged and the request is abandoned; the goroutine carries on, because it is the
// only one that may touch the emulator and a terminal without it could never be read again.
func (t *Terminal) serve(e emulator.Emulator, req vtRequest, before emulator.Modes) (after emulator.Modes) {
	after = before
	defer func() {
		if r := recover(); r != nil {
			t.deps.Log.Error("emulator panicked", "terminal", t.ID, "panic", fmt.Sprint(r))
		}
		if req.done != nil {
			close(req.done)
		}
	}()
	if req.fn != nil {
		req.fn(e)
		return after
	}
	modesMayChange := false
	at := 0
	for _, m := range req.marks {
		// Feed up to the mark, so that what the mark reports (the cursor row of a prompt, the modes a
		// query is answered from) is the state at that exact point of the stream.
		if m.Offset > at {
			e.Feed(req.data[at:m.Offset])
			at = m.Offset
		}
		t.onMark(e, m, req.base+int64(m.Offset))
		switch m.Kind {
		case scan.KindMode, scan.KindKitty, scan.KindReset:
			modesMayChange = true
		}
	}
	if at < len(req.data) {
		e.Feed(req.data[at:])
	}
	t.fed.Store(req.base + int64(len(req.data)))
	if modesMayChange {
		now := e.Modes()
		if modesInfo(now) != modesInfo(before) {
			t.mu.Lock()
			t.modes = modesInfo(now)
			t.mu.Unlock()
			t.deps.Events.Publish("terminal.mode", t.ID, modesInfo(now))
		}
		after = now
	}
	return after
}

// onMark turns a mark into state and events. It runs on the emulator goroutine.
func (t *Terminal) onMark(e emulator.Emulator, m scan.Mark, seq int64) {
	pub := t.deps.Events.Publish
	switch m.Kind {
	case scan.KindTitle:
		t.mu.Lock()
		t.title = m.Text
		t.mu.Unlock()
		pub("terminal.title", t.ID, map[string]any{"title": m.Text, "seq": seq})
	case scan.KindCwd:
		t.mu.Lock()
		t.cwd = m.Text
		t.mu.Unlock()
		pub("terminal.cwd", t.ID, map[string]any{"cwd": m.Text, "seq": seq})
	case scan.KindBell:
		pub("terminal.bell", t.ID, map[string]any{"seq": seq})
	case scan.KindNotify:
		pub("terminal.notify", t.ID, map[string]any{"title": m.Title, "body": m.Text, "seq": seq})
	case scan.KindProgress:
		pub("terminal.progress", t.ID, map[string]any{"state": m.State, "value": m.Value, "seq": seq})
	case scan.KindPrompt, scan.KindVscode:
		// Marks without the launch's nonce are ignored: they are output replayed from somewhere
		// else, a nested shell's, or a program imitating a shell.
		if t.verifiedMark(m) {
			t.onShellMark(e, m, seq)
		}
	case scan.KindQuery:
		if t.deps.Answer != nil {
			facts := t.OwnerFacts()
			t.applyTheme(e, facts.Theme)
			if reply := t.deps.Answer(m, e, answer.Owner{PxW: facts.PxW, PxH: facts.PxH}); len(reply) > 0 {
				t.in.Reply(reply)
				t.answered(seq, reply)
			}
		}
	}
}

// WithEmulator runs fn on the emulator goroutine and waits for it. It fails once the terminal has
// been forgotten.
func (t *Terminal) WithEmulator(fn func(emulator.Emulator)) error {
	done := make(chan struct{})
	select {
	case t.vt <- vtRequest{fn: fn, done: done}:
	case <-t.vtQuit:
		return ErrForgotten
	}
	select {
	case <-done:
		return nil
	case <-t.vtQuit:
		return ErrForgotten
	}
}

// supervise waits for the program to exit, lets the output drain, and records the exit.
func (t *Terminal) supervise() {
	exit := t.proc.Wait()
	select {
	case <-t.readerDone:
	case <-time.After(drainAfterExit):
	}
	_ = t.proc.Close()
	<-t.readerDone
	t.in.Close()
	// Every chunk read so far is queued ahead of this barrier, so the events it produced are
	// published before the exit.
	_ = t.WithEmulator(func(emulator.Emulator) {})
	t.deps.Events.Flush(t.ID)
	head := t.ring.Head()
	t.mu.Lock()
	t.status = "exited"
	t.exit = exit
	t.exitedAt = t.deps.Clock.Now().UTC()
	t.mu.Unlock()
	t.ring.Shrink(config.ExitedRingBytes)
	if t.disk != nil {
		t.disk.Close()
	}
	data := map[string]any{"exit_code": exit.Code, "signal": nullable(exit.Signal), "seq": head}
	t.deps.Events.Publish("terminal.exited", t.ID, data)
	t.deps.Log.Info("terminal exited", "terminal", t.ID, "pid", t.Pid, "exit_code", exit.Code, "signal", exit.Signal)
	close(t.done)
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// Done is closed once the terminal has exited and its exit event is published.
func (t *Terminal) Done() <-chan struct{} { return t.done }

// Running reports whether the program is still running.
func (t *Terminal) Running() bool {
	select {
	case <-t.done:
		return false
	default:
		return true
	}
}

// Exit returns how the program ended; ok is false while it runs.
func (t *Terminal) Exit() (ptyproc.Exit, bool) {
	if t.Running() {
		return ptyproc.Exit{}, false
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.exit, true
}

// forget stops the emulator goroutine and releases what is left. Only an exited terminal is
// forgotten.
func (t *Terminal) forget() {
	t.closeAttachments()
	t.vtOnce.Do(func() { close(t.vtQuit) })
	if t.containment != nil {
		_ = t.containment.Close()
	}
}

// Errors of terminal operations.
var (
	ErrForgotten = errors.New("the terminal has been forgotten")
)

// HumanInput writes a human's keystrokes. They are never recorded anywhere.
func (t *Terminal) HumanInput(p []byte) {
	if !t.Running() {
		return
	}
	t.mu.Lock()
	t.lastInput = t.deps.Clock.Now().UTC()
	t.mu.Unlock()
	t.in.Human(p)
}

// Origin names who an agent write is from, for the journal.
type Origin struct {
	Actor    string
	LaunchID string
	Note     string
}

// AgentWrite delivers data under arbitration (when wait is set) and journals it once delivered.
func (t *Terminal) AgentWrite(ctx context.Context, kind string, data []byte, origin Origin, wait bool, timeout time.Duration) (Receipt, error) {
	if !t.Running() {
		return Receipt{}, ErrExited
	}
	ctx = context.WithValue(ctx, writeMeta{}, agentMeta{kind: kind, origin: origin})
	return t.in.Agent(ctx, data, wait, timeout)
}

type writeMeta struct{}

type agentMeta struct {
	kind   string
	origin Origin
}

// delivered runs on the writer goroutine after an agent write reached the PTY.
func (t *Terminal) delivered(a *agentWrite) {
	t.mu.Lock()
	t.lastInput = a.receipt.DeliveredAt
	t.mu.Unlock()
	meta, _ := a.ctx.Value(writeMeta{}).(agentMeta)
	err := t.deps.Journal.Record(logx.AgentWrite{
		At: a.receipt.DeliveredAt, Terminal: t.ID, Actor: meta.origin.Actor, LaunchID: meta.origin.LaunchID,
		Note: meta.origin.Note, Kind: meta.kind,
	}, a.data)
	if err != nil {
		t.deps.Log.Warn("agent write journal", "terminal", t.ID, "error", err.Error())
	}
	t.agentTyped(meta.origin.Actor)
}

// Modes returns the emulator's current modes, asked on its goroutine so they reflect every byte
// read so far.
func (t *Terminal) Modes() emulator.Modes {
	var m emulator.Modes
	_ = t.WithEmulator(func(e emulator.Emulator) { m = e.Modes() })
	return m
}

// Keyboard returns the keyboard state.
func (t *Terminal) Keyboard() KeyboardState { return t.in.Keyboard() }

// SetKeyboard changes who holds the keyboard.
func (t *Terminal) SetKeyboard(owner string, ttl time.Duration) KeyboardState {
	return t.in.SetKeyboard(owner, ttl)
}

// Resize applies a size as the host, which becomes the size owner until a client claims it.
func (t *Terminal) Resize(cols, rows, pxW, pxH int) error {
	return t.setSize(Size{Cols: cols, Rows: rows, PxW: pxW, PxH: pxH}, nil)
}

// Signal sends sig to the foreground job (group) or to the program alone.
func (t *Terminal) Signal(sig syscall.Signal, group bool) error {
	if !t.Running() {
		return ErrExited
	}
	if group {
		return t.proc.SignalForeground(sig)
	}
	return t.proc.Signal(sig)
}

// Kill ends the terminal and everything it started, and waits until the exit is published.
func (t *Terminal) Kill(grace time.Duration) ptyproc.Exit {
	if t.Running() {
		t.proc.KillTree(grace)
		<-t.done
	}
	e, _ := t.Exit()
	return e
}

// ReadOutput reads the ring from since. With strip, escape sequences are removed and the text is
// cut at a character boundary so a following read resumes cleanly.
func (t *Terminal) ReadOutput(since int64, max int, strip bool) (data []byte, from, to int64, gap bool) {
	data, from, gap = t.ring.ReadFrom(since, max)
	to = from + int64(len(data))
	if strip {
		// Do not end in the middle of a UTF-8 character unless the read was cut short by nothing.
		if cut := incompleteTail(data); cut > 0 && to < t.ring.Head() {
			data = data[:len(data)-cut]
			to -= int64(cut)
		}
		data = scan.Strip(data)
	}
	return data, from, to, gap
}

// incompleteTail is the number of bytes at the end of p that begin a UTF-8 character not finished
// within p.
func incompleteTail(p []byte) int {
	for i := 1; i <= 3 && i <= len(p); i++ {
		b := p[len(p)-i]
		if b&0xc0 == 0x80 {
			continue // a continuation byte: look further back
		}
		need := 1
		switch {
		case b&0xe0 == 0xc0:
			need = 2
		case b&0xf0 == 0xe0:
			need = 3
		case b&0xf8 == 0xf0:
			need = 4
		}
		if need > i {
			return i
		}
		return 0
	}
	return 0
}

// OutputHead is the output offset one past the last byte read.
func (t *Terminal) OutputHead() int64 { return t.ring.Head() }

func isExecutable(path string) bool {
	st, err := os.Stat(path)
	return err == nil && !st.IsDir() && st.Mode()&0o111 != 0
}
