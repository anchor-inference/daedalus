package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

// The desktop application is two programs. The shell (desktop/shell, Electron) owns every window
// the operator sees: the launcher's own pages and the app are shown in it, never in a browser, and
// it is what the installers put in the Start menu, the Dock and the application menu. This launcher
// is the engine under it — the setup, the runtime, the supervisor, the update manager — started by
// the shell with --shell and kept as its child for as long as the shell runs.
//
// What the launcher used to do with a window or a browser of its own it asks the shell to do, one
// JSON object per line on its standard output; what the shell asks of it arrives the same way on
// its standard input. Everything else the launcher prints goes to launcher.log in the local state
// folder (logs/): the shell's end of standard output is this protocol and nothing else, and there is
// no terminal anywhere to read the rest in.
//
// Events, launcher to shell:
//
//	{"event":"show","url":U}                        put U in the window: the launcher's page first, the app once it answers
//	{"event":"focus","url":U}                       bring the window to the front, at U when there is one
//	{"event":"notify","title":T,"body":B,"url":U}   a desktop notification; a click shows U
//	{"event":"upgrade","data":D}                    the operator asked for the newer release: close this launcher, then run `upgrade --yes --data D`
//	{"event":"fatal","message":M,"log":L}           the launcher stopped before it could show anything; M says why, L is its log
//
// Commands, shell to launcher:
//
//	{"command":"focus","link":L}   the app was opened again, with the daedalus:// link L when there was one
//	{"command":"quit"}             the window was closed: stop, as Ctrl+C would
//
// The end of standard input is a quit as well. A shell that crashed or was killed by an uninstaller
// must not leave a native agent running behind a window nobody can see.

// shell is the link to the shell when this process was started with --shell, and nil otherwise.
var shell *shellLink

// shellQuit ends the launcher's run when the shell says so, or goes away.
var shellQuit context.CancelFunc = func() {}

// shellLog is the launcher's log under the shell, for the message that says where to look.
func shellLog() string {
	if shell == nil {
		return ""
	}
	return shell.log
}

// notificationTarget is the address a click on a notification shows: the one it names, or the
// conversation it is about.
func notificationTarget(app *App, n Notification) string {
	if n.Link != "" {
		return n.Link
	}
	if n.Session != "" && isIdentifier(n.Session) {
		return DeepLinkTarget(linkScheme+"://open/"+n.Session, AppURL(APIPort(app.paths), app.Lang()))
	}
	return ""
}

type shellLink struct {
	mu  sync.Mutex
	out io.Writer
	// log is launcher.log, once the process's output has been pointed at it.
	log string
}

type shellEvent struct {
	Event   string `json:"event"`
	URL     string `json:"url,omitempty"`
	Title   string `json:"title,omitempty"`
	Body    string `json:"body,omitempty"`
	Data    string `json:"data,omitempty"`
	Message string `json:"message,omitempty"`
	Log     string `json:"log,omitempty"`
}

type shellCommand struct {
	Command string `json:"command"`
	Link    string `json:"link"`
}

// emit writes one event. Lines are written whole under the lock, because the events come from
// every goroutine the launcher has and a line torn by another is a line the shell cannot read.
func (l *shellLink) emit(event shellEvent) {
	if l == nil {
		return
	}
	body, err := json.Marshal(event)
	if err != nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	_, _ = l.out.Write(append(body, '\n'))
}

// listen reads the shell's commands until its input ends, and quits then.
func (l *shellLink) listen(in io.Reader, focus func(link string), quit func()) {
	scanner := bufio.NewScanner(in)
	for scanner.Scan() {
		var command shellCommand
		if err := json.Unmarshal(scanner.Bytes(), &command); err != nil {
			continue
		}
		switch command.Command {
		case "focus":
			focus(command.Link)
		case "quit":
			quit()
			return
		}
	}
	quit()
}

// shellLogLimit is the size at which launcher.log is rolled over to launcher.log.1. One generation
// is kept: the log is for the start that went wrong, and that is nearly always the last one.
const shellLogLimit = 5 << 20

// openShellLog opens launcher.log in the local state folder for the process's own output. The
// folder is 0700 like the rest of the local state: the log names the data folder, the ports and
// what the setup did.
func openShellLog(p Paths) (*os.File, error) { return openLocalLog(p, "launcher.log") }

// openLocalLog opens one of the launcher's own logs in the local state folder for appending.
func openLocalLog(p Paths, name string) (*os.File, error) {
	if err := os.MkdirAll(p.Local, 0o700); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(p.RuntimeLogs, 0o700); err != nil {
		return nil, err
	}
	path := filepath.Join(p.RuntimeLogs, name)
	if info, err := os.Stat(path); err == nil && info.Size() > shellLogLimit {
		_ = os.Rename(path, path+".1")
	}
	return os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
}

// shellExecutable is the shell's own executable when this launcher was installed as part of the
// desktop application, which puts the two side by side: Daedalus.exe beside daedalus-desktop.exe,
// daedalus beside daedalus-desktop on Linux, and Contents/MacOS/Daedalus inside the bundle. Empty
// for a launcher on its own — a build from source, a server.
func shellExecutable(exe string) string {
	if exe == "" {
		return ""
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	name := "daedalus"
	switch runtime.GOOS {
	case "windows":
		name = "Daedalus.exe"
	case "darwin":
		name = "Daedalus"
	}
	candidate := filepath.Join(filepath.Dir(exe), name)
	if info, err := os.Stat(candidate); err == nil && info.Mode().IsRegular() {
		return candidate
	}
	return ""
}

// handOverToShell starts the desktop application instead of this launcher when the launcher was
// opened the way the application is — no command, no flags, a shell beside it — and reports
// whether it did. That is a shortcut or a habit from before there was a shell: double-clicking
// daedalus-desktop.exe, or an older installation's menu entry. What the operator meant is the app
// in its window, and the shell is what shows it; this process has nothing left to do.
func handOverToShell(opts options) bool {
	if opts.shell || opts.command != "" || opts.data != "" || opts.port != defaultPort || opts.setup || opts.mode != ModeUnset {
		return false
	}
	if os.Getenv("DAEDALUS_NO_SHELL") != "" {
		return false
	}
	exe, err := os.Executable()
	if err != nil {
		return false
	}
	target := shellExecutable(exe)
	if target == "" {
		return false
	}
	var args []string
	if opts.link != "" {
		args = append(args, opts.link)
	}
	cmd := exec.Command(target, args...)
	cmd.Dir = filepath.Dir(target)
	if err := cmd.Start(); err != nil {
		return false
	}
	_ = cmd.Process.Release()
	return true
}

// reopenApplication starts the application again after an upgrade it closed itself for: the new
// version when the upgrade committed, the one from before when it was rolled back — whichever is
// beside this executable now. That start says how the upgrade went (announceLastUpgrade).
func reopenApplication() {
	exe, err := os.Executable()
	if err != nil {
		return
	}
	target := shellExecutable(exe)
	if target == "" {
		return
	}
	cmd := exec.Command(target)
	cmd.Dir = filepath.Dir(target)
	if err := cmd.Start(); err == nil {
		_ = cmd.Process.Release()
	}
}

// announceLastUpgrade says once, on the first start after it, how the last upgrade ended: the
// application closed for it, and this start is the first thing the operator sees afterwards.
func announceLastUpgrade(app *App) {
	journal, err := readJournal(app.paths)
	if err != nil || journal.Kind != kindUpgrade {
		return
	}
	marker := filepath.Join(upgradeDir(app.paths), "announced")
	stamp := journal.Started.UTC().Format(time.RFC3339Nano)
	if body, err := os.ReadFile(marker); err == nil && strings.TrimSpace(string(body)) == stamp {
		return
	}
	var line string
	switch journal.Stage {
	case stageCommitted:
		line = fmt.Sprintf("updated from %s to %s", journal.From, journal.To)
	case stageRolledBack:
		line = fmt.Sprintf("the update to %s did not complete and was undone, data included: %s", journal.To, journal.Error)
	default:
		return
	}
	app.log("%s", line)
	shell.emit(shellEvent{Event: "notify", Title: "Daedalus", Body: line})
	_ = os.WriteFile(marker, []byte(stamp+"\n"), 0o600)
}
