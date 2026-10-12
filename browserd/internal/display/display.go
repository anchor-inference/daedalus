// Package display runs the virtual X display that headed browsers draw on when the daemon has no
// display of its own: in the container, where nobody looks at the windows and the screencast is
// what the operator sees.
package display

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// ReadyTimeout is how long Xvfb has to say which display it took. It starts in a tenth of a second;
// the rest is a cold disk.
const ReadyTimeout = 15 * time.Second

// Display is one running Xvfb.
type Display struct {
	// Name is what DISPLAY is set to for a browser on it, ":<n>".
	Name string

	cmd    *exec.Cmd
	done   chan struct{}
	once   sync.Once
	stderr *tail
}

// ErrNoXvfb is a headed mode asked for where there is no display and no Xvfb to make one.
var ErrNoXvfb = errors.New("headed mode needs a display: DISPLAY is not set and Xvfb is not installed")

// Start runs Xvfb with one screen of w×h at 24 bits and waits until it accepts clients. The display
// number is Xvfb's own choice (-displayfd): a fixed one would collide with a second daemon, a
// test, or a display a person left behind in /tmp/.X11-unix.
func Start(w, h int) (*Display, error) {
	path, err := exec.LookPath("Xvfb")
	if err != nil {
		return nil, ErrNoXvfb
	}
	r, wr, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	defer r.Close()
	d := &Display{done: make(chan struct{}), stderr: &tail{n: 4 << 10}}
	// No TCP listener: the browsers are its only clients and they are local. No reset when the last
	// client leaves, which would otherwise rebuild the server each time the last browser closes.
	d.cmd = exec.Command(path, "-displayfd", "3", "-screen", "0", fmt.Sprintf("%dx%dx24", w, h),
		"-nolisten", "tcp", "-noreset", "-dpi", "96")
	d.cmd.Stderr = d.stderr
	d.cmd.ExtraFiles = []*os.File{wr}
	detach(d.cmd)
	if err := d.cmd.Start(); err != nil {
		wr.Close()
		return nil, fmt.Errorf("Xvfb did not start: %w", err)
	}
	wr.Close()
	go func() {
		_ = d.cmd.Wait()
		close(d.done)
	}()
	number := make(chan string, 1)
	go func() {
		line, _ := bufio.NewReader(r).ReadString('\n')
		number <- strings.TrimSpace(line)
	}()
	select {
	case n := <-number:
		if n == "" {
			d.Close()
			return nil, fmt.Errorf("Xvfb exited before it took a display: %s", d.lastWords())
		}
		d.Name = ":" + n
		return d, nil
	case <-time.After(ReadyTimeout):
		d.Close()
		return nil, fmt.Errorf("Xvfb did not take a display within %s: %s", ReadyTimeout, d.lastWords())
	}
}

// Alive says whether the server still runs.
func (d *Display) Alive() bool {
	select {
	case <-d.done:
		return false
	default:
		return true
	}
}

// Done is closed when the server has exited.
func (d *Display) Done() <-chan struct{} { return d.done }

// Close ends the server. Browsers still drawing on it lose their windows and exit.
func (d *Display) Close() {
	d.once.Do(func() {
		if d.cmd.Process != nil {
			_ = d.cmd.Process.Kill()
		}
		<-d.done
	})
}

func (d *Display) lastWords() string {
	s := strings.TrimSpace(d.stderr.String())
	if s == "" {
		return "it wrote nothing to stderr"
	}
	if i := strings.LastIndexByte(s, '\n'); i >= 0 {
		s = s[i+1:]
	}
	return s
}

type tail struct {
	mu  sync.Mutex
	n   int
	buf []byte
}

var _ io.Writer = (*tail)(nil)

func (t *tail) Write(b []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, b...)
	if len(t.buf) > t.n {
		t.buf = append([]byte(nil), t.buf[len(t.buf)-t.n:]...)
	}
	return len(b), nil
}

func (t *tail) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}
