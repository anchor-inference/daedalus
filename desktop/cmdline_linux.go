//go:build linux

package main

import (
	"os"
	"strconv"
	"strings"
)

// commandLineOf is a process's command line, read from /proc.
func commandLineOf(pid int) (string, bool) {
	body, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/cmdline")
	if err != nil {
		return "", false
	}
	return strings.ReplaceAll(string(body), "\x00", " "), true
}

// processStartMarker is when the process started, in clock ticks since boot (field 22 of
// /proc/<pid>/stat). With the pid it names one process for good: a pid reused later has another.
func processStartMarker(pid int) (string, bool) {
	body, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return "", false
	}
	// The command name in parentheses may hold spaces; the fields after the last ")" do not.
	text := string(body)
	end := strings.LastIndexByte(text, ')')
	if end < 0 {
		return "", false
	}
	fields := strings.Fields(text[end+1:])
	// fields[0] is field 3 (state); starttime is field 22.
	if len(fields) < 20 {
		return "", false
	}
	return fields[19], true
}
