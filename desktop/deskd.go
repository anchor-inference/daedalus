package main

// The desktop daemon in native mode: computer use, the agent seeing and driving the operator's own
// screen. The launcher starts it as a child of its own beside the terminal and browser daemons, for
// the same reason they are: the supervisor restarts the agent whenever a change is applied, and a
// lease the operator granted, or a stop they pressed, must not depend on the agent being up. Quitting
// the launcher stops it, and whatever it held down is released on the way out.
//
// It is started directly, never through launchd and never in a systemd scope. On macOS the system
// attributes Screen Recording and Accessibility to the responsible process — Daedalus.app, through
// the launcher it started — and a daemon started by launchd would be a program of its own with no
// grants at all. On Linux a scope would not take the grants away (there are none), but there is
// nothing to cap: the daemon starts no programs, its memory is a few screenshots, and a scope would
// only add a probe and a dependency on the operator's user manager to something that needs neither.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// noDeskdReason is what the run directory says when this build carries no desktop daemon.
const noDeskdReason = "this build carries no deskd; computer use is unavailable"

// waylandReason is what it says on a Wayland session, where the daemon would refuse to start: its
// X11 compatibility server shows only the programs still on X11, and the portals Wayland needs are
// not served yet. Saying so here keeps a daemon from being restarted for ever only to say the same.
const waylandReason = "this is a Wayland session; computer use supports X11 sessions only for now"

// shellAppID is the desktop application's bundle id on macOS: appId in shell/electron-builder.yml.
const shellAppID = "io.github.anchor-inference.daedalus"

// deskdRunDir holds the daemon's endpoint and the host's token, which the agent is told about;
// deskdStateDir the operator's token and the audit, which it is not. Both are inside the local state
// folder, which the agent's policy seals whole, so the host reaches the run directory through the
// path it is given and never reads the operator's token beside it.
func deskdRunDir(p Paths) string   { return filepath.Join(p.Local, "deskd", "run") }
func deskdStateDir(p Paths) string { return filepath.Join(p.Local, "deskd", "state") }

// deskdConfigPath is the configuration the launcher writes before each start: the protected apps.
func deskdConfigPath(p Paths) string { return filepath.Join(p.Local, "deskd", "config.json") }

// deskdOperatorToken is where the daemon writes the operator's token, its default. Only the launcher
// reads it: the token grants leases, and a lease is the agent's hands on the operator's desktop.
func deskdOperatorToken(p Paths) string { return filepath.Join(deskdStateDir(p), "operator.token") }

// deskdBinary is the daemon this launcher runs: DAEDALUS_DESKD when it is set (a daemon built by
// hand), else the deskd packaged beside the launcher's own executable — Contents/MacOS in the Mac
// bundle, as for the other daemons. "" when there is none.
func deskdBinary(exe string, getenv func(string) string, exists func(string) bool) string {
	if p := getenv("DAEDALUS_DESKD"); p != "" {
		return p
	}
	if exe == "" {
		return ""
	}
	name := "deskd"
	if runtime.GOOS == "windows" {
		name = "deskd.exe"
	}
	if p := filepath.Join(filepath.Dir(exe), name); exists(p) {
		return p
	}
	return ""
}

// deskdListen is where the daemon listens, by ptyd's rule: its socket, or loopback TCP on Windows
// and where the socket's path would be too long.
func deskdListen(runDir string) string {
	if runtime.GOOS == "windows" || len(filepath.Join(runDir, "deskd.sock")) > 100 {
		return "tcp:127.0.0.1:0"
	}
	return "unix"
}

// deskdLogPath is the daemon's own JSON-lines log. Its standard output and error, where only a
// failure to start or a panic ever lands, go to deskdOutputPath beside it.
func deskdLogPath(p Paths) string    { return filepath.Join(p.RuntimeLogs, "deskd.log") }
func deskdOutputPath(p Paths) string { return filepath.Join(p.RuntimeLogs, "deskd-output.log") }

// deskdArgv is the daemon's command line.
func deskdArgv(binary string, p Paths) []string {
	run := deskdRunDir(p)
	return []string{binary, "serve", "--run-dir", run, "--state-dir", deskdStateDir(p), "--config", deskdConfigPath(p),
		"--listen", deskdListen(run), "--log-file", deskdLogPath(p)}
}

// deskdMatcher names an app the daemon must never send input to nor let a lease scope to. The
// fields are deskd's own (deskd/internal/desk Matcher), and every field an entry sets must match.
type deskdMatcher struct {
	BundleID string `json:"bundle_id,omitempty"`
	ExePath  string `json:"exe_path,omitempty"`
	ExeName  string `json:"exe_name,omitempty"`
	WMClass  string `json:"wm_class,omitempty"`
	PID      int    `json:"pid,omitempty"`
}

// deskdConfig is the daemon's configuration. Only the protected apps are set: every limit keeps the
// daemon's default, which docs/architecture/computer.md states. deskd refuses a key it does not
// know, so nothing goes in here that its decoder does not have.
type deskdConfig struct {
	Protected []deskdMatcher `json:"protected"`
}

// deskdProtected is Daedalus itself, in every form the daemon can recognise: the entry that keeps the
// agent from pressing "allow" on its own approval card, or typing into its own composer.
//
//   - the application's window: its bundle id on macOS, its executable elsewhere (Daedalus.exe,
//     Daedalus inside the bundle, daedalus on Linux — executableName in electron-builder.yml), and on
//     Linux the window class Electron gives it. Electron sets WM_CLASS from the package's name and
//     product name, "daedalus" and "Daedalus"; deskd compares either half without case, so the one
//     entry covers both.
//   - the launcher, whose pages a build with its own window shows, by its executable and its pid;
//   - the shell that started this launcher, by its pid, when there is one: that covers the window
//     even if it was started from a copy renamed out of every rule above.
//
// What it cannot cover: the app opened in an ordinary browser tab. The daemon names apps by process,
// and the browser holding that tab is the operator's browser, which a lease may rightly reach; a tab
// on the app's own address is not something this version of the daemon can tell apart.
func deskdProtected(goos, launcherExe string, launcherPID, shellPID int) []deskdMatcher {
	var out []deskdMatcher
	switch goos {
	case "darwin":
		out = append(out, deskdMatcher{BundleID: shellAppID}, deskdMatcher{ExeName: "Daedalus"})
	case "windows":
		out = append(out, deskdMatcher{ExeName: "Daedalus"})
	default:
		out = append(out, deskdMatcher{ExeName: "daedalus"}, deskdMatcher{WMClass: "daedalus"})
	}
	if launcherExe != "" {
		base := filepath.Base(strings.ReplaceAll(launcherExe, `\`, "/"))
		out = append(out, deskdMatcher{ExeName: strings.TrimSuffix(base, ".exe")})
	}
	if launcherPID > 0 {
		out = append(out, deskdMatcher{PID: launcherPID})
	}
	if shellPID > 0 && shellPID != launcherPID {
		out = append(out, deskdMatcher{PID: shellPID})
	}
	return out
}

// shellPID is the pid of the desktop application's shell when this launcher runs under it: the shell
// starts the launcher itself, so it is the parent. 0 for a launcher on its own, whose parent is a
// terminal or a session manager that must not be protected in the operator's stead.
func shellPID() int {
	if shell == nil {
		return 0
	}
	return os.Getppid()
}

// writeDeskdConfig writes the configuration beside the target and renames it into place, so a daemon
// restarted in the middle reads the old file or the new one and never half of one — which it would
// refuse, and then keep refusing at every restart.
func writeDeskdConfig(path string, config deskdConfig) error {
	body, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return err
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".config-*.json")
	if err != nil {
		return err
	}
	name := tmp.Name()
	if _, err := tmp.Write(append(body, '\n')); err != nil {
		tmp.Close()
		os.Remove(name)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(name)
		return err
	}
	if err := os.Rename(name, path); err != nil {
		os.Remove(name)
		return err
	}
	return nil
}

// waylandSession reports a Wayland session by deskd's own test, so the launcher and the daemon never
// disagree about whether it would start.
func waylandSession(getenv func(string) string) bool {
	if runtime.GOOS != "linux" {
		return false
	}
	return getenv("XDG_SESSION_TYPE") == "wayland" || (getenv("WAYLAND_DISPLAY") != "" && getenv("XDG_SESSION_TYPE") != "x11")
}

// newDeskd is the desktop daemon's process, or nil when this build carries none or the session is one
// it cannot serve; then the run directory is left saying why, and the app shows computer use
// unavailable with that reason.
func (n *Native) newDeskd() child {
	exe, _ := os.Executable()
	binary := deskdBinary(exe, os.Getenv, exists)
	run := deskdRunDir(n.paths)
	reason := ""
	switch {
	case binary == "":
		reason = noDeskdReason
	case waylandSession(os.Getenv):
		reason = waylandReason
	}
	if reason != "" {
		n.log("%s", reason)
		if err := markPtyd(run, reason); err != nil {
			n.log("the desktop daemon's directory: %v", err)
		}
		return nil
	}
	if err := markPtyd(run, ""); err != nil {
		n.log("the desktop daemon's directory: %v", err)
	}
	config := deskdConfig{Protected: deskdProtected(runtime.GOOS, exe, os.Getpid(), shellPID())}
	if err := writeDeskdConfig(deskdConfigPath(n.paths), config); err != nil {
		// Without its configuration the daemon would run with nothing protected, Daedalus's own
		// window included: no daemon is the safe answer.
		reason = fmt.Sprintf("the desktop daemon's configuration could not be written: %v", err)
		n.log("%s", reason)
		if err := markPtyd(run, reason); err != nil {
			n.log("the desktop daemon's directory: %v", err)
		}
		return nil
	}
	// The daemon's own log is opened by the daemon and so is never rotated by a restart; it is rolled
	// over here, once for each start of the stack, as the output log is before each start of the child.
	if err := os.MkdirAll(n.paths.RuntimeLogs, 0o755); err == nil {
		if err := rotateLog(deskdLogPath(n.paths), logLimitBytes, logKeep); err != nil {
			n.log("the desktop daemon's log: %v", err)
		}
	}
	// The working directory is the local state folder, for the reason browserd's is: a working
	// directory in the data folder pins it against a switch or a restore. The environment is the
	// operator's, which is where DISPLAY and XAUTHORITY come from on Linux.
	return &Process{
		Name:    "desktop daemon",
		Argv:    deskdArgv(binary, n.paths),
		Dir:     n.paths.Local,
		Env:     environWithout("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_PYTHON", "PYTHONPATH"),
		LogPath: deskdOutputPath(n.paths),
		Log:     n.log,
		PidFile: filepath.Join(pidsDir(n.paths), "deskd.json"),
	}
}

// deskdReason is why there is no daemon to talk to, read from the run directory: the note the
// launcher leaves there when it starts none.
func deskdReason(runDir string) string {
	body, err := os.ReadFile(filepath.Join(runDir, unavailableFile))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(body))
}

// computerStatusLine is the desktop daemon's line in `status`: why there is none, or whether it
// answers. A `status` from a second terminal has no child of its own to ask, so it looks at the run
// directory as the agent would.
func computerStatusLine(runDir string) string {
	if reason := deskdReason(runDir); reason != "" {
		return "unavailable — " + reason
	}
	if ptydAnswers(runDir) {
		return "the desktop daemon is running"
	}
	return "the desktop daemon is not running"
}
