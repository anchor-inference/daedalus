//go:build linux

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// A non-dumpable peer hides cwd, exe and fd even from the same UID. The launcher cannot
// establish that it is outside data, so it must refuse with a useful recovery instruction.
func TestNonDumpablePeerFailsClosedWithoutMissingAWriter(t *testing.T) {
	data := filepath.Join(t.TempDir(), "data")
	if err := os.Mkdir(data, 0o700); err != nil {
		t.Fatal(err)
	}
	for _, inside := range []bool{false, true} {
		cwd := t.TempDir()
		if inside {
			cwd = data
		}
		cmd := exec.Command("python3", "-c", "import ctypes,time; ctypes.CDLL(None).prctl(4,0,0,0,0); time.sleep(30)")
		cmd.Dir = cwd
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		pid := cmd.Process.Pid
		time.Sleep(200 * time.Millisecond)
		err := checkDataWriters(data)
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		if err == nil || !strings.Contains(err.Error(), fmt.Sprint(pid)) || !strings.Contains(err.Error(), "isolated cgroup") {
			t.Fatalf("non-dumpable process inside=%t: %v", inside, err)
		}
	}
}

func TestDataWriterGateFindsAnUnrecordedWorkingDirectoryAndOpenFile(t *testing.T) {
	data := filepath.Join(t.TempDir(), "data")
	if err := os.Mkdir(data, 0o700); err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(data, "writer.log")
	if err := os.WriteFile(file, []byte("before"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name string
		cmd  *exec.Cmd
		want string
	}{
		{"cwd", &exec.Cmd{Path: "/bin/sleep", Args: []string{"sleep", "30"}, Dir: data}, "working directory"},
		{"fd", exec.Command("sh", "-c", `exec 3< "$1"; exec sleep 30`, "sh", file), "open under the data folder"},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := test.cmd.Start(); err != nil {
				t.Fatal(err)
			}
			defer func() { _ = test.cmd.Process.Kill(); _ = test.cmd.Wait() }()
			deadline := time.Now().Add(2 * time.Second)
			for time.Now().Before(deadline) {
				if err := checkDataWriters(data); err != nil && strings.Contains(err.Error(), test.want) {
					return
				}
				time.Sleep(20 * time.Millisecond)
			}
			t.Fatalf("unrecorded %s writer was not refused", test.name)
		})
	}
}
