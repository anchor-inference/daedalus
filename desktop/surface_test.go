package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestDeepLinkTargetFindsTheSession(t *testing.T) {
	app := "http://127.0.0.1:8765/app/"
	for _, one := range []struct {
		link string
		want string
	}{
		{"daedalus://open/abc123", "http://127.0.0.1:8765/app/agents/abc123"},
		{"daedalus://session/abc123", "http://127.0.0.1:8765/app/agents/abc123"},
		{"daedalus:///open/abc123", "http://127.0.0.1:8765/app/agents/abc123"},
		{"DAEDALUS://OPEN/abc123", "http://127.0.0.1:8765/app/agents/abc123"},
		// A link with nothing to open, and one that is not ours at all, both land on the app rather
		// than anywhere a link could aim the launcher.
		{"daedalus://open", app},
		{"daedalus://", app},
		{"https://example.invalid/app/agents/x", app},
		{"nonsense", app},
	} {
		if got := DeepLinkTarget(one.link, app); got != one.want {
			t.Fatalf("%s opened %s, want %s", one.link, got, one.want)
		}
	}
	// The language the launcher hands the app rides on a deep link too: it is after the `?`, and
	// the session goes on the path in front of it rather than inside the parameter.
	withLang := "http://127.0.0.1:8765/app/?lang=ru"
	if got := DeepLinkTarget("daedalus://open/abc123", withLang); got != "http://127.0.0.1:8765/app/agents/abc123?lang=ru" {
		t.Fatalf("a deep link lost the language: %s", got)
	}
	// A link arrives from outside, so an id that is not one opens the app and nothing else.
	for _, refused := range []string{"daedalus://open/../../etc", "daedalus://open/a b", "daedalus://open/a%2Fb", "daedalus://open/a\u0000b"} {
		if got := DeepLinkTarget(refused, app); got != app {
			t.Fatalf("%s opened %s", refused, got)
		}
	}
}

func TestDeepLinkIsNotACommand(t *testing.T) {
	opts, err := parseArgs([]string{"daedalus://open/abc"})
	if err != nil || opts.command != "" || opts.link != "daedalus://open/abc" {
		t.Fatalf("%+v %v", opts, err)
	}
	if !IsDeepLink("daedalus://open/abc") || IsDeepLink("open") {
		t.Fatal("a link and a command are told apart by the scheme")
	}
}

func TestSecondLaunchFocusesTheFirst(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	// No launcher is running: there is nothing to focus and nothing to wait for.
	if FocusRunning(context.Background(), paths, "") {
		t.Fatal("an empty folder answered as a running launcher")
	}
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())

	focused := make(chan string, 1)
	server.OnFocus(func(_ context.Context, url string) { focused <- url })
	if !FocusRunning(context.Background(), paths, "daedalus://open/abc123") {
		t.Fatal("the running launcher did not answer")
	}
	select {
	case url := <-focused:
		if !strings.Contains(url, "/app/agents/abc123") {
			t.Fatalf("the link arrived as %s", url)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the link never arrived")
	}

	// The file is the handover, and the token in it is what makes the answer ours. Without it the
	// endpoint refuses, so another program on the port cannot be mistaken for the launcher.
	var found instance
	data, err := os.ReadFile(filepath.Join(paths.Data, "launcher.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &found); err != nil {
		t.Fatal(err)
	}
	if found.Port != server.Port() || found.Token != server.Token() {
		t.Fatalf("the handover names %d, the page listens on %d", found.Port, server.Port())
	}
	if got := post(t, server.URL()+"focus", map[string]string{csrfHeader: "not-the-token"}, nil); got != http.StatusForbidden {
		t.Fatalf("focus without the token answered %d", got)
	}
	server.Stop(context.Background())
	if _, err := os.Stat(filepath.Join(paths.Data, "launcher.json")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("a stopped launcher still claims the installation")
	}
}

func TestTheDesktopEntryIsWrittenWhereTheDesktopLooks(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("the desktop entry is the Linux registration")
	}
	paths := setupTempInstall(t)
	t.Setenv("XDG_DATA_HOME", filepath.Join(t.TempDir(), "share"))
	if err := RegisterScheme(paths); err != nil {
		t.Fatal(err)
	}
	entry, err := os.ReadFile(filepath.Join(os.Getenv("XDG_DATA_HOME"), "applications", "daedalus-desktop.desktop"))
	if err != nil {
		t.Fatal(err)
	}
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	// The path is quoted: unquoted, a launcher living under a path with a space in it writes an
	// Exec= line the desktop splits into arguments, and the links stop working with nothing said.
	for _, want := range []string{"MimeType=x-scheme-handler/daedalus;", "Exec=\"" + exe + "\" %u", "Name=Daedalus"} {
		if !strings.Contains(string(entry), want) {
			t.Fatalf("the desktop entry does not carry %q:\n%s", want, entry)
		}
	}
	// The marker records which executable was registered, so a start that changes nothing does
	// nothing, and a launcher that has moved registers itself again.
	recorded, err := os.ReadFile(filepath.Join(paths.Data, "scheme.txt"))
	if err != nil || strings.TrimSpace(string(recorded)) != exe {
		t.Fatalf("the registration was not recorded: %q %v", recorded, err)
	}
}

func TestFindChromiumLooksWhereTheInstallersPut(t *testing.T) {
	// PATH first: an operator who put a browser there means it.
	onPath := func(name string) (string, error) {
		if name == "google-chrome" {
			return "/opt/bin/google-chrome", nil
		}
		return "", errors.New("not on PATH")
	}
	if got := findChromium("linux", "/home/x", onPath, func(string) bool { return false }); got != "/opt/bin/google-chrome" {
		t.Fatalf("PATH was not tried first: %s", got)
	}
	// Then the install locations, which is the case that matters: a program started from Finder or
	// from Explorer has a PATH with no browser in it.
	none := func(string) (string, error) { return "", errors.New("not on PATH") }
	edge := "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
	if got := findChromium("darwin", "/Users/x", none, func(path string) bool { return path == edge }); got != edge {
		t.Fatalf("the installed browser was not found: %s", got)
	}
	if got := findChromium("windows", "", none, func(path string) bool { return strings.HasSuffix(path, `Google\Chrome\Application\chrome.exe`) }); got == "" {
		t.Fatal("chrome in Program Files was not found")
	}
	// None installed is not an error: the default browser is the last step of the chain.
	if got := findChromium("linux", "", none, func(string) bool { return false }); got != "" {
		t.Fatalf("a machine with no Chromium answered %s", got)
	}
}

// A notification's title and body come from the agent's notifications over the event stream,
// and they are handed to a helper as arguments. One beginning with a dash would be read as an
// option — another -e script for osascript, another flag for notify-send — so it is fenced first.
func TestANotificationCannotBeginWithAnOption(t *testing.T) {
	for _, text := range []string{"-e do shell script \"id\"", "  --title", "-"} {
		if got := fenceDash(text); strings.HasPrefix(got, "-") {
			t.Fatalf("%q was handed on as %q, which a helper reads as an option", text, got)
		}
	}
	if got := fenceDash("A title"); got != "A title" {
		t.Fatalf("an ordinary title was changed to %q", got)
	}
}

// The handover file names the port and carries the token that authorises start, stop and update.
// A launcher that was killed leaves it behind, and by then anything may be on that port.
func TestAHandoverFromADeadLauncherIsNotBelieved(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteInstance(paths, 45999, "a-token"); err != nil {
		t.Fatal(err)
	}
	if _, ok := readInstance(paths); !ok {
		t.Fatal("this launcher's own handover was not believed")
	}
	// A pid no process holds: the file is stale and is removed rather than trusted.
	data, err := json.Marshal(instance{Port: 45999, Token: "a-token", PID: 4194303})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(instanceFile(paths), data, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, ok := readInstance(paths); ok {
		t.Fatal("a handover from a process that is gone was believed")
	}
	if _, err := os.Stat(instanceFile(paths)); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("the stale handover was left where the next start will read it again")
	}
}
