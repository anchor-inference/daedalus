package main

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func readProgress(t *testing.T, url string) upgradeProgress {
	t.Helper()
	response, err := http.Get(url + "progress")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var p upgradeProgress
	if err := json.NewDecoder(response.Body).Decode(&p); err != nil {
		t.Fatal(err)
	}
	return p
}

// The window an upgrade from the app opens says which step is under way, ticks off the ones before
// it, carries the upgrade's last lines, and is in the installation's language.
func TestTheUpgradeWindowFollowsTheSteps(t *testing.T) {
	w, url := serveUpgradeWindow(LangRU)
	if url == "" {
		t.Fatal("the window's page did not start")
	}
	w.SetTarget("desktop-v0.15.2")
	w.Step("backup")
	_, _ = io.WriteString(w, "Stopping the stack...\nBacking up the data...\n")
	p := readProgress(t, url)
	if p.To != "0.15.2" || p.Step != "backup" || p.State != "running" || strings.Join(p.Done, ",") != "download,verify,prepare,stop" {
		t.Fatalf("progress %+v", p)
	}
	if len(p.Lines) != 2 || p.Lines[1] != "Backing up the data..." {
		t.Fatalf("lines %v", p.Lines)
	}
	response, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	page, _ := io.ReadAll(response.Body)
	response.Body.Close()
	if !strings.Contains(string(page), `lang="ru"`) || !strings.Contains(string(page), messages[LangRU]["upgradewin.step.backup"]) {
		t.Fatal("the page is not in the installation's language")
	}

	// The end is read by the page before the process goes: Finish waits for that read.
	finished := make(chan struct{})
	go func() { w.Finish(errors.New("the stack did not come up")); close(finished) }()
	time.Sleep(50 * time.Millisecond)
	if p := readProgress(t, url); p.State != "failed" || p.Message != "the stack did not come up" {
		t.Fatalf("after a failure the page reads %+v", p)
	}
	select {
	case <-finished:
	case <-time.After(3 * time.Second):
		t.Fatal("Finish kept waiting after the page had read the end")
	}
}

// A nil window is what a terminal upgrade has; every call on it is a no-op.
func TestATerminalUpgradeHasNoWindow(t *testing.T) {
	var w *upgradeWindow
	w.Step("download")
	w.SetTarget("desktop-v1.0.0")
	_, _ = io.WriteString(w, "line\n")
	w.Finish(nil)
}
