//go:build windows

package main

import (
	"os/exec"
	"strconv"
	"strings"
)

// commandLineOf is a process's command line, from CIM. Not yet run on a real Windows machine.
func commandLineOf(pid int) (string, bool) {
	out, err := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command",
		"(Get-CimInstance Win32_Process -Filter \"ProcessId="+strconv.Itoa(pid)+"\").CommandLine").Output()
	if err != nil {
		return "", false
	}
	line := strings.TrimSpace(string(out))
	return line, line != ""
}

// processStartMarker is the process's creation time, from CIM. Not yet run on a real Windows machine.
func processStartMarker(pid int) (string, bool) {
	out, err := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command",
		"(Get-CimInstance Win32_Process -Filter \"ProcessId="+strconv.Itoa(pid)+"\").CreationDate.ToUniversalTime().Ticks").Output()
	if err != nil {
		return "", false
	}
	line := strings.TrimSpace(string(out))
	return line, line != ""
}
