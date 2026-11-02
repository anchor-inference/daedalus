//go:build windows

package sidechan

import (
	"os"
	"os/exec"
	"sync"

	"github.com/ascorblack/daedalus/ptyd/internal/ptyproc"
)

// procGroup is a program run by exec.run and everything it starts: on Windows a job object, the one
// thing that holds a whole tree there. The program joins it the moment it has started; a child it
// starts in that instant can escape, which a process group on Unix does not allow, and is the one
// gap left.
type procGroup struct {
	cmd *exec.Cmd
	mu  sync.Mutex
	job *ptyproc.Job
}

func newGroup(cmd *exec.Cmd) *procGroup { return &procGroup{cmd: cmd} }

func (g *procGroup) started() {
	job, err := ptyproc.NewJob()
	if err != nil {
		return
	}
	if err := job.AssignPid(g.cmd.Process.Pid); err != nil {
		job.Close()
		return
	}
	g.mu.Lock()
	g.job = job
	g.mu.Unlock()
}

// release closes the job, which ends what the program left running. On Unix such a process
// survives; here it would otherwise be a process nothing tracks, started by a call that returned.
func (g *procGroup) release() {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.job != nil {
		g.job.Close()
		g.job = nil
	}
}

// signal ends the tree. Windows has no hangup for a program without a console, so the polite first
// step of the Unix version has nothing to send, and both steps terminate.
func (g *procGroup) signal(kill bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.job != nil {
		_ = g.job.Terminate(1)
		return
	}
	if g.cmd.Process != nil {
		_ = g.cmd.Process.Kill()
	}
}

// signalOf is empty: Windows programs end with an exit code, never a signal.
func signalOf(ps *os.ProcessState) string { return "" }
