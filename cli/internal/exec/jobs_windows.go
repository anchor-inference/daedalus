//go:build windows

package exec

import (
	"errors"
	osexec "os/exec"
	"strconv"
	"syscall"
)

// stillActive is what GetExitCodeProcess answers for a process that has not ended.
const stillActive = 259

// processAlive says whether a process with this id is still running.
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	h, err := syscall.OpenProcess(syscall.PROCESS_QUERY_INFORMATION, false, uint32(pid))
	if err != nil {
		// Access denied means it is there; any other answer means it is not.
		return errors.Is(err, syscall.ERROR_ACCESS_DENIED)
	}
	defer syscall.CloseHandle(h)
	var code uint32
	if err := syscall.GetExitCodeProcess(h, &code); err != nil {
		return false
	}
	return code == stillActive
}

// killGroup ends the tree of a job another client started. Its job object belongs to that client, so
// taskkill walks the tree instead.
func killGroup(pid int) error {
	if pid <= 0 {
		return errors.New("no process")
	}
	return osexec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(pid)).Run()
}
