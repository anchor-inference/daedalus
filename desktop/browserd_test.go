package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
)

// DAEDALUS_BROWSERD wins; then the daemon beside the launcher, which in a macOS bundle is
// Contents/MacOS; then nothing, and the run directory says why.
func TestTheBrowserDaemonIsFoundWhereTheReleasePutsIt(t *testing.T) {
	name := "browserd"
	if runtime.GOOS == "windows" {
		name = "browserd.exe"
	}
	exe := filepath.Join("apps", "Daedalus.app", "Contents", "MacOS", "daedalus-desktop")
	beside := filepath.Join(filepath.Dir(exe), name)
	env := func(value string) func(string) string {
		return func(key string) string {
			if key == "DAEDALUS_BROWSERD" {
				return value
			}
			return ""
		}
	}
	has := func(p string) bool { return p == beside }
	if got := browserdBinary(exe, env("/built/browserd"), has); got != "/built/browserd" {
		t.Fatalf("the environment's daemon: %q", got)
	}
	if got := browserdBinary(exe, env(""), has); got != beside {
		t.Fatalf("the packaged daemon: %q", got)
	}
	if got := browserdBinary(exe, env(""), func(string) bool { return false }); got != "" {
		t.Fatalf("no daemon: %q", got)
	}
}

func TestTheBrowserDaemonRunsInTheSealedRuntimeAsTheHostEnvironment(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	// The run directory and the profiles are both under the local state folder, which the agent's
	// policy seals whole, and neither is in the data folder an upgrade copies.
	for _, dir := range []string{browserdRunDir(paths), browserdStateDir(paths)} {
		if !within(dir, paths.Local) || within(dir, paths.Data) {
			t.Fatalf("%s is not in the sealed local state folder", dir)
		}
	}
	if env := envMap(supervisorEnv(paths, nil, nil)); env["DAEDALUS_LOCAL"] != paths.Local {
		t.Fatalf("the agent is not told which folder to seal: %q", env["DAEDALUS_LOCAL"])
	}
	argv := browserdArgv("/opt/browserd", paths, nil)
	want := []string{"/opt/browserd", "serve", "--env", "host", "--run-dir", browserdRunDir(paths), "--state-dir", browserdStateDir(paths), "--listen"}
	if !slices.Equal(argv[:len(want)], want) {
		t.Fatalf("argv %v", argv)
	}
	scoped := browserdArgv("/opt/browserd", paths, []string{"systemd-run", "--scope", "--"})
	if !slices.Equal(scoped[:3], []string{"systemd-run", "--scope", "--"}) || scoped[3] != "/opt/browserd" {
		t.Fatalf("scoped argv %v", scoped)
	}
	// The agent is told where the daemon is, whether or not there is one.
	env := envMap(supervisorEnv(paths, nil, map[string]string{}))
	if env["BROWSER_HOST_DIR"] != browserdRunDir(paths) {
		t.Fatalf("BROWSER_HOST_DIR = %q", env["BROWSER_HOST_DIR"])
	}
	if browserdListen(`C:\d`) != "tcp:127.0.0.1:0" && runtime.GOOS == "windows" {
		t.Fatal("Windows has no unix socket for the daemon")
	}
	if runtime.GOOS != "windows" {
		if got := browserdListen("/d/runtime/browserd/run"); got != "unix" {
			t.Fatalf("a short run directory: %s", got)
		}
		if got := browserdListen("/" + strings.Repeat("deep/", 20) + "runtime/browserd/run"); got != "tcp:127.0.0.1:0" {
			t.Fatalf("a deep run directory: %s", got)
		}
	}
}

func TestTheBrowserDaemonFindsTheInstalledBrowsersAndASandboxHelper(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	base := []string{"HOME=/home/someone", "PLAYWRIGHT_BROWSERS_PATH=/somewhere/else"}
	helper := func(p string) bool { return p == "/usr/lib/chromium/chrome-sandbox" }
	env := envMap(browserdEnv(paths, base, helper))
	// The installation's own folder, not whatever the operator's shell said, and even before
	// anything is installed there.
	if env["PLAYWRIGHT_BROWSERS_PATH"] != paths.RuntimeBrowsers {
		t.Fatalf("PLAYWRIGHT_BROWSERS_PATH = %q", env["PLAYWRIGHT_BROWSERS_PATH"])
	}
	if runtime.GOOS == "linux" {
		if env["CHROME_DEVEL_SANDBOX"] != "/usr/lib/chromium/chrome-sandbox" {
			t.Fatalf("the sandbox helper: %q", env["CHROME_DEVEL_SANDBOX"])
		}
		// One the operator named is kept.
		mine := envMap(browserdEnv(paths, append(base, "CHROME_DEVEL_SANDBOX=/mine"), helper))
		if mine["CHROME_DEVEL_SANDBOX"] != "/mine" {
			t.Fatalf("the operator's helper was replaced: %q", mine["CHROME_DEVEL_SANDBOX"])
		}
	}
	none := envMap(browserdEnv(paths, base, func(string) bool { return false }))
	if _, ok := none["CHROME_DEVEL_SANDBOX"]; ok {
		t.Fatal("a helper named where there is none")
	}
}

// The scope that caps the browser's memory is used only when starting something in it works.
func TestTheMemoryScopeIsProbedBeforeItIsUsed(t *testing.T) {
	found := func(string) (string, error) { return "/usr/bin/systemd-run", nil }
	missing := func(string) (string, error) { return "", errors.New("not found") }
	var probed []string
	works := func(_ context.Context, argv []string) error { probed = argv; return nil }
	fails := func(context.Context, []string) error { return errors.New("no user manager") }
	scope := systemScope(context.Background(), found, works)
	if runtime.GOOS != "linux" {
		if scope != nil {
			t.Fatalf("a scope off Linux: %v", scope)
		}
		return
	}
	if len(scope) == 0 || scope[0] != "/usr/bin/systemd-run" || scope[len(scope)-1] != "--" || !slices.Contains(scope, "MemoryMax="+browserdMemoryMax) || !slices.Contains(scope, "MemorySwapMax=0") {
		t.Fatalf("scope %v", scope)
	}
	if probed[len(probed)-1] != "true" {
		t.Fatalf("the probe ran %v", probed)
	}
	if systemScope(context.Background(), found, fails) != nil || systemScope(context.Background(), missing, works) != nil {
		t.Fatal("a scope that cannot start anything was used")
	}
}

// The daemon works in the local state folder, never in the data folder: a daemon that outlived a
// crashed launcher with its working directory in the data folder held every later switch and
// restore of it off.
func TestTheBrowserDaemonDoesNotWorkInTheDataFolder(t *testing.T) {
	p := fixtureData(t)
	binary := filepath.Join(t.TempDir(), "browserd")
	if err := os.WriteFile(binary, []byte("a daemon"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DAEDALUS_BROWSERD", binary)
	child := NewNative(p, func(string, ...any) {}).newBrowserd(context.Background())
	proc, ok := child.(*Process)
	if !ok || proc == nil {
		t.Fatalf("no browser daemon process: %#v", child)
	}
	if proc.Dir != p.Local || within(proc.Dir, p.Data) {
		t.Fatalf("the daemon works in %s", proc.Dir)
	}
}
