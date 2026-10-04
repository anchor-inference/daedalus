//go:build linux

package containment

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestScopeAndObservations(t *testing.T) {
	good := Scope{AttemptID: "attempt_123", HostGeneration: "123", LaunchID: "launch_123"}
	if !valid(good) || valid(Scope{AttemptID: "../escape", HostGeneration: good.HostGeneration, LaunchID: good.LaunchID}) {
		t.Fatal("attempt identities must not become paths")
	}
	if key(good) == key(Scope{AttemptID: good.AttemptID, HostGeneration: "124", LaunchID: good.LaunchID}) {
		t.Fatal("an older host generation reused the same ownership key")
	}
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "cgroup.events"), []byte("populated 1\nfrozen 0\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "cpu.stat"), []byte("usage_usec 42\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	group := &group{path: root}
	view, err := group.Observe()
	if err != nil || !view.Populated || view.CPUUsageUsec != 42 || view.MemoryPeakBytes != -1 {
		t.Fatalf("uncertain metrics may not become zero: %+v, %v", view, err)
	}
}

func TestWriterContainmentWithoutResourceCeilings(t *testing.T) {
	root := t.TempDir()
	if err := install(root, Limits{}); err != nil {
		t.Fatal(err)
	}
	for file, expected := range map[string]string{
		"memory.max": "max", "cpu.max": "max 100000", "pids.max": "max",
	} {
		value, err := os.ReadFile(filepath.Join(root, file))
		if err != nil || string(value) != expected {
			t.Fatalf("%s = %q, %v; want %q", file, value, err, expected)
		}
	}
}

func TestKernelContainmentOwnsSetsidDescendantWithoutKillingSibling(t *testing.T) {
	controller, err := NewController("testinstance")
	if err != nil {
		t.Skipf("no own delegated cgroup: %v", err)
	}
	if status := controller.Probe(); !status.Available {
		t.Skipf("no verified kernel containment: %s", status.Reason)
	}
	if _, err := exec.LookPath("setsid"); err != nil {
		t.Skip("setsid is not installed")
	}
	scope := Scope{AttemptID: "attempt_test_123", HostGeneration: "123", LaunchID: "launch_test_123"}
	owned, err := controller.Claim(scope, Limits{MemoryBytes: 64 << 20, CPUMillis: 1000, ProcessCount: 8})
	if err != nil {
		t.Fatal(err)
	}
	defer owned.Close()
	defer owned.Kill(time.Second)
	other := exec.Command("sleep", "10")
	if err := other.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = other.Process.Kill(); _, _ = other.Process.Wait() }()
	cmd := exec.Command("sh", "-c", "setsid sleep 10 >/dev/null 2>&1 & exit")
	cmd.SysProcAttr = &syscall.SysProcAttr{UseCgroupFD: true, CgroupFD: owned.FD()}
	if err := cmd.Run(); err != nil {
		t.Fatal(err)
	}
	view, err := owned.Observe()
	if err != nil || !view.Populated {
		t.Fatalf("escaped-session child must remain in the owned handle: %+v, %v", view, err)
	}
	view, err = owned.Kill(2 * time.Second)
	if err != nil || view.Populated {
		t.Fatalf("the owned cgroup still has processes: %+v, %v", view, err)
	}
	if err := other.Process.Signal(syscall.Signal(0)); err != nil {
		t.Fatalf("unrelated process was affected: %v", err)
	}
	if _, err := controller.Lookup(Scope{AttemptID: scope.AttemptID, HostGeneration: "124", LaunchID: scope.LaunchID}); err == nil {
		t.Fatal("a different generation reached this handle")
	}
}

func TestProcessCeilingReportsKernelRefusal(t *testing.T) {
	controller, err := NewController("testpids")
	if err != nil {
		t.Skipf("no own delegated cgroup: %v", err)
	}
	if status := controller.Probe(); !status.Available {
		t.Skipf("no verified kernel containment: %s", status.Reason)
	}
	scope := Scope{AttemptID: "attempt_pids_123", HostGeneration: "123", LaunchID: "launch_pids_123"}
	owned, err := controller.Claim(scope, Limits{MemoryBytes: 64 << 20, CPUMillis: 1000, ProcessCount: 2})
	if err != nil {
		t.Fatal(err)
	}
	defer owned.Close()
	defer owned.Kill(time.Second)
	cmd := exec.Command("sh", "-c", "sleep 5 & sleep 5 & sleep 5 & wait")
	cmd.SysProcAttr = &syscall.SysProcAttr{UseCgroupFD: true, CgroupFD: owned.FD()}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer cmd.Process.Kill()
	deadline := time.Now().Add(time.Second)
	for readKey(filepath.Join(controller.root, "daedalus-"+controller.instance+"-"+key(scope), "pids.events"), "max") == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	view, err := owned.Observe()
	if err != nil || view.PIDsMaxEvents <= 0 {
		t.Fatalf("the kernel did not enforce the process ceiling: %+v, %v", view, err)
	}
}
