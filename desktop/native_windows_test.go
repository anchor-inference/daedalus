//go:build windows

package main

import (
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"testing"
	"time"
)

func TestWindowsConsoleBreakStopsAHiddenChildGracefully(t *testing.T) {
	if os.Getenv("DAEDALUS_TEST_CONSOLE_WORKER") == "1" {
		interrupt := make(chan os.Signal, 1)
		signal.Notify(interrupt, os.Interrupt)
		defer signal.Stop(interrupt)
		_ = os.WriteFile(os.Getenv("DAEDALUS_TEST_CONSOLE_READY"), []byte("ready"), 0o600)
		select {
		case <-interrupt:
			_ = os.WriteFile(os.Getenv("DAEDALUS_TEST_CONSOLE_STOPPED"), []byte("graceful"), 0o600)
		case <-time.After(20 * time.Second):
		}
		return
	}
	dir := t.TempDir()
	ready, stopped := filepath.Join(dir, "ready"), filepath.Join(dir, "stopped")
	cmd := exec.Command(os.Args[0], "-test.run=^TestWindowsConsoleBreakStopsAHiddenChildGracefully$")
	cmd.Env = append(os.Environ(), "DAEDALUS_TEST_CONSOLE_WORKER=1", "DAEDALUS_TEST_CONSOLE_READY="+ready, "DAEDALUS_TEST_CONSOLE_STOPPED="+stopped)
	setProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	deadline := time.Now().Add(5 * time.Second)
	for !exists(ready) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if !exists(ready) {
		t.Fatal("hidden child did not start")
	}
	start := time.Now()
	terminateGroup(cmd)
	for !exists(stopped) && time.Since(start) < 5*time.Second {
		time.Sleep(20 * time.Millisecond)
	}
	if body, err := os.ReadFile(stopped); err != nil || string(body) != "graceful" {
		t.Fatalf("hidden child did not receive CTRL_BREAK within five seconds: %q, %v", body, err)
	}
}
