//go:build !windows

package main

// A child of the launcher is put in a process group of its own, so a signal reaches it and anything
// still in that group. What it does not reach is the bot: the supervisor starts it in a session of
// its own, so a measured stop goes launcher -> supervisor group -> the supervisor's own handler ->
// the bot, drained. The group is the way in, not the way all the way down.

import (
	"os/exec"
	"syscall"
)

func setProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

// terminateGroup asks the whole group to end. SIGTERM is what the supervisor handles: it drains the
// bot, which is the difference between stopping an installation and interrupting it.
func terminateGroup(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM); err != nil {
		_ = cmd.Process.Signal(syscall.SIGTERM)
	}
}

func killGroup(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); err != nil {
		_ = cmd.Process.Kill()
	}
}

// terminatePID and killPID end a recorded child's whole group; the group id is its pid, because it
// was started with Setpgid.
func terminatePID(pid int, _ bool) {
	if err := syscall.Kill(-pid, syscall.SIGTERM); err != nil {
		_ = syscall.Kill(pid, syscall.SIGTERM)
	}
}

func killPID(pid int) {
	// kill(0) and kill(-0) mean this process's own group, and -1 means everything this user may
	// signal: a record that says 0 or 1 — missing, damaged, or not ours — must never reach them.
	if pid <= 1 {
		return
	}
	if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil {
		_ = syscall.Kill(pid, syscall.SIGKILL)
	}
}
