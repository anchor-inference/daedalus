// Package ptytest drives a terminal program the way a person's terminal would, and records what it
// draws.
//
// A program runs in a real pseudo-terminal, so it sees a TTY, a window size and resize signals,
// and everything it writes is kept with a timestamp and fed into an emulator that reconstructs the
// screen. Tests then ask two kinds of question: what is on the screen now (Screen, WaitFor), and how
// did the program get it there (Analyze over the recording: full clears, erase sequences, the share
// of bytes inside synchronized updates). The second kind is what tells flicker apart from a correct
// final frame, which a screen comparison alone cannot do.
//
// The emulator also answers the program's terminal queries (device attributes, cursor position,
// colours), because a real terminal does and a program that waits for an answer would otherwise
// stall in a way no person ever sees.
package ptytest

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/charmbracelet/x/ansi"
	"github.com/charmbracelet/x/vt"
	"github.com/creack/pty"
)

// Chunk is one read from the program's output, as it arrived.
type Chunk struct {
	At   time.Duration
	Data []byte
}

// Recording is everything the program wrote, in order, with the size it was drawn at.
type Recording struct {
	Cols, Rows int
	Chunks     []Chunk
	// Resizes are the moments the window size changed, for the cast and the analysis.
	Resizes []Resize
}

// Resize is a window size change during a recording.
type Resize struct {
	At         time.Duration
	Cols, Rows int
}

// Term is a program running in a pseudo-terminal, with the screen it has drawn.
type Term struct {
	cmd   *exec.Cmd
	ptmx  *os.File
	start time.Time

	mu        sync.Mutex
	emulator  *vt.Emulator
	recording Recording
	changed   chan struct{}

	done    chan struct{}
	waitErr error
	closed  bool
}

// Option changes how Start emulates the terminal.
type Option func(*options)

type options struct {
	noSync bool
}

// WithoutSyncSupport makes the emulated terminal deny synchronized output (mode 2026), as tmux
// 3.4 and older terminals do, so the program's fallback drawing can be measured.
func WithoutSyncSupport() Option { return func(o *options) { o.noSync = true } }

// Start runs cmd in a pseudo-terminal of the given size. With a non-nil t the program is closed
// when the test ends, so a failing test cannot leave it running.
//
// The emulated terminal claims synchronized output (mode 2026) when asked, as kitty, ghostty,
// WezTerm, iTerm2, foot and Windows Terminal do; the emulator itself does not know the mode, and a
// renderer that only enables it after a positive answer would otherwise never be measured with it.
func Start(t testing.TB, cmd *exec.Cmd, cols, rows int, opts ...Option) (*Term, error) {
	var o options
	for _, opt := range opts {
		opt(&o)
	}
	if cmd.Env == nil {
		cmd.Env = os.Environ()
	}
	cmd.Env = append(cmd.Env, "TERM=xterm-256color", "COLORTERM=truecolor")
	ptmx, err := pty.StartWithSize(cmd, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
	if errors.Is(err, pty.ErrUnsupported) && t != nil {
		// Windows has ConPTY, not the Unix pseudo-terminal this harness drives. The interface's
		// screens are still covered there by the golden frames and the model-level tests.
		t.Skip("no Unix pseudo-terminal on this system")
	}
	if err != nil {
		return nil, fmt.Errorf("start %s in a pty: %w", cmd.Path, err)
	}
	term := &Term{
		cmd:       cmd,
		ptmx:      ptmx,
		start:     time.Now(),
		emulator:  vt.NewEmulator(cols, rows),
		recording: Recording{Cols: cols, Rows: rows},
		changed:   make(chan struct{}, 1),
		done:      make(chan struct{}),
	}
	if !o.noSync {
		emulator := term.emulator
		emulator.RegisterCsiHandler(ansi.Command('?', '$', 'p'), func(params ansi.Params) bool {
			if n, _, ok := params.Param(0, 0); ok && n == 2026 {
				_, _ = io.WriteString(emulator.InputPipe(), ansi.ReportMode(ansi.ModeSynchronizedOutput, ansi.ModeReset))
				return true
			}
			return false
		})
	}
	go term.answer()
	go term.read()
	go func() {
		term.waitErr = cmd.Wait()
		close(term.done)
	}()
	if t != nil {
		t.Cleanup(func() { _ = term.Close() })
	}
	return term, nil
}

// read copies the program's output into the recording and the emulator.
func (t *Term) read() {
	buffer := make([]byte, 64*1024)
	for {
		n, err := t.ptmx.Read(buffer)
		if n > 0 {
			data := bytes.Clone(buffer[:n])
			t.mu.Lock()
			t.recording.Chunks = append(t.recording.Chunks, Chunk{At: time.Since(t.start), Data: data})
			_, _ = t.emulator.Write(data)
			t.mu.Unlock()
			select {
			case t.changed <- struct{}{}:
			default:
			}
		}
		if err != nil {
			return
		}
	}
}

// answer forwards the emulator's replies to terminal queries back to the program. The emulator
// writes them into a pipe synchronously while it parses output, so this loop must run for as long
// as output can arrive or the parser would block on its own reply.
func (t *Term) answer() {
	buffer := make([]byte, 4096)
	for {
		n, err := t.emulator.Read(buffer)
		if n > 0 {
			_, _ = t.ptmx.Write(buffer[:n])
		}
		if err != nil {
			return
		}
	}
}

// Write sends raw bytes to the program, as typed.
func (t *Term) Write(s string) error {
	_, err := io.WriteString(t.ptmx, s)
	return err
}

// Keys sends named keys (see KeySequence) or, for a name that is not a key, its text.
func (t *Term) Keys(names ...string) error {
	for _, name := range names {
		if err := t.Write(KeySequence(name)); err != nil {
			return err
		}
	}
	return nil
}

// Paste sends text as a bracketed paste, as a terminal does when a person pastes.
func (t *Term) Paste(text string) error {
	return t.Write("\x1b[200~" + text + "\x1b[201~")
}

// Resize changes the window size; the program receives SIGWINCH from the pty.
func (t *Term) Resize(cols, rows int) error {
	t.mu.Lock()
	t.emulator.Resize(cols, rows)
	t.recording.Resizes = append(t.recording.Resizes, Resize{At: time.Since(t.start), Cols: cols, Rows: rows})
	t.mu.Unlock()
	return pty.Setsize(t.ptmx, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
}

// Lines is the emulated screen as plain text, one string per row, right-trimmed. Every row is
// present, blank ones included, so a test can ask what is on the last row.
func (t *Term) Lines() []string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return screenLines(t.emulator)
}

func screenLines(e *vt.Emulator) []string {
	width, height := e.Width(), e.Height()
	lines := make([]string, height)
	var row strings.Builder
	for y := range height {
		row.Reset()
		for x := 0; x < width; x++ {
			cell := e.CellAt(x, y)
			if cell == nil || cell.Content == "" {
				if cell != nil && cell.Width == 0 {
					// The trailing half of a wide character: the character itself was written.
					continue
				}
				row.WriteByte(' ')
				continue
			}
			row.WriteString(cell.Content)
		}
		lines[y] = strings.TrimRight(row.String(), " ")
	}
	return lines
}

// History is the emulated terminal's scrollback followed by its main screen, as plain text: what a
// person could scroll back through after the program exits. It reads the main screen even while
// the alternate screen is shown.
func (t *Term) History() []string {
	t.mu.Lock()
	defer t.mu.Unlock()
	var lines []string
	if scrollback := t.emulator.Scrollback(); scrollback != nil {
		for _, line := range scrollback.Lines() {
			var row strings.Builder
			for _, cell := range line {
				if cell.Content == "" {
					if cell.Width == 0 {
						continue
					}
					row.WriteByte(' ')
					continue
				}
				row.WriteString(cell.Content)
			}
			lines = append(lines, strings.TrimRight(row.String(), " "))
		}
	}
	if !t.emulator.IsAltScreen() {
		lines = append(lines, screenLines(t.emulator)...)
	}
	return lines
}

// Screen is the emulated screen as plain text, rows joined by newlines, trailing blank rows kept.
func (t *Term) Screen() string { return strings.Join(t.Lines(), "\n") }

// AltScreen reports whether the program has switched to the alternate screen.
func (t *Term) AltScreen() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.emulator.IsAltScreen()
}

// WaitFor waits until the screen contains want, or fails after timeout with the screen in the
// error so a failing test says what it saw instead.
func (t *Term) WaitFor(want string, timeout time.Duration) error {
	return t.WaitUntil(func(screen string) bool { return strings.Contains(screen, want) }, timeout, "screen to contain "+fmt.Sprintf("%q", want))
}

// WaitUntil waits until ok holds for the screen.
func (t *Term) WaitUntil(ok func(screen string) bool, timeout time.Duration, what string) error {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	poll := time.NewTicker(20 * time.Millisecond)
	defer poll.Stop()
	for {
		if ok(t.Screen()) {
			return nil
		}
		select {
		case <-t.changed:
		case <-poll.C:
		case <-t.done:
			if ok(t.Screen()) {
				return nil
			}
			return fmt.Errorf("program exited while waiting for %s; screen:\n%s", what, t.Screen())
		case <-deadline.C:
			return fmt.Errorf("timed out waiting for %s; screen:\n%s", what, t.Screen())
		}
	}
}

// Wait waits for the program to exit, up to timeout.
func (t *Term) Wait(timeout time.Duration) error {
	select {
	case <-t.done:
		// Let the reader drain what the program wrote on its way out.
		time.Sleep(50 * time.Millisecond)
		return t.waitErr
	case <-time.After(timeout):
		return errors.New("program did not exit in time")
	}
}

// Recording is a copy of everything recorded so far.
func (t *Term) Recording() Recording {
	t.mu.Lock()
	defer t.mu.Unlock()
	copied := t.recording
	copied.Chunks = append([]Chunk(nil), t.recording.Chunks...)
	copied.Resizes = append([]Resize(nil), t.recording.Resizes...)
	return copied
}

// Close stops the program (if still running) and releases the pty.
func (t *Term) Close() error {
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		return nil
	}
	t.closed = true
	t.mu.Unlock()
	select {
	case <-t.done:
	default:
		if t.cmd.Process != nil {
			_ = t.cmd.Process.Kill()
		}
		select {
		case <-t.done:
		case <-time.After(2 * time.Second):
		}
	}
	// The emulator is deliberately left open: its reply loop reads the emulator's closed flag
	// without a lock, and closing it here would be a data race for no gain once the pty is gone.
	return t.ptmx.Close()
}
