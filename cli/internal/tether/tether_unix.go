//go:build unix

package tether

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"strconv"
	"sync"
	"syscall"
	"time"
)

// grace is the time between the hangup and the kill, as exec.run gives a program that ran out of
// time: long enough for an installer to remove its lock file, short enough not to matter.
const grace = 2 * time.Second

// Tether is the client's end of the watchdog: the pipe it writes each command's group to.
type Tether struct {
	mu   sync.Mutex
	w    *os.File
	done chan struct{}
}

// Start starts the watchdog. An error leaves the client without one; on Linux the leaders of its
// commands still die with it.
func Start() (*Tether, error) {
	self := "/proc/self/exe"
	if runtime.GOOS != "linux" {
		// Linux's link names the running program even when an update has replaced or removed the
		// file it was started from; elsewhere the path is the best there is.
		var err error
		if self, err = os.Executable(); err != nil {
			return nil, fmt.Errorf("find this program to start its watchdog: %w", err)
		}
	}
	// The pipe is opened close-on-exec, as Go opens every descriptor, and that is load-bearing: a
	// command that inherited the write end would keep the pipe open after the client died, and the
	// watchdog would never see it go.
	r, w, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	cmd := &exec.Cmd{Path: self, Args: []string{"daedalus-cli-tether"}, Dir: "/", Stdin: r,
		Env: append(os.Environ(), envChild+"=1"),
		// A group of its own: Ctrl+C and the terminal's hangup reach the terminal's foreground
		// group, the client's, and must not take the watchdog down before it has done its work.
		// No parent-death signal for it, for the same reason.
		SysProcAttr: &syscall.SysProcAttr{Setpgid: true}}
	if err := cmd.Start(); err != nil {
		r.Close()
		w.Close()
		return nil, fmt.Errorf("start the watchdog: %w", err)
	}
	r.Close()
	t := &Tether{w: w, done: make(chan struct{})}
	go func() {
		_ = cmd.Wait()
		close(t.done)
	}()
	return t, nil
}

// Hold registers a command's process group with the watchdog and returns what unregisters it. A
// watchdog that has gone makes both a no-op: the command then lives as it would have untied.
func (t *Tether) Hold(pid int) func() {
	t.send('+', pid)
	return func() { t.send('-', pid) }
}

func (t *Tether) send(op byte, pid int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.w == nil {
		return
	}
	// One short line is one write, which a pipe delivers whole.
	_, _ = t.w.Write([]byte(string(op) + strconv.Itoa(pid) + "\n"))
}

// Close closes the client's end, which is what dying does: the watchdog ends whatever is still
// registered and exits. For the tests, and for a client that wants to end its commands on its way out.
func (t *Tether) Close() {
	t.mu.Lock()
	w := t.w
	t.w = nil
	t.mu.Unlock()
	if w != nil {
		w.Close()
	}
}

// Done is closed when the watchdog has exited.
func (t *Tether) Done() <-chan struct{} { return t.done }

// Watch is the watchdog: it keeps the groups the client registers on in, and when in ends — the
// client is gone, however it went — or a SIGTERM asks it to stop, it hangs up every group still
// registered and kills them after the grace. It returns the exit status.
func Watch(in io.Reader) int {
	// The terminal's signals are the client's business; a hangup here would leave the commands
	// with nobody to end them.
	signal.Ignore(syscall.SIGINT, syscall.SIGHUP, syscall.SIGQUIT, syscall.SIGTSTP, syscall.SIGTTIN, syscall.SIGTTOU)
	term := make(chan os.Signal, 1)
	signal.Notify(term, syscall.SIGTERM)

	lines := make(chan string)
	go func() {
		scan := bufio.NewScanner(in)
		for scan.Scan() {
			lines <- scan.Text()
		}
		close(lines)
	}()
	groups := map[int]bool{}
	for {
		select {
		case line, ok := <-lines:
			if !ok {
				end(groups)
				return 0
			}
			if len(line) < 2 {
				continue
			}
			pid, err := strconv.Atoi(line[1:])
			// A pid of 1 or less names no group of the client's, and kill(-1) would be everything.
			if err != nil || pid <= 1 {
				continue
			}
			switch line[0] {
			case '+':
				groups[pid] = true
			case '-':
				delete(groups, pid)
			}
		case <-term:
			end(groups)
			return 0
		}
	}
}

// end hangs up each group and kills what is left after the grace. Unregistering happens right after
// a command is waited for, so a group here is one whose leader the client never saw end; its number
// cannot name another group within the grace unless the system went through every pid in between.
func end(groups map[int]bool) {
	if len(groups) == 0 {
		return
	}
	for pid := range groups {
		_ = syscall.Kill(-pid, syscall.SIGHUP)
	}
	time.Sleep(grace)
	for pid := range groups {
		_ = syscall.Kill(-pid, syscall.SIGKILL)
	}
}
