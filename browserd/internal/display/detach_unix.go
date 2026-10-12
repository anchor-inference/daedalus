//go:build unix

package display

import (
	"os/exec"
	"syscall"
)

// detach puts the server in a session of its own, so a terminal's signal to the daemon's group does
// not end the display under browsers the daemon is still closing.
func detach(cmd *exec.Cmd) { cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true} }
