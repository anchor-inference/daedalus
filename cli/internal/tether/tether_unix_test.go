//go:build unix

package tether

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	lexec "github.com/ascorblack/daedalus/cli/internal/exec"
)

// envHelper makes the test binary a stand-in for the client: it ties an executor the way main does
// and runs one command through it, which the test then outlives by killing the stand-in.
const envHelper = "TETHER_TEST_CLIENT"

func TestMain(m *testing.M) {
	if Child() {
		os.Exit(Watch(os.Stdin))
	}
	if mode := os.Getenv(envHelper); mode != "" {
		os.Exit(client(mode, os.Getenv("TETHER_TEST_DIR")))
	}
	os.Exit(m.Run())
}

// client runs `command` in dir through a tied executor and never returns while it runs. "full" ties
// it to a real watchdog; "leader-only" ties it to a tether that registers nothing, which leaves the
// kernel's parent-death signal as the only thing that ends the command.
func client(mode, dir string) int {
	ex, err := lexec.New(nil)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	switch mode {
	case "full":
		t, err := Start()
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 2
		}
		ex.Tie(t)
	case "leader-only":
		ex.Tie(nothing{})
	}
	_, err = ex.Shell(context.Background(), dir, os.Getenv("TETHER_TEST_COMMAND"), 5*time.Minute)
	fmt.Fprintln(os.Stderr, "the command ended under a live client:", err)
	return 3
}

type nothing struct{}

func (nothing) Hold(int) func() { return func() {} }

// startClient starts the stand-in and waits until the command has written its pids into dir.
func startClient(t *testing.T, mode, command string, files ...string) (*exec.Cmd, []int) {
	t.Helper()
	dir := t.TempDir()
	cmd := exec.Command(os.Args[0], "-test.run=^$")
	cmd.Env = append(os.Environ(), envHelper+"="+mode, "TETHER_TEST_DIR="+dir, "TETHER_TEST_COMMAND="+command)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})
	pids := make([]int, len(files))
	deadline := time.Now().Add(20 * time.Second)
	for i, name := range files {
		for {
			body, err := os.ReadFile(filepath.Join(dir, name))
			if n, convErr := strconv.Atoi(strings.TrimSpace(string(body))); err == nil && convErr == nil && n > 1 {
				pids[i] = n
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("the command never wrote %s; the stand-in said: %s", name, stderr.String())
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
	for _, pid := range pids {
		pid := pid
		t.Cleanup(func() { _ = syscall.Kill(pid, syscall.SIGKILL) })
	}
	return cmd, pids
}

// alive says whether pid is a process that still runs. A zombie has ended: an orphan is reparented
// to whatever adopts it, and in a container that may be a process that never reaps.
func alive(pid int) bool {
	if err := syscall.Kill(pid, 0); err != nil {
		return false
	}
	if runtime.GOOS != "linux" {
		return true
	}
	stat, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return false
	}
	fields := strings.Fields(string(stat[bytes.LastIndexByte(stat, ')')+1:]))
	return len(fields) > 0 && fields[0] != "Z" && fields[0] != "X"
}

func waitGone(t *testing.T, what string, pid int, within time.Duration) {
	t.Helper()
	deadline := time.Now().Add(within)
	for alive(pid) {
		if time.Now().After(deadline) {
			t.Fatalf("%s (pid %d) still runs %s after its client was killed", what, pid, within)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// The reported case: `kill -9` of the client while a command runs. The command's shell and the
// program it started in the background both end, not just the shell.
func TestAKilledClientTakesItsCommandsWithIt(t *testing.T) {
	client, pids := startClient(t, "full", "echo $$ > leader; sleep 300 & echo $! > child; wait", "leader", "child")
	if err := client.Process.Signal(syscall.SIGKILL); err != nil {
		t.Fatal(err)
	}
	waitGone(t, "the command's shell", pids[0], 10*time.Second)
	waitGone(t, "the program it started", pids[1], 10*time.Second)
}

// The watchdog alone ends a whole group: the parent-death signal reaches only the leader, and this
// group's leader is a shell that ignores the hangup it is sent, so its background child goes only
// by the watchdog's kill.
func TestTheWatchdogEndsTheWholeGroupWhenTheClientGoes(t *testing.T) {
	tether, err := Start()
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	group := exec.Command("sh", "-c", "trap '' HUP; sleep 300 & echo $! > child; wait")
	group.Dir = dir
	group.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := group.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = syscall.Kill(-group.Process.Pid, syscall.SIGKILL); _ = group.Wait() })
	tether.Hold(group.Process.Pid)

	finished := exec.Command("sleep", "300")
	finished.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := finished.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = finished.Process.Kill(); _ = finished.Wait() })
	tether.Hold(finished.Process.Pid)()

	var child int
	for deadline := time.Now().Add(10 * time.Second); child == 0; time.Sleep(20 * time.Millisecond) {
		body, _ := os.ReadFile(filepath.Join(dir, "child"))
		child, _ = strconv.Atoi(strings.TrimSpace(string(body)))
		if time.Now().After(deadline) {
			t.Fatal("the group never started its child")
		}
	}
	t.Cleanup(func() { _ = syscall.Kill(child, syscall.SIGKILL) })

	tether.Close()
	select {
	case <-tether.Done():
	case <-time.After(10 * time.Second):
		t.Fatal("the watchdog did not exit when its client's end closed")
	}
	waitGone(t, "the group's background child", child, time.Second)
	exited := make(chan struct{})
	go func() { _ = group.Wait(); close(exited) }()
	select {
	case <-exited:
	case <-time.After(5 * time.Second):
		t.Fatal("the group's leader still runs")
	}
	// A group let go of before the client went is not the watchdog's to end.
	if !alive(finished.Process.Pid) {
		t.Fatal("the watchdog ended a group that had been released")
	}
}

// A SIGTERM aimed at the watchdog (a `pkill` of the client's name finds it too) ends the commands
// rather than orphaning them; the terminal's own signals do not touch it.
func TestTheWatchdogEndsItsGroupsOnSIGTERMAndIgnoresTheTerminal(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	cmd := exec.Command(os.Args[0], "-test.run=^$")
	cmd.Env = append(os.Environ(), envChild+"=1")
	cmd.Stdin = r
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	r.Close()
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
	group := exec.Command("sleep", "300")
	group.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := group.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = group.Process.Kill(); _ = group.Wait() })
	fmt.Fprintf(w, "+%d\n", group.Process.Pid)
	// Give the watchdog time to install its handlers and read the line before it is signalled.
	time.Sleep(time.Second)
	_ = cmd.Process.Signal(syscall.SIGINT)
	_ = cmd.Process.Signal(syscall.SIGHUP)
	time.Sleep(200 * time.Millisecond)
	if !alive(group.Process.Pid) {
		t.Fatal("the terminal's signals reached the command through the watchdog")
	}
	_ = cmd.Process.Signal(syscall.SIGTERM)
	exited := make(chan struct{})
	go func() { _ = group.Wait(); close(exited) }()
	select {
	case <-exited:
	case <-time.After(5 * time.Second):
		t.Fatal("the watchdog did not end its group on SIGTERM")
	}
}
