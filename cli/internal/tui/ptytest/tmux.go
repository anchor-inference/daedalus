package ptytest

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"
)

// Tmux is a headless tmux server on a private socket, so a test never touches a person's own
// sessions and two tests never touch each other's.
type Tmux struct {
	Socket string
	// Target is the pane every command addresses.
	Target string
}

// TmuxAvailable reports whether a tmux binary is on PATH.
func TmuxAvailable() bool {
	_, err := exec.LookPath("tmux")
	return err == nil
}

// NewTmux starts a detached session of the given size running a plain shell. The socket name
// carries the process id and the time, so concurrent runs cannot collide.
func NewTmux(cols, rows int) (*Tmux, error) {
	socket := fmt.Sprintf("ptytest-%d-%d", os.Getpid(), time.Now().UnixNano())
	session := "gate"
	tmux := &Tmux{Socket: socket, Target: session + ":0.0"}
	// A bare config: a person's ~/.tmux.conf could change the history limit, the status bar or
	// the terminal type, and any of those changes what is measured.
	cmd := exec.Command("tmux", "-L", socket, "-f", os.DevNull, "new-session", "-d", "-s", session,
		"-x", fmt.Sprint(cols), "-y", fmt.Sprint(rows), "env", "PS1=$ ", "sh")
	if out, err := cmd.CombinedOutput(); err != nil {
		return nil, fmt.Errorf("tmux new-session: %v: %s", err, out)
	}
	// The status bar would take a row from the program; the window size must stay as asked even
	// with no client attached.
	_ = tmux.Run("set-option", "-g", "status", "off")
	_ = tmux.Run("set-option", "-g", "history-limit", "10000")
	_ = tmux.Run("set-window-option", "-g", "window-size", "manual")
	_ = tmux.Run("resize-window", "-t", session, "-x", fmt.Sprint(cols), "-y", fmt.Sprint(rows))
	return tmux, nil
}

// Run runs one tmux command against this server.
func (t *Tmux) Run(args ...string) error {
	out, err := exec.Command("tmux", append([]string{"-L", t.Socket}, args...)...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("tmux %s: %v: %s", strings.Join(args, " "), err, out)
	}
	return nil
}

// Output runs one tmux command and returns what it printed.
func (t *Tmux) Output(args ...string) (string, error) {
	out, err := exec.Command("tmux", append([]string{"-L", t.Socket}, args...)...).Output()
	return string(out), err
}

// Type sends text to the pane literally, then Enter when enter is true.
func (t *Tmux) Type(text string, enter bool) error {
	if err := t.Run("send-keys", "-t", t.Target, "-l", text); err != nil {
		return err
	}
	if enter {
		return t.Run("send-keys", "-t", t.Target, "Enter")
	}
	return nil
}

// Capture is the visible pane as plain text.
func (t *Tmux) Capture() (string, error) { return t.Output("capture-pane", "-p", "-t", t.Target) }

// History is the pane's whole history plus the visible screen.
func (t *Tmux) History() (string, error) {
	return t.Output("capture-pane", "-p", "-S", "-", "-E", "-", "-t", t.Target)
}

// Resize changes the window size.
func (t *Tmux) Resize(cols, rows int) error {
	return t.Run("resize-window", "-t", strings.SplitN(t.Target, ":", 2)[0], "-x", fmt.Sprint(cols), "-y", fmt.Sprint(rows))
}

// Kill stops the server and everything in it.
func (t *Tmux) Kill() { _ = t.Run("kill-server") }
