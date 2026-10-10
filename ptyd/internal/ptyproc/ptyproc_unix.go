//go:build unix

package ptyproc

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"github.com/creack/pty"
)

// Proc is a running program and the master side of its PTY.
type Proc struct {
	Master *os.File
	Pid    int
	tag    string

	wrapped bool
	program atomic.Int64 // the wrapped program's pid, once found; 0 before

	cmd  *exec.Cmd
	done chan struct{}
	exit Exit

	killOnce sync.Mutex
}

// Start runs spec on a new PTY in a new session, with the PTY as its controlling terminal. The
// program is its own process group leader, so the group and the session are both numbered by its
// pid.
func Start(spec Spec) (*Proc, error) {
	if len(spec.Argv) == 0 {
		return nil, errors.New("empty argv")
	}
	DefaultSignalsForChildren()
	cmd := &exec.Cmd{Path: spec.Path, Args: spec.Argv, Dir: spec.Dir, Env: spec.Env}
	size := &pty.Winsize{Cols: uint16(spec.Cols), Rows: uint16(spec.Rows), X: uint16(spec.PxW), Y: uint16(spec.PxH)}
	attrs := &syscall.SysProcAttr{Setsid: true, Setctty: true}
	if err := containmentAttrs(attrs, spec); err != nil {
		return nil, err
	}
	master, err := pty.StartWithAttrs(cmd, size, attrs)
	if err != nil {
		return nil, err
	}
	p := &Proc{Pid: cmd.Process.Pid, tag: spec.Tag, wrapped: spec.Wrapped, cmd: cmd, done: make(chan struct{})}
	go p.wait()
	if p.Master, err = pollable(master); err != nil {
		_ = syscall.Kill(-p.Pid, syscall.SIGKILL)
		<-p.done
		return nil, err
	}
	return p, nil
}

// DefaultSignalsForChildren makes the programs the daemon starts from now on begin with the default
// action for the hangup, the interrupt and the broken pipe, whatever the daemon itself inherited, and
// keeps a broken pipe from ever ending the daemon. The daemon calls it as it starts; Start and
// exec.run call it again, which costs nothing.
//
// A signal ignored when a process starts stays ignored across exec, and a shell does not undo that
// for its commands: bash leaves a signal that was ignored when it started ignored in everything it
// runs. A daemon started in the background of a script (which ignores SIGINT in what it runs), under
// nohup, or by a service manager that ignores SIGHUP would hand that on to every terminal: Ctrl+C
// would interrupt nothing, and the hangup that ends a terminal would end nothing either. The Go
// runtime keeps an inherited ignoring of exactly these two signals, and resets a signal it catches
// to the default in a child; so catching them, and dropping what arrives, leaves the daemon as it
// was and gives every program the defaults back.
//
// The broken pipe is caught always. The daemon once ignored it with signal.Ignore, and every program
// it started inherited that: `tail file | head -c N` printed "tail: error writing 'standard output':
// Broken pipe" into the output the agent read, which corrupted a file read through base64, and every
// writer cut short by a reader that had enough (`yes | head`) complained the same way. The daemon
// still must not die of it: a socket write already returns EPIPE, but a write to a standard output or
// error whose reader went away makes the runtime raise SIGPIPE and end the process unless something
// has asked for the signal. Catching it and dropping it gives the daemon the error and the children
// the default.
func DefaultSignalsForChildren() {
	defaultSignals.Do(func() {
		for _, sig := range []os.Signal{syscall.SIGHUP, syscall.SIGINT, syscall.SIGPIPE} {
			if sig == syscall.SIGPIPE || signal.Ignored(sig) {
				c := make(chan os.Signal, 1)
				signal.Notify(c, sig)
				go func() {
					for range c {
					}
				}()
			}
		}
	})
}

var defaultSignals sync.Once

// pollable turns the master into a file the runtime's poller manages. The library's ioctls go
// through Fd(), which switches the file to blocking mode for good; a blocking read cannot be
// interrupted by Close, so a terminal whose program exited while a background job still held the
// PTY would keep its reader, and its "running" state, forever. A duplicate descriptor set
// non-blocking and wrapped anew is registered with the poller, and every ioctl from here on goes
// through SyscallConn, which never touches the mode.
func pollable(master *os.File) (*os.File, error) {
	fd, err := syscall.Dup(int(master.Fd()))
	master.Close()
	if err != nil {
		return nil, err
	}
	syscall.CloseOnExec(fd)
	if err := syscall.SetNonblock(fd, true); err != nil {
		syscall.Close(fd)
		return nil, err
	}
	return os.NewFile(uintptr(fd), "/dev/ptmx"), nil
}

// ioctl runs one ioctl on the master without taking it out of the poller.
func (p *Proc) ioctl(req uintptr, arg unsafe.Pointer) error {
	sc, err := p.Master.SyscallConn()
	if err != nil {
		return err
	}
	var errno syscall.Errno
	if err := sc.Control(func(fd uintptr) {
		_, _, errno = syscall.Syscall(syscall.SYS_IOCTL, fd, req, uintptr(arg))
	}); err != nil {
		return err
	}
	if errno != 0 {
		return errno
	}
	return nil
}

func (p *Proc) wait() {
	_ = p.cmd.Wait()
	ps := p.cmd.ProcessState
	if ps == nil {
		// Wait failed without reaping anything, which exec only does for a process it never started.
		p.exit = Exit{Code: -1}
		close(p.done)
		return
	}
	if st, ok := ps.Sys().(syscall.WaitStatus); ok && st.Signaled() {
		p.exit = Exit{Code: -1, Signal: SignalName(st.Signal())}
	} else {
		p.exit = Exit{Code: ps.ExitCode()}
	}
	close(p.done)
}

// Done is closed when the program has exited and been reaped.
func (p *Proc) Done() <-chan struct{} { return p.done }

// Wait blocks until the program exits and returns how it ended. It may be called any number of
// times.
func (p *Proc) Wait() Exit {
	<-p.done
	return p.exit
}

// Resize sets the PTY's size; the kernel sends SIGWINCH to the foreground group.
func (p *Proc) Resize(cols, rows, pxW, pxH int) error {
	ws := pty.Winsize{Cols: uint16(cols), Rows: uint16(rows), X: uint16(pxW), Y: uint16(pxH)}
	return p.ioctl(uintptr(syscall.TIOCSWINSZ), unsafe.Pointer(&ws))
}

// Foreground returns the PTY's foreground process group: the job a Ctrl+C would reach.
func (p *Proc) Foreground() (int, error) {
	var pgrp int32
	if err := p.ioctl(uintptr(syscall.TIOCGPGRP), unsafe.Pointer(&pgrp)); err != nil {
		return 0, err
	}
	return int(pgrp), nil
}

// Signal sends sig to the program alone.
func (p *Proc) Signal(sig syscall.Signal) error {
	return ignoreGone(syscall.Kill(p.Pid, sig))
}

// SignalForeground sends sig to the foreground process group, falling back to the program's own
// group when the PTY cannot say (it is closing, or the program exited). This is what the kernel
// does for a typed Ctrl+C: interrupting `sleep` under an interactive shell reaches `sleep`, which
// the shell's own group would not.
func (p *Proc) SignalForeground(sig syscall.Signal) error {
	if pg, err := p.Foreground(); err == nil && pg > 0 {
		if err := syscall.Kill(-pg, sig); err == nil {
			return nil
		}
	}
	return ignoreGone(syscall.Kill(-p.Pid, sig))
}

// KillTree ends the program and everything it started: SIGHUP to its group and the foreground
// group, up to grace for the program to exit, then SIGKILL to the group, the session and (where the
// process table is read: Linux and macOS) every process that descends from it or carries its tag.
// It returns once the program has been reaped.
func (p *Proc) KillTree(grace time.Duration) Exit {
	p.killOnce.Lock()
	defer p.killOnce.Unlock()
	// Collect the stragglers before the first signal: once a parent dies its children are reparented
	// and the tree no longer leads to them.
	before := p.members(!p.exited())
	if pg, err := p.Foreground(); err == nil && pg > 0 && pg != p.Pid {
		_ = syscall.Kill(-pg, syscall.SIGHUP)
	}
	_ = syscall.Kill(-p.Pid, syscall.SIGHUP)
	// A shell ignores HUP only when told to; programs that catch it get the grace to clean up.
	select {
	case <-p.done:
	case <-time.After(grace):
	}
	_ = syscall.Kill(-p.Pid, syscall.SIGKILL)
	// The program has been reaped by now, or is about to be, so its pid may already belong to someone
	// else: the second look goes by session and tag only, never by a tree rooted at that pid.
	for _, m := range append(before, p.members(false)...) {
		m.kill()
	}
	return p.Wait()
}

func (p *Proc) exited() bool {
	select {
	case <-p.done:
		return true
	default:
		return false
	}
}

// Close releases the master side. Anything still holding the PTY open sees a hangup.
func (p *Proc) Close() error { return p.Master.Close() }

func ignoreGone(err error) error {
	if errors.Is(err, syscall.ESRCH) {
		return nil
	}
	return err
}

var signalNames = map[syscall.Signal]string{
	syscall.SIGHUP: "HUP", syscall.SIGINT: "INT", syscall.SIGQUIT: "QUIT", syscall.SIGILL: "ILL",
	syscall.SIGTRAP: "TRAP", syscall.SIGABRT: "ABRT", syscall.SIGBUS: "BUS", syscall.SIGFPE: "FPE",
	syscall.SIGKILL: "KILL", syscall.SIGUSR1: "USR1", syscall.SIGSEGV: "SEGV", syscall.SIGUSR2: "USR2",
	syscall.SIGPIPE: "PIPE", syscall.SIGALRM: "ALRM", syscall.SIGTERM: "TERM", syscall.SIGCONT: "CONT",
	syscall.SIGSTOP: "STOP", syscall.SIGTSTP: "TSTP", syscall.SIGTTIN: "TTIN", syscall.SIGTTOU: "TTOU",
	syscall.SIGWINCH: "WINCH", syscall.SIGXCPU: "XCPU", syscall.SIGXFSZ: "XFSZ",
}

// SignalName is the short name of a signal ("KILL"), or its number when it has none here.
func SignalName(s syscall.Signal) string {
	if n, ok := signalNames[s]; ok {
		return n
	}
	return fmt.Sprint(int(s))
}

// ParseSignal maps the names the protocol accepts to signals.
func ParseSignal(name string) (syscall.Signal, bool) {
	switch name {
	case "INT":
		return syscall.SIGINT, true
	case "TERM":
		return syscall.SIGTERM, true
	case "HUP":
		return syscall.SIGHUP, true
	case "KILL":
		return syscall.SIGKILL, true
	case "QUIT":
		return syscall.SIGQUIT, true
	case "TSTP":
		return syscall.SIGTSTP, true
	case "CONT":
		return syscall.SIGCONT, true
	case "WINCH":
		return syscall.SIGWINCH, true
	case "USR1":
		return syscall.SIGUSR1, true
	case "USR2":
		return syscall.SIGUSR2, true
	}
	return 0, false
}
