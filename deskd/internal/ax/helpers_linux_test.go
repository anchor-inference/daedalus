//go:build linux

package ax_test

import (
	"bufio"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/ax"
	"github.com/ascorblack/daedalus/deskd/internal/ax/axfake"
)

// The helper search is proved on Linux, through /proc, with stand-ins for the helper the tool
// leaves on macOS; the darwin half differs only in how it reads a process's environment.
func TestTheToolsLingeringHelperGoesWithItsLease(t *testing.T) {
	ctx := context.Background()
	cfg, _ := fakeTool(t, axfake.Scene{})
	tool := ax.Open(ctx, cfg)
	if _, err := tool.Run(ctx, "l1", time.Second, ax.ListWindowsArgs()...); err != nil {
		t.Fatal(err)
	}
	// Stand-ins for the helper the tool leaves on macOS: in a process group of their own, with the
	// tool's internal marker and the state root they inherited. The third is another program's copy
	// of the tool, which must be left alone.
	root := filepath.Join(cfg.StateDir, "l1")
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	// Each stand-in is this test binary, and the search starts only once every one has said it is
	// running. Until the kernel has finished an exec, the new process's /proc/<pid>/environ reads
	// empty, though Start has already returned: Start waits only for the exec's point of no return,
	// and the environment is laid out after it. That takes some 50 µs with the binary in the page
	// cache and milliseconds when it is not, as on a fresh CI machine running sleep for the first
	// time, where the search read three empty environments, matched nothing, and the test waited
	// out its 2 s for a kill that was never sent.
	start := func(env ...string) *exec.Cmd {
		cmd := exec.Command(self)
		cmd.Env = append(os.Environ(), append([]string{standInEnvName + "=1"}, env...)...)
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		out, err := cmd.StdoutPipe()
		if err != nil {
			t.Fatal(err)
		}
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = cmd.Process.Kill(); _, _ = cmd.Process.Wait() })
		line, err := bufio.NewReader(out).ReadString('\n')
		if err != nil || line != "ready\n" {
			t.Fatalf("the stand-in did not start: %q %v", line, err)
		}
		return cmd
	}
	ours := start("AGENT_DESKTOP_INTERNAL_COMMAND_HOST=1", "AGENT_DESKTOP_HOME="+root)
	later := start("AGENT_DESKTOP_INTERNAL_COMMAND_HOST=1", "AGENT_DESKTOP_HOME="+filepath.Join(cfg.StateDir, "l2"))
	foreign := start("AGENT_DESKTOP_INTERNAL_COMMAND_HOST=1", "AGENT_DESKTOP_HOME="+t.TempDir())
	exited := func(c *exec.Cmd) bool {
		done := make(chan struct{})
		go func() { _, _ = c.Process.Wait(); close(done) }()
		select {
		case <-done:
			return true
		case <-time.After(2 * time.Second):
			return false
		}
	}
	tool.EndLease("l1")
	if !exited(ours) {
		t.Fatal("the lease's helper outlived it")
	}
	tool.Close()
	if !exited(later) {
		t.Fatal("a helper outlived the daemon")
	}
	if err := foreign.Process.Signal(syscall.Signal(0)); err != nil {
		t.Fatalf("another program's helper was stopped: %v", err)
	}
	if _, err := os.Stat(cfg.StateDir); !os.IsNotExist(err) {
		t.Fatalf("the tool's state outlived the daemon: %v", err)
	}
	if ax.IsHelperOf([]string{"AGENT_DESKTOP_HOME=" + root + "-other", "AGENT_DESKTOP_INTERNAL_COMMAND_HOST=1"}, root) ||
		ax.IsHelperOf([]string{"AGENT_DESKTOP_HOME=" + root}, root) {
		t.Fatal("a process that is not the lease's helper matched")
	}
}
