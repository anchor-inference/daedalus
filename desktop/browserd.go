package main

// The browser daemon in native mode: the agent's browser on the operator's own machine. The launcher
// starts it as a child of its own beside the terminal daemon, for the same reason: the supervisor
// restarts the agent whenever a change is applied, and a browser the operator signed in to must
// outlive that. Quitting the launcher stops it, and with it every browser; the profiles, and so the
// logins made in them, stay in its state directory.
//
// Natively there is no container around it. Its network wall — the proxy inside the daemon that
// every page's connection goes through — is the only wall between a page and this machine's ports
// and LAN, and the doctor says so.

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// noBrowserdReason is what the run directory says when this build carries no browser daemon.
const noBrowserdReason = "this build carries no browserd; the agent's browser is unavailable"

// browserdMemoryMax caps the daemon and every Chromium it starts, on Linux where the operator's
// systemd can hold them in a scope. The same figure as the compose service's limit: two browsers
// with eight tabs each measured under 1.2 GB, and a runaway page must not take the desktop with it.
const browserdMemoryMax = "3G"

// browserdRunDir holds the daemon's endpoint and token, and browserdStateDir the agent's profiles,
// downloads and uploads. Both are inside the local state folder, which the agent's policy seals
// whole: the token drives browsers holding the logins made in them, and the profiles are those
// logins. They were never in the backups and are not in what an upgrade copies; the migration out
// of the data folder carries them over rather than rebuilding them.
func browserdRunDir(p Paths) string   { return filepath.Join(p.Local, "browserd", "run") }
func browserdStateDir(p Paths) string { return filepath.Join(p.Local, "browserd", "state") }

// browserdBinary is the daemon this launcher runs: DAEDALUS_BROWSERD when it is set (a daemon built
// by hand), else the browserd packaged beside the launcher's own executable. "" when there is none.
func browserdBinary(exe string, getenv func(string) string, exists func(string) bool) string {
	if p := getenv("DAEDALUS_BROWSERD"); p != "" {
		return p
	}
	if exe == "" {
		return ""
	}
	name := "browserd"
	if runtime.GOOS == "windows" {
		name = "browserd.exe"
	}
	if p := filepath.Join(filepath.Dir(exe), name); exists(p) {
		return p
	}
	return ""
}

// browserdListen is where the daemon listens, by ptyd's rule: its socket, or loopback TCP on Windows
// and where the socket's path would be too long.
func browserdListen(runDir string) string {
	if runtime.GOOS == "windows" || len(filepath.Join(runDir, "browserd.sock")) > 100 {
		return "tcp:127.0.0.1:0"
	}
	return "unix"
}

// browserdArgv is the daemon's command line, inside a memory-capped scope when scope is not empty.
func browserdArgv(binary string, p Paths, scope []string) []string {
	run := browserdRunDir(p)
	argv := []string{binary, "serve", "--env", "host", "--run-dir", run, "--state-dir", browserdStateDir(p), "--listen", browserdListen(run)}
	return append(append([]string(nil), scope...), argv...)
}

// systemScope is the prefix that starts a command in a transient systemd scope of the operator's
// with a memory limit, or nil where there is no such thing: not Linux, no systemd-run, or no user
// manager to ask (a session without one, a container). It is probed once, by starting `true` the same
// way; a prefix that cannot start anything would leave the daemon restarting for ever.
func systemScope(ctx context.Context, lookPath func(string) (string, error), probe func(ctx context.Context, argv []string) error) []string {
	if runtime.GOOS != "linux" {
		return nil
	}
	run, err := lookPath("systemd-run")
	if err != nil {
		return nil
	}
	scope := []string{run, "--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=" + browserdMemoryMax, "-p", "MemorySwapMax=0", "--"}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if probe(ctx, append(append([]string(nil), scope...), "true")) != nil {
		return nil
	}
	return scope
}

func runProbe(ctx context.Context, argv []string) error {
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = nil, nil, nil
	return cmd.Run()
}

// sandboxHelpers are the setuid sandbox helpers a system Chrome or Chromium installs. Chromium's own
// sandbox needs either unprivileged user namespaces or such a helper, and Ubuntu 23.10 and later
// refuse the namespaces to a downloaded Chromium through AppArmor: without a helper it will not start
// with its sandbox on, and the daemon never turns it off by itself.
var sandboxHelpers = []string{
	"/opt/google/chrome/chrome-sandbox",
	"/opt/google/chrome-beta/chrome-sandbox",
	"/opt/microsoft/msedge/msedge-sandbox",
	"/usr/lib/chromium/chrome-sandbox",
	"/usr/lib/chromium-browser/chrome-sandbox",
}

// setuidRoot reports whether path is a setuid executable owned by root: the only kind of helper
// Chromium can use, and the only kind worth naming to it.
type setuidRoot func(path string) bool

// browserdEnv is what the daemon runs with: the operator's environment without the runtime's Python
// variables, the folder the browsers are installed into, and a sandbox helper where Linux needs one.
func browserdEnv(p Paths, base []string, helper setuidRoot) []string {
	env := make([]string, 0, len(base)+2)
	named := false
	for _, kv := range base {
		key, _, _ := strings.Cut(kv, "=")
		if strings.EqualFold(key, "PLAYWRIGHT_BROWSERS_PATH") {
			continue
		}
		if key == "CHROME_DEVEL_SANDBOX" {
			named = true
		}
		env = append(env, kv)
	}
	// Always given, installed or not: `install browser` fills the folder while the daemon runs, and
	// the daemon looks for Chromium when it starts a browser, not only when it starts itself.
	env = append(env, "PLAYWRIGHT_BROWSERS_PATH="+p.RuntimeBrowsers)
	if runtime.GOOS == "linux" && !named {
		for _, h := range sandboxHelpers {
			if helper(h) {
				env = append(env, "CHROME_DEVEL_SANDBOX="+h)
				break
			}
		}
	}
	return env
}

// newBrowserd is the browser daemon's process, or nil when this build carries none; then the run
// directory is left saying so, and the app shows the browser unavailable with that reason.
func (n *Native) newBrowserd(ctx context.Context) child {
	exe, _ := os.Executable()
	binary := browserdBinary(exe, os.Getenv, exists)
	run := browserdRunDir(n.paths)
	if binary == "" {
		n.log("%s", noBrowserdReason)
		if err := markPtyd(run, noBrowserdReason); err != nil {
			n.log("the browser daemon's directory: %v", err)
		}
		return nil
	}
	if err := markPtyd(run, ""); err != nil {
		n.log("the browser daemon's directory: %v", err)
	}
	base := environWithout("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_PYTHON", "PYTHONPATH")
	scope := systemScope(ctx, exec.LookPath, runProbe)
	if scope == nil && runtime.GOOS == "linux" {
		n.log("no systemd user manager to cap the browser's memory in; it runs uncapped")
	}
	// The working directory is the local state folder, not the data folder: a working directory
	// there pins it, and a daemon that outlived a crashed launcher then held every later switch or
	// restore of the data off.
	return &Process{
		Name:    "browser daemon",
		Argv:    browserdArgv(binary, n.paths, scope),
		Dir:     n.paths.Local,
		Env:     browserdEnv(n.paths, base, isSetuidRoot),
		LogPath: filepath.Join(n.paths.RuntimeLogs, "browserd.log"),
		Log:     n.log,
		PidFile: filepath.Join(pidsDir(n.paths), "browserd.json"),
	}
}
