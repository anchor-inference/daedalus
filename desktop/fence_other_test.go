//go:build !linux

package main

import (
	"path/filepath"
	"testing"
)

// Without a kernel writer fence the switch refuses and changes nothing.
func TestFenceSwitchFailsClosedWithoutAKernelFence(t *testing.T) {
	data := filepath.Join(t.TempDir(), "data")
	rep := fencedSwitch(fenceOptions{Data: data})
	if rep.Outcome != fenceFailClosed || rep.ExitCode != 5 {
		t.Fatalf("%+v", rep)
	}
}
