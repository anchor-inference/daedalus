//go:build windows

package main

import (
	"os/exec"
	"strings"
	"testing"
)

// The record of a child is only as good as these two: read from the process directly, a long
// command line whole, and the same marker every time it is asked of the same process.
func TestAProcessIsKnownByItsCommandLineAndStartTime(t *testing.T) {
	// Longer than the first buffer commandLineOf offers, so its retry is what reads it.
	tail := strings.Repeat("x", 3000)
	cmd := exec.Command("cmd", "/c", "ping -n 30 127.0.0.1 >nul & rem "+tail)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { killPID(cmd.Process.Pid); _ = cmd.Wait() })
	line, ok := commandLineOf(cmd.Process.Pid)
	if !ok || !strings.Contains(line, "cmd") || !strings.Contains(line, "rem "+tail) {
		t.Fatalf("the command line is not the child's: ok=%v, %d characters, %.80q", ok, len(line), line)
	}
	first, ok := processStartMarker(cmd.Process.Pid)
	if !ok || first == "" {
		t.Fatal("no start marker for a running child")
	}
	if again, _ := processStartMarker(cmd.Process.Pid); again != first {
		t.Fatalf("the marker moved: %s, then %s", first, again)
	}
	if _, ok := processStartMarker(0x7ffffffc); ok {
		t.Fatal("a pid nobody holds has a start marker")
	}
}
