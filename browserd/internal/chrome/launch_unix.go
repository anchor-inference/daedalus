//go:build unix

package chrome

import (
	"io"
	"os"
	"os/exec"
	"syscall"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
)

// startPiped starts the browser with its debugging pipe on file descriptors 3 (the browser reads)
// and 4 (the browser writes), in a session of its own so that the whole tree can be ended at once.
func startPiped(opts Options, stderr io.Writer, handler func(cdp.Event)) (*exec.Cmd, *cdp.Conn, error) {
	toR, toW, err := os.Pipe()
	if err != nil {
		return nil, nil, err
	}
	fromR, fromW, err := os.Pipe()
	if err != nil {
		toR.Close()
		toW.Close()
		return nil, nil, err
	}
	cmd := exec.Command(opts.Path, Args(opts)...)
	cmd.Env = opts.Env
	cmd.Stderr = stderr
	cmd.ExtraFiles = []*os.File{toR, fromW}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		toR.Close()
		toW.Close()
		fromR.Close()
		fromW.Close()
		return nil, nil, err
	}
	// The browser holds its ends now; the daemon keeps only its own, so the pipe ends when the
	// browser does.
	toR.Close()
	fromW.Close()
	conn := cdp.New(fromR, toW, handler)
	go func() {
		<-conn.Done()
		fromR.Close()
		toW.Close()
	}()
	return cmd, conn, nil
}

// killTree ends the browser's session: the browser and every helper it started, and returns once
// they have died (see waitGroupGone), or after groupExitWait if one has not.
func killTree(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	_ = cmd.Process.Kill()
	waitGroupGone(cmd.Process.Pid, groupExitWait)
}

// groupExitWait bounds the wait for a killed browser's processes to die: they die in milliseconds,
// and a stop must never hang on one the kernel is slow to take down.
const groupExitWait = 5 * time.Second
