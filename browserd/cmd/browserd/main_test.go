//go:build unix

package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/chrome"
	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
)

// TestServeAndStop runs the daemon as a program: the run directory, the handshake, a second daemon
// refused, a browser opened, and every Chromium process gone after SIGTERM.
func TestServeAndStop(t *testing.T) {
	if _, ok := chrome.Find(""); !ok {
		if os.Getenv("BROWSERD_REQUIRE_CHROMIUM") != "" {
			t.Fatal("no Chromium, and the gate requires one")
		}
		t.Skip("no Chromium here")
	}
	dir := t.TempDir()
	bin := filepath.Join(dir, "browserd")
	if out, err := exec.Command("go", "build", "-o", bin, ".").CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	run, state := filepath.Join(dir, "run"), filepath.Join(dir, "state")
	cmd := exec.Command(bin, "serve", "--env", "test", "--run-dir", run, "--state-dir", state)
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer cmd.Process.Kill()
	deadline := time.Now().Add(10 * time.Second)
	for {
		if _, err := os.Stat(filepath.Join(run, "endpoint")); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("no endpoint")
		}
		time.Sleep(50 * time.Millisecond)
	}
	for name, mode := range map[string]os.FileMode{"token": 0o600, "browserd.sock": 0o600} {
		st, err := os.Stat(filepath.Join(run, name))
		if err != nil || st.Mode().Perm() != mode {
			t.Fatalf("%s: %v %v", name, st, err)
		}
	}
	out, err := exec.Command(bin, "serve", "--env", "test", "--run-dir", run, "--state-dir", state).CombinedOutput()
	if err == nil || !strings.Contains(string(out), "another browserd holds the run directory") {
		t.Fatalf("a second daemon: %v %s", err, out)
	}
	c, err := clienttest.Dial(run)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if !strings.Contains(string(c.Hello), `"protocol":1`) || !strings.Contains(string(c.Hello), `"env":"test"`) {
		t.Fatalf("hello: %s", c.Hello)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if err := c.Call(ctx, "browser.open", map[string]any{"group_id": "g", "profile": "p"}, nil); err != nil {
		t.Fatal(err)
	}
	var list struct {
		Browsers []struct {
			Pid int `json:"pid"`
		} `json:"browsers"`
	}
	if err := c.Call(ctx, "browser.list", nil, &list); err != nil || len(list.Browsers) != 1 {
		t.Fatalf("list: %+v %v", list, err)
	}
	chromePid := list.Browsers[0].Pid
	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("exit: %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("the daemon did not stop")
	}
	// Nothing of the browser is left: not its main process, nor any process of its session.
	if alive(chromePid) {
		t.Fatalf("Chromium %d outlived the daemon", chromePid)
	}
	if sessionMembers(chromePid) > 0 {
		t.Fatal("a Chromium helper outlived the daemon")
	}
	for _, name := range []string{"endpoint", "token", "browserd.sock"} {
		if _, err := os.Stat(filepath.Join(run, name)); !os.IsNotExist(err) {
			t.Fatalf("%s left behind", name)
		}
	}
}

func alive(pid int) bool {
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return false
	}
	// A zombie waiting for its parent is not running.
	i := strings.LastIndexByte(string(data), ')')
	return i > 0 && !strings.HasPrefix(strings.TrimSpace(string(data[i+1:])), "Z")
}

func sessionMembers(sid int) int {
	entries, _ := os.ReadDir("/proc")
	n := 0
	for _, e := range entries {
		data, err := os.ReadFile("/proc/" + e.Name() + "/stat")
		if err != nil {
			continue
		}
		i := strings.LastIndexByte(string(data), ')')
		f := strings.Fields(string(data[i+1:]))
		if len(f) > 3 && f[3] == strconv.Itoa(sid) && f[0] != "Z" {
			n++
		}
	}
	return n
}

