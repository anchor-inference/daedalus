package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

// Every event is one JSON object on one line, whole, whichever goroutine wrote it: the shell reads
// lines, and a line torn by another is one it cannot read.
func TestTheShellIsToldOneWholeLinePerEvent(t *testing.T) {
	var out bytes.Buffer
	link := &shellLink{out: &out}
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			link.emit(shellEvent{Event: "show", URL: "http://127.0.0.1:8770/"})
		}()
	}
	wg.Wait()
	lines := strings.Split(strings.TrimSuffix(out.String(), "\n"), "\n")
	if len(lines) != 50 {
		t.Fatalf("%d lines for 50 events", len(lines))
	}
	for _, line := range lines {
		var event shellEvent
		if err := json.Unmarshal([]byte(line), &event); err != nil || event.Event != "show" {
			t.Fatalf("a line the shell cannot read: %q (%v)", line, err)
		}
	}
	var none *shellLink
	none.emit(shellEvent{Event: "show"})
}

// A focus carries its link through; quit, and the end of the input, both stop the launcher — a
// shell that died must not leave an agent running behind it.
func TestTheShellsCommandsAreHeardAndItsGoingAwayIsAQuit(t *testing.T) {
	link := &shellLink{out: &bytes.Buffer{}}
	var links []string
	quits := 0
	link.listen(strings.NewReader(`{"command":"focus","link":"daedalus://open/abc"}`+"\nnot json\n"+`{"command":"quit"}`+"\n"+`{"command":"focus"}`+"\n"),
		func(l string) { links = append(links, l) }, func() {}, func() { quits++ })
	if len(links) != 1 || links[0] != "daedalus://open/abc" || quits != 1 {
		t.Fatalf("links %v, quits %d", links, quits)
	}
	signIns := 0
	link.listen(strings.NewReader(`{"command":"sign-in"}`+"\n"), func(string) {}, func() { signIns++ }, func() {})
	if signIns != 1 {
		t.Fatal("the window asked to be signed in and the launcher did not hear it")
	}
	quits = 0
	link.listen(strings.NewReader(""), func(string) {}, func() {}, func() { quits++ })
	if quits != 1 {
		t.Fatal("the end of the shell's input did not stop the launcher")
	}
}

func TestShellIsAFlagOnlyTheStartTakes(t *testing.T) {
	opts, err := parseArgs([]string{"--shell"})
	if err != nil || !opts.shell || opts.command != "" {
		t.Fatalf("%+v %v", opts, err)
	}
	defer func() { shell = nil }()
	if err := run([]string{"--shell", "uninstall"}); err == nil || !strings.Contains(err.Error(), "terminal") {
		t.Fatalf("uninstall ran under the shell: %v", err)
	}
}

// The shell is found beside the launcher under the name the installers give it, and a launcher
// with nothing beside it is a launcher on its own.
func TestTheShellIsFoundBesideTheLauncher(t *testing.T) {
	dir := t.TempDir()
	exe := filepath.Join(dir, "daedalus-desktop")
	if shellExecutable(exe) != "" {
		t.Fatal("a shell was found where there is none")
	}
	name := map[string]string{"windows": "Daedalus.exe", "darwin": "Daedalus"}[runtime.GOOS]
	if name == "" {
		name = "daedalus"
	}
	if err := os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := shellExecutable(exe); got != filepath.Join(dir, name) {
		t.Fatalf("the shell is %q", got)
	}
}

// A copy the operator cannot write — a package's files, an AppImage's mount — is told to take the
// release's own file, before anything is asked or written.
func TestACopyThatCannotReplaceItselfSaysSo(t *testing.T) {
	dir := t.TempDir()
	if why := notSelfReplaceable(dir, func(string) string { return "" }); why != "" {
		t.Fatalf("a writable folder was refused: %s", why)
	}
	if entries, _ := os.ReadDir(dir); len(entries) != 0 {
		t.Fatal("the check left something behind")
	}
	appimage := func(name string) string {
		if name == "APPIMAGE" {
			return "/home/someone/Daedalus.AppImage"
		}
		return ""
	}
	if why := notSelfReplaceable(dir, appimage); !strings.Contains(why, "AppImage") {
		t.Fatalf("an AppImage was not named: %q", why)
	}
	if runtime.GOOS != "windows" && os.Geteuid() != 0 {
		locked := filepath.Join(t.TempDir(), "opt")
		if err := os.Mkdir(locked, 0o555); err != nil {
			t.Fatal(err)
		}
		if why := notSelfReplaceable(locked, func(string) string { return "" }); !strings.Contains(why, "package") {
			t.Fatalf("a folder this user cannot write was not refused: %q", why)
		}
	}
}

// Under the shell a notification names the conversation it is about by its address in the app.
func TestANotificationUnderTheShellCarriesItsAddress(t *testing.T) {
	paths := setupTempInstall(t)
	app := NewApp(paths)
	got := notificationTarget(app, Notification{Session: "abc-1"})
	if !strings.Contains(got, "/agents/abc-1") {
		t.Fatalf("the click would show %q", got)
	}
	if notificationTarget(app, Notification{Session: "../x"}) != "" {
		t.Fatal("a session id from the network was trusted")
	}
	if notificationTarget(app, Notification{Link: "http://127.0.0.1:1/x"}) != "http://127.0.0.1:1/x" {
		t.Fatal("a notification's own address was not used")
	}
}

// The page's Install and restart is handed to the shell — never run inside the launcher it would
// replace — and only when there is a shell, a release to install, and a mode upgrade does.
func TestInstallingAnUpgradeIsTheShellsToDo(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{}, ModeNative); err != nil {
		t.Fatal(err)
	}
	app := NewApp(paths)
	app.SetMode(ModeNative)
	server := NewServer(app, 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())
	token := map[string]string{csrfHeader: server.csrf}
	defer func() { shell = nil }()

	shell = nil
	if got := post(t, server.URL()+"api/action/upgrade", token, nil); got != http.StatusConflict {
		t.Fatalf("without a shell the action answered %d", got)
	}
	var out bytes.Buffer
	shell = &shellLink{out: &out}
	if got := post(t, server.URL()+"api/action/upgrade", token, nil); got != http.StatusConflict {
		t.Fatalf("with nothing to install the action answered %d", got)
	}
	app.mu.Lock()
	app.offer = &Offer{From: "desktop-v0.13.0", To: "desktop-v0.14.0"}
	app.mu.Unlock()
	if got := post(t, server.URL()+"api/action/upgrade", token, nil); got != http.StatusAccepted {
		t.Fatalf("the action answered %d", got)
	}
	var event shellEvent
	if err := json.Unmarshal(bytes.TrimSpace(out.Bytes()), &event); err != nil || event.Event != "upgrade" || event.Data != paths.Data {
		t.Fatalf("the shell was told %q (%v)", out.String(), err)
	}

	out.Reset()
	app.SetMode(ModeDocker)
	if got := post(t, server.URL()+"api/action/upgrade", token, nil); got != http.StatusConflict || out.Len() != 0 {
		t.Fatalf("Docker mode answered %d and told the shell %q", got, out.String())
	}
}
