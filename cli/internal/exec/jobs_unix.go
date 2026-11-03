//go:build unix

package exec

import (
	"errors"
	"syscall"
	"time"
)

// processAlive says whether a process with this id exists. A process of another user counts: it is
// there, only not ours to signal.
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	err := syscall.Kill(pid, 0)
	return err == nil || errors.Is(err, syscall.EPERM)
}

// killGroup ends the process group a job leads, for a job another client started: the hangup, then
// the kill after the grace exec.run gives a program that ran out of time.
func killGroup(pid int) error {
	if pid <= 0 {
		return errors.New("no process")
	}
	if err := syscall.Kill(-pid, syscall.SIGHUP); err != nil && !errors.Is(err, syscall.ESRCH) {
		return err
	}
	deadline := time.Now().Add(2 * time.Second)
	for processAlive(pid) && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
		return err
	}
	return nil
}
