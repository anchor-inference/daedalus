//go:build !linux && !windows

package main

import (
	"os/exec"
	"strconv"
	"strings"
)

// commandLineOf is a process's command line, from ps. Not yet run on a real Mac.
func commandLineOf(pid int) (string, bool) {
	out, err := exec.Command("ps", "-o", "command=", "-p", strconv.Itoa(pid)).Output()
	if err != nil {
		return "", false
	}
	line := strings.TrimSpace(string(out))
	return line, line != ""
}

// processStartMarker is when the process started, as ps reports it. Not yet run on a real Mac.
func processStartMarker(pid int) (string, bool) {
	out, err := exec.Command("ps", "-o", "lstart=", "-p", strconv.Itoa(pid)).Output()
	if err != nil {
		return "", false
	}
	line := strings.TrimSpace(string(out))
	return line, line != ""
}
