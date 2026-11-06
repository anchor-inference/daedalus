//go:build !windows

package main

import (
	"os/exec"
	"syscall"
)

// clientAttributes leaves a client in this process's group, so the launcher's signal to the group, and
// the record of it after a crash, reach it too. No Pdeathsig: it follows the thread that forked, and
// the Go runtime retires threads.
func clientAttributes(*exec.Cmd) {}

func afterStart(*exec.Cmd) {}

func endClient(cmd *exec.Cmd) { _ = cmd.Process.Signal(syscall.SIGTERM) }
