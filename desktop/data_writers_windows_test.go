//go:build windows

package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestWindowsDataWriterWorker(t *testing.T) {
	file := os.Getenv("DAEDALUS_WRITER_TEST_FILE")
	if file == "" {
		t.Skip("worker only")
	}
	f, err := os.OpenFile(file, os.O_WRONLY|os.O_APPEND, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := os.WriteFile(os.Getenv("DAEDALUS_WRITER_TEST_READY"), []byte("ready"), 0o600); err != nil {
		t.Fatal(err)
	}
	for {
		_, _ = f.WriteString("live\n")
		time.Sleep(30 * time.Millisecond)
	}
}

func TestWindowsWriterBlocksBackupGateUntilStopped(t *testing.T) {
	p, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(p.Workspaces, "existing.log")
	if err := os.MkdirAll(p.Workspaces, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(file, []byte("before\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ready := filepath.Join(t.TempDir(), "ready")
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(exe, "-test.run=^TestWindowsDataWriterWorker$")
	cmd.Env = append(os.Environ(), "DAEDALUS_WRITER_TEST_FILE="+file, "DAEDALUS_WRITER_TEST_READY="+ready)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	for i := 0; i < 100 && !exists(ready); i++ {
		time.Sleep(20 * time.Millisecond)
	}
	if !exists(ready) {
		t.Fatal("writer did not start")
	}
	if err := quiesceData(context.Background(), p); err == nil || !strings.Contains(err.Error(), "existing.log") {
		t.Fatalf("live writer was not refused: %v", err)
	}
	if exists(backupsDir(p)) {
		t.Fatal("backup started while writer was alive")
	}
	_ = cmd.Process.Kill()
	_ = cmd.Wait()
	if err := quiesceData(context.Background(), p); err != nil {
		t.Fatalf("gate stayed closed after writer stopped: %v", err)
	}
}
