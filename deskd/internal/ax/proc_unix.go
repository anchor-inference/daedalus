//go:build !windows

package ax

import (
	"os/exec"
	"syscall"
)

// setGroup puts the child in a process group of its own, so a kill reaches whatever it started
// (the tool's helper processes) and not only the tool.
func setGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func killGroup(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); err != nil {
		return cmd.Process.Kill()
	}
	return nil
}
