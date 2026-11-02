//go:build unix

package sidechan

import (
	"os"
	"os/exec"
	"syscall"

	"github.com/ascorblack/daedalus/ptyd/internal/ptyproc"
)

// procGroup is a program run by exec.run and everything it starts: on Unix its process group.
type procGroup struct{ cmd *exec.Cmd }

// newGroup starts the program as the leader of a new process group, so the whole group can be
// ended together, and never with a controlling terminal of the daemon's.
func newGroup(cmd *exec.Cmd) *procGroup {
	// The hangup that ends the group on a timeout must not be ignored by the program.
	ptyproc.DefaultSignalsForChildren()
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	return &procGroup{cmd: cmd}
}

func (g *procGroup) started() {}

func (g *procGroup) release() {}

// signal sends the hangup (or, with kill, SIGKILL) to the program's process group.
func (g *procGroup) signal(kill bool) {
	if g.cmd.Process == nil {
		return
	}
	sig := syscall.SIGHUP
	if kill {
		sig = syscall.SIGKILL
	}
	// The group id is the leader's pid. A leader that already exited leaves the group behind it,
	// which is exactly what still has to be ended.
	_ = syscall.Kill(-g.cmd.Process.Pid, sig)
}

// signalOf names the signal that ended the program, as terminals' exits name theirs.
func signalOf(ps *os.ProcessState) string {
	if ws, ok := ps.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
		return ptyproc.SignalName(ws.Signal())
	}
	return ""
}
