package main

// Native mode: the launcher is the supervisor of the supervisor.
//
// In Docker mode the launcher hands the stack to compose and compose keeps it alive. Here there is
// nothing under the launcher but the operating system, so the launcher does the job compose did: it
// starts launcher/supervisor.py under the runtime's python, keeps its output in a rotated log,
// starts it again when it dies, and stops it when the launcher quits. The supervisor's own job is
// unchanged — it is still the thing that preflights a change, restarts the bot and rolls a bad
// revision back — and that is the whole point of running it here rather than rewriting it in Go.
//
// Applying a local change is therefore two hops and not one: the launcher asks the supervisor to
// restart over its socket, and the supervisor preflights the commit on a detached copy of itself
// before it re-execs the bot. Stopping and starting the process from here would skip the check.

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// defaultKeyproxyPort is where the key proxy answers on the loopback interface in native mode. The
// container publishes nothing, so this is the first port the installation actually occupies beyond
// the app's own; KEYPROXY_PORT in the env file moves it.
const defaultKeyproxyPort = "3201"

// defaultSupervisorPort is the loopback port the supervisor listens on where unix sockets are not
// available. Windows only: everywhere else the socket is a file in the state directory, which no
// other process on the machine can even see.
const defaultSupervisorPort = "8769"

// healthySeconds is how long a child has to stay up for its start to count as a good one. Under it,
// the restart delay doubles; over it, the delay goes back to where it started.
const healthySeconds = 60

// logLimitBytes is how large the supervisor's log grows before the launcher rolls it over, and
// logKeep is how many rolled files are kept. The supervisor writes its own copy into the state
// directory as well; this is the launcher's, and it is the one that holds a crash that happened
// before the state directory was writable.
const (
	logLimitBytes = 8 << 20
	logKeep       = 3
)

// backoffFor is the delay before the n-th consecutive failed start. Doubling from a second to a
// minute: fast enough that a transient failure costs nothing, slow enough that a process which
// cannot start at all does not spin.
func backoffFor(consecutive int) time.Duration {
	if consecutive <= 1 {
		return time.Second
	}
	delay := time.Second << (consecutive - 1)
	if delay > 60*time.Second {
		return 60 * time.Second
	}
	return delay
}

// rotateLog moves a log out of the way once it has grown past limit, keeping the last few. It is
// called before a process is started, not while it is writing, so nothing is ever moved out from
// under an open file descriptor.
func rotateLog(path string, limit int64, keep int) error {
	info, err := os.Stat(path)
	if err != nil || info.Size() < limit {
		return nil
	}
	_ = os.Remove(fmt.Sprintf("%s.%d", path, keep))
	for i := keep - 1; i >= 1; i-- {
		_ = os.Rename(fmt.Sprintf("%s.%d", path, i), fmt.Sprintf("%s.%d", path, i+1))
	}
	return os.Rename(path, path+".1")
}

// Process is one long-lived child the launcher keeps alive: the supervisor, and the key proxy
// beside it. Everything about it is decided before it starts, so the supervising goroutine has one
// job — start it, wait for it, start it again — and can be tested against a child that is a shell.
type Process struct {
	Name    string
	Argv    []string
	Dir     string
	Env     []string
	LogPath string
	Log     func(string, ...any)
	// PidFile is where the running child is recorded (orphans.go), or empty for none.
	PidFile string

	mu       sync.Mutex
	cmd      *exec.Cmd
	stopping bool
	starts   int
	failures int
	lastErr  string
	done     chan struct{}
}

// Start runs the child and keeps running it until Stop. It returns as soon as the first attempt has
// been made: a child that cannot start is a state the page shows, not a reason for the launcher to
// exit.
func (pr *Process) Start(ctx context.Context) {
	pr.mu.Lock()
	if pr.done != nil {
		pr.mu.Unlock()
		return
	}
	pr.done = make(chan struct{})
	pr.stopping = false
	done := pr.done
	pr.mu.Unlock()
	go pr.supervise(ctx, done)
}

func (pr *Process) supervise(ctx context.Context, done chan struct{}) {
	defer close(done)
	for {
		started := time.Now()
		err := pr.runOnce(ctx)
		if pr.stopped() || ctx.Err() != nil {
			return
		}
		if time.Since(started) > healthySeconds*time.Second {
			pr.mu.Lock()
			pr.failures = 0
			pr.mu.Unlock()
		}
		pr.mu.Lock()
		pr.failures++
		attempt := pr.failures
		if err != nil {
			pr.lastErr = err.Error()
		}
		pr.mu.Unlock()
		delay := backoffFor(attempt)
		pr.logf("%s exited (%v); starting it again in %s", pr.Name, err, delay)
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
		if pr.stopped() {
			return
		}
	}
}

// runOnce starts the child once and waits for it. The log file is opened here rather than kept
// open, so a rotation between two starts takes effect on the next one.
func (pr *Process) runOnce(ctx context.Context) error {
	if pr.LogPath != "" {
		if err := os.MkdirAll(filepath.Dir(pr.LogPath), 0o755); err != nil {
			return err
		}
		if err := rotateLog(pr.LogPath, logLimitBytes, logKeep); err != nil {
			return err
		}
	}
	cmd := exec.Command(pr.Argv[0], pr.Argv[1:]...)
	cmd.Dir = pr.Dir
	cmd.Env = pr.Env
	var logFile *os.File
	if pr.LogPath != "" {
		file, err := os.OpenFile(pr.LogPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
		if err != nil {
			return err
		}
		logFile = file
		cmd.Stdout, cmd.Stderr = file, file
	}
	setProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		if logFile != nil {
			logFile.Close()
		}
		return err
	}
	pr.mu.Lock()
	pr.cmd = cmd
	pr.starts++
	pr.mu.Unlock()
	pr.logf("%s started pid=%d", pr.Name, cmd.Process.Pid)
	writeChildRecord(pr.PidFile, ChildRecord{Name: pr.Name, PID: cmd.Process.Pid, Program: pr.Argv[0], Started: time.Now().UTC(), IsolatedConsole: runtime.GOOS == "windows"})
	err := cmd.Wait()
	removeChildRecord(pr.PidFile, cmd.Process.Pid)
	if logFile != nil {
		logFile.Close()
	}
	return err
}

// Stop ends the child and the loop that keeps it alive. The whole process group goes, because the
// supervisor starts the bot in a session of its own and the bot starts tools in theirs: signalling
// only the supervisor would leave the agent running with nothing above it.
func (pr *Process) Stop(ctx context.Context) {
	pr.mu.Lock()
	pr.stopping = true
	cmd, done := pr.cmd, pr.done
	pr.done = nil
	pr.mu.Unlock()
	if cmd == nil || cmd.Process == nil {
		return
	}
	terminateGroup(cmd)
	select {
	case <-waitFor(done):
	case <-time.After(40 * time.Second):
		// The bot is given 25 seconds to drain a run by the supervisor; past that the operator is
		// waiting on something that is not coming back.
		pr.logf("%s did not stop in time; killing it", pr.Name)
		killGroup(cmd)
		<-waitFor(done)
	case <-ctx.Done():
	}
}

func waitFor(done chan struct{}) <-chan struct{} {
	if done == nil {
		closed := make(chan struct{})
		close(closed)
		return closed
	}
	return done
}

func (pr *Process) stopped() bool {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	return pr.stopping
}

// Running reports whether the child is up right now.
func (pr *Process) Running() bool {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	return pr.cmd != nil && pr.cmd.Process != nil && (pr.cmd.ProcessState == nil || !pr.cmd.ProcessState.Exited())
}

// Starts is how many times the child has been started, which is how the page distinguishes a
// process that is up from one that is being restarted over and over.
func (pr *Process) Starts() int {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	return pr.starts
}

func (pr *Process) logf(format string, args ...any) {
	if pr.Log != nil {
		pr.Log(format, args...)
	}
}

// child is a process the launcher keeps alive. Native holds its three through this, so the order
// they are stopped in can be tested without starting anything.
type child interface {
	Start(ctx context.Context)
	Stop(ctx context.Context)
}

// Native is the whole of the native installation the launcher owns: the runtime it downloads, the
// supervisor it keeps alive, the key proxy beside it, and the terminal and browser daemons.
type Native struct {
	paths Paths
	log   func(string, ...any)

	supervisor child
	keyproxy   child
	ptyd       child
	browserd   child
	git        string

	// bootID is the id the running supervisor was given, which the app echoes (ready.go).
	bootID string
	// strictHealth is set for the start that ends an upgrade or an update: only an answer carrying
	// bootID counts (ready.go). An ordinary start also takes an older app's answer without one.
	strictHealth bool

	// What the progress page is told: which piece of a start this is, and how far through the one
	// download whose size is known in advance. Both are optional — the command line has no page to
	// draw and passes neither.
	stage      func(Stage)
	downloaded func(done, total int64)
}

func NewNative(p Paths, log func(string, ...any)) *Native {
	return &Native{paths: p, log: log}
}

// OnProgress is how the launcher's page follows a start. Without it nothing here changes: the
// reports are made through these two and both are checked before they are called.
func (n *Native) OnProgress(stage func(Stage), downloaded func(done, total int64)) {
	n.stage, n.downloaded = stage, downloaded
}

func (n *Native) enter(stage Stage) {
	if n.stage != nil {
		n.stage(stage)
	}
}

func (n *Native) progress(done, total int64) {
	if n.downloaded != nil {
		n.downloaded(done, total)
	}
}

// venvPython is the interpreter everything native runs through: the supervisor, the key proxy and
// every command the launcher asks the installation to run for it.
func (n *Native) venvPython() string { return venvPython(n.paths) }

func venvPython(p Paths) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(p.RuntimeVenv, "Scripts", "python.exe")
	}
	return filepath.Join(p.RuntimeVenv, "bin", "python")
}

func uvBinary(p Paths) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(p.RuntimeUV, "uv.exe")
	}
	return filepath.Join(p.RuntimeUV, "uv")
}

// Ensure brings the whole runtime into being, in the order the pieces depend on each other: uv
// first because it installs the interpreter, then the interpreter, then the two binaries the tools
// need, then the environment the app runs in. Every step is a no-op once it has been done, so this
// is also what a warm start runs, and on a warm start it costs one stat per tool.
func (n *Native) Ensure(ctx context.Context) error {
	if err := n.paths.EnsureNativeDirs(); err != nil {
		return err
	}
	n.enter(StageRuntime)
	uv, err := pick(uvDownloads, runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return fmt.Errorf("uv: %w", err)
	}
	rg, err := pick(ripgrepDownloads, runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return fmt.Errorf("ripgrep: %w", err)
	}
	// What is still to be fetched, before anything is fetched. The archives are pinned, so their
	// sizes are known here rather than guessed from a Content-Length that may not arrive — and a
	// warm start, which downloads nothing, reports a total of zero and gets no bar at all.
	var downloaded, total int64
	for _, d := range []download{uv, rg} {
		if !n.paths.installed(d) {
			total += d.size
		}
	}
	if git, err := pick(gitDownloads, runtime.GOOS, runtime.GOARCH); err == nil && !n.paths.installed(git) {
		total += git.size
	}
	n.progress(0, total)
	got, err := installTool(ctx, n.paths, uv, n.paths.RuntimeUV, n.log)
	if err != nil {
		return err
	}
	downloaded += got
	n.progress(downloaded, total)
	got, err = installTool(ctx, n.paths, rg, n.paths.RuntimeBin, n.log)
	if err != nil {
		return err
	}
	downloaded += got
	n.progress(downloaded, total)
	gitPath, got, err := EnsureGit(ctx, n.paths, n.log)
	if err != nil {
		return err
	}
	n.git, downloaded = gitPath, downloaded+got
	n.progress(downloaded, total)
	if err := n.ensurePython(ctx); err != nil {
		return err
	}
	n.enter(StageCheckouts)
	if err := EnsureRepos(ctx, n.paths, n.gitRunner(), n.log); err != nil {
		return err
	}
	n.enter(StageEnvironment)
	if err := n.syncVenv(ctx); err != nil {
		return err
	}
	if err := n.ensureApp(ctx); err != nil {
		return err
	}
	if downloaded > 0 {
		n.log("the runtime is installed: %.0f MB of archives downloaded into %s, plus the interpreter and the environment uv fetches", float64(downloaded)/1e6, n.paths.Runtime)
	}
	return nil
}

// ensurePython asks uv for a managed CPython inside the runtime folder. uv checks its own downloads
// against the hashes python-build-standalone publishes, so there is no second table here; what this
// controls is where it lands, which is the folder the installation owns and nothing else.
func (n *Native) ensurePython(ctx context.Context) error {
	if _, err := os.Stat(n.paths.stamp("python")); err == nil {
		return nil
	}
	n.log("installing python %s", pythonVersion)
	cmd := exec.CommandContext(ctx, uvBinary(n.paths), "python", "install", pythonVersion)
	cmd.Env = n.runtimeEnv()
	if out, err := runCmd(cmd); err != nil {
		return fmt.Errorf("python %s could not be installed: %s", pythonVersion, strings.TrimSpace(out))
	}
	return os.WriteFile(n.paths.stamp("python"), []byte(pythonVersion+"\n"), 0o644)
}

// syncVenv builds the environment the app runs in, from the checkout's own lock file. It is skipped
// when the environment already matches what the checkout declares — the same stamp the supervisor
// uses, so the two never sync over each other.
func (n *Native) syncVenv(ctx context.Context) error {
	if !exists(n.paths.Bot) {
		return errors.New("the checkout is missing; the runtime cannot be built from it")
	}
	if n.venvMatchesCheckout() {
		return nil
	}
	n.log("building the environment (this is the long part of a first run)")
	cmd := exec.CommandContext(ctx, uvBinary(n.paths), "sync", "--frozen", "--inexact")
	cmd.Dir = n.paths.Bot
	cmd.Env = n.runtimeEnv()
	if out, err := runCmd(cmd); err != nil {
		return fmt.Errorf("the environment could not be built: %s", strings.TrimSpace(out))
	}
	return n.stampVenv()
}

// dependencyStamp is the file the supervisor writes into the environment to record what it was
// built for. Writing the same file here means a first start does not sync a second time.
const dependencyStamp = ".daedalus-dependencies"

func (n *Native) venvMatchesCheckout() bool {
	want, err := dependencyDigest(n.paths.Bot)
	if err != nil {
		return false
	}
	body, err := os.ReadFile(filepath.Join(n.paths.RuntimeVenv, dependencyStamp))
	if err != nil {
		return false
	}
	return strings.TrimSpace(string(body)) == want
}

func (n *Native) stampVenv() error {
	want, err := dependencyDigest(n.paths.Bot)
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(n.paths.RuntimeVenv, dependencyStamp), []byte(want), 0o644)
}

// gitRunner is how the checkouts are made and committed in native mode.
func (n *Native) gitRunner() gitRunner {
	git := n.git
	if git == "" {
		git = "git"
	}
	return func(ctx context.Context, name string, args ...string) (string, error) {
		cmd := exec.CommandContext(ctx, git, append([]string{"-C", filepath.Join(n.paths.Data, name)}, args...)...)
		cmd.Env = n.runtimeEnv()
		return runCmd(cmd)
	}
}

// searchPath is the PATH every native process runs with: the runtime's own binaries first, so the
// pinned ripgrep and the pinned git are the ones the agent's tools find, then what the launcher
// inherited, so everything else on the machine still works.
func (n *Native) searchPath() string {
	dirs := []string{n.paths.RuntimeBin, n.paths.RuntimeUV}
	if runtime.GOOS == "windows" {
		dirs = append(dirs, filepath.Join(n.paths.RuntimeGit, "cmd"), filepath.Join(n.paths.RuntimeGit, "usr", "bin"), filepath.Join(n.paths.RuntimeGit, "mingw64", "bin"))
	}
	if exists(n.paths.RuntimeNode) {
		node := n.paths.RuntimeNode
		if runtime.GOOS != "windows" {
			node = filepath.Join(node, "bin")
		}
		dirs = append(dirs, node)
	}
	dirs = append(dirs, filepath.Dir(venvPython(n.paths)))
	if inherited := os.Getenv("PATH"); inherited != "" {
		dirs = append(dirs, strings.Split(inherited, string(os.PathListSeparator))...)
	}
	return strings.Join(dirs, string(os.PathListSeparator))
}

// runtimeEnv is what uv and git run with: the launcher's environment, the runtime's PATH, and the
// two variables that keep uv inside the installation's own folder rather than in the user's cache
// and home directory.
func (n *Native) runtimeEnv() []string {
	env := environWithout("PATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR")
	return append(env,
		"PATH="+n.searchPath(),
		"UV_PROJECT_ENVIRONMENT="+n.paths.RuntimeVenv,
		"UV_PYTHON_INSTALL_DIR="+n.paths.RuntimePython,
		"UV_CACHE_DIR="+filepath.Join(n.paths.Runtime, "cache"),
		"UV_PYTHON="+pythonVersion,
		"GIT_TERMINAL_PROMPT=0",
	)
}

// environWithout is the process environment with some names taken out, so what follows can set them
// without the inherited value winning or appearing twice.
func environWithout(names ...string) []string {
	drop := make(map[string]bool, len(names))
	for _, name := range names {
		drop[strings.ToUpper(name)] = true
	}
	out := make([]string, 0, len(os.Environ()))
	for _, kv := range os.Environ() {
		if key, _, _ := strings.Cut(kv, "="); !drop[strings.ToUpper(key)] {
			out = append(out, kv)
		}
	}
	return out
}

// supervisorEnv is the environment the supervisor gets: every path it owns, the command it starts
// the bot with, and the values the env file carries. It is the same set of names the compose file
// passes into the container — the supervisor reads its world from the environment either way, which
// is what lets one supervisor serve both modes.
func supervisorEnv(p Paths, base []string, settings map[string]string) []string {
	env := append([]string(nil), base...)
	add := func(key, value string) {
		if value != "" {
			env = append(env, key+"="+value)
		}
	}
	add("DAEDALUS_NATIVE", "1")
	// The agent computes what it must never write into, and where its shell is on Windows, from the
	// folder the installation really lives in rather than from a constant naming a container's.
	add("DAEDALUS_RUNTIME", p.Runtime)
	add("DAEDALUS_BOT_REPO", p.Bot)
	add("DAEDALUS_CORE_REPO", p.Core)
	add("DAEDALUS_STATE", p.State)
	add("DAEDALUS_WORKSPACES", p.Workspaces)
	// The provider keys are beside the checkouts rather than under the state directory, and the
	// launcher is the only thing that knows it: the agent needs the path to refuse a tool that reaches
	// for it, which it cannot do for a file it has never been told about.
	add("DAEDALUS_SECRETS", p.Secrets)
	if exe, err := os.Executable(); err == nil {
		add("DAEDALUS_LAUNCHER", exe)
	}
	// The Telegram bot token and the API hash live in this file. The agent needs the path in order
	// to refuse a tool that reaches for it, which it cannot do for a file it has never been told about.
	add("DAEDALUS_ENV_FILE", p.Env)
	add("DAEDALUS_SSH_SOURCE", p.SSH)
	// The host terminals: the daemon the launcher runs writes its endpoint and token here. Always
	// given, daemon or not — the directory then says why there is none.
	add("TERMINALS_HOST_DIR", ptydRunDir(p))
	// The browser daemon, the same way.
	add("BROWSER_HOST_DIR", browserdRunDir(p))
	// The Mini App as a release archive carries it, beside the launcher. It is not the checkout's
	// own miniapp/dist: that is the thing this would be a fallback for, and pointing one at the
	// other makes the fallback a no-op that looks like a copy.
	if bundle := bundledApp(); bundle != "" {
		add("DAEDALUS_BAKED_APP", bundle)
	}
	add("DAEDALUS_BOT_CMD", quoteArgv(venvPython(p))+" -m daedalus serve")
	add("DAEDALUS_PREFLIGHT_VENV", filepath.Join(p.Runtime, "preflight-venv"))
	add("UV_PROJECT_ENVIRONMENT", p.RuntimeVenv)
	// The app serves the operator's own machine, and the services a session starts are reached at
	// the same address: nothing here is published to a network.
	add("SERVICES_PUBLIC_HOST", "127.0.0.1")
	if runtime.GOOS == "windows" {
		add("DAEDALUS_SUPERVISOR_TCP", "127.0.0.1:"+parsePort(settings["DAEDALUS_SUPERVISOR_PORT"], defaultSupervisorPort))
	} else {
		add("DAEDALUS_SUPERVISOR_SOCKET", filepath.Join(p.State, "supervisor.sock"))
	}
	add("KEYPROXY_BASE_URL", "http://127.0.0.1:"+parsePort(settings["KEYPROXY_PORT"], defaultKeyproxyPort))
	if exists(p.RuntimeBrowsers) {
		// Only once the browser extra has been installed: unset, the browser skills say the tools
		// are not there, which is true and is better than a path to an empty folder.
		add("PLAYWRIGHT_BROWSERS_PATH", p.RuntimeBrowsers)
	}
	for _, key := range []string{"API_PORT", "SERVICES_PORT_RANGE", "USD_PER_DAY", "TELEGRAM_BOT_TOKEN", "OWNER_USER_ID", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "MINIAPP_PUBLIC_URL", "DAEDALUS_SELFDEV_MODE", "GITHUB_TOKEN", "GITHUB_DAEDALUS_TOKEN", "DAEDALUS_GITHUB_ORG"} {
		add(key, settings[key])
	}
	return env
}

// keyproxyEnv is what the key proxy runs with. The provider keys come from the file the launcher
// keeps at 0600 outside every checkout, and they are read here rather than put in the supervisor's
// environment: the agent's process never holds a key in native mode either, which is the one part
// of the container's isolation that survives without a container.
func keyproxyEnv(p Paths, base []string, keys map[string]string, settings map[string]string, home string) []string {
	env := append([]string(nil), base...)
	for key, value := range keys {
		if value != "" {
			env = append(env, key+"="+value)
		}
	}
	env = append(env,
		"KEYPROXY_PORT="+parsePort(settings["KEYPROXY_PORT"], defaultKeyproxyPort),
		"KEYPROXY_HOST=127.0.0.1",
		"KEYPROXY_BUDGET_FLAG="+filepath.Join(p.State, "BUDGET_EXCEEDED"),
		"KEYPROXY_BUDGET_DB="+filepath.Join(p.State, "daedalus.sqlite"),
		"KEYPROXY_AGENT_API=http://127.0.0.1:"+parsePort(settings["API_PORT"], "8765"),
		"PYTHONPATH="+filepath.Join(p.Bot, "deploy", "keyproxy"),
	)
	if home != "" {
		env = append(env,
			"KEYPROXY_CODEX_AUTH="+filepath.Join(home, ".codex", "auth.json"),
			"KEYPROXY_GROK_AUTH="+filepath.Join(home, ".grok", "auth.json"),
			"KEYPROXY_CLAUDE_AUTH="+filepath.Join(home, ".claude", ".credentials.json"),
		)
	}
	return env
}

// quoteArgv wraps a path in quotes when it holds a space, because the supervisor's bot command is a
// command line and a Windows installation lives under a folder with a space in it more often than not.
func quoteArgv(path string) string {
	if strings.ContainsAny(path, " \t") {
		return `"` + path + `"`
	}
	return path
}

// Start brings the native installation up: the runtime, the key proxy, the supervisor, and then the
// wait for the app to answer. Calling it twice is calling it once — the processes are already there.
func (n *Native) Start(ctx context.Context) error {
	port, portWasFree, err := n.clearTheWay(ctx)
	if err != nil {
		return err
	}
	if err := n.Ensure(ctx); err != nil {
		return err
	}
	settings := readEnv(readFile(n.paths.Env))
	home, _ := os.UserHomeDir()
	base := environWithout("PATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "PYTHONPATH")
	base = append(base, "PATH="+n.searchPath(), "UV_PYTHON_INSTALL_DIR="+n.paths.RuntimePython, "UV_CACHE_DIR="+filepath.Join(n.paths.Runtime, "cache"), "UV_PYTHON="+pythonVersion)
	if n.keyproxy == nil {
		n.keyproxy = &Process{
			Name:    "key proxy",
			Argv:    []string{n.venvPython(), filepath.Join(n.paths.Bot, "deploy", "keyproxy", "proxy.py")},
			Dir:     n.paths.Bot,
			Env:     keyproxyEnv(n.paths, base, readEnv(readFile(n.paths.KeyproxyEnv)), settings, home),
			LogPath: filepath.Join(n.paths.RuntimeLogs, "keyproxy.log"),
			Log:     n.log,
			PidFile: filepath.Join(pidsDir(n.paths), "keyproxy.json"),
		}
	}
	if n.supervisor == nil {
		n.bootID = newBootID()
		n.supervisor = &Process{
			Name:    "supervisor",
			Argv:    []string{n.venvPython(), filepath.Join(n.paths.Bot, "launcher", "supervisor.py")},
			Dir:     n.paths.Bot,
			Env:     append(supervisorEnv(n.paths, base, settings), "DAEDALUS_BOOT_ID="+n.bootID),
			LogPath: filepath.Join(n.paths.RuntimeLogs, "supervisor.log"),
			Log:     n.log,
			PidFile: filepath.Join(pidsDir(n.paths), "supervisor.json"),
		}
	}
	if n.ptyd == nil {
		n.ptyd = n.newPtyd()
	}
	// The terminal daemon first, and a few seconds to come up, so the agent finds it on its first
	// look rather than on its next retry. Start only launches the process; without the wait the
	// supervisor was the first of the two to run. A daemon that is slower than that is found later.
	if n.ptyd != nil {
		n.ptyd.Start(ctx)
		waitForPtyd(ctx, ptydRunDir(n.paths), 5*time.Second)
	}
	// The browser daemon after it, by the same reasoning. It starts no Chromium until the agent
	// opens a browser, so it is up in a moment.
	if n.browserd == nil {
		n.browserd = n.newBrowserd(ctx)
	}
	if n.browserd != nil {
		n.browserd.Start(ctx)
		waitForPtyd(ctx, browserdRunDir(n.paths), 5*time.Second)
	}
	n.keyproxy.Start(ctx)
	n.supervisor.Start(ctx)
	n.enter(StageStart)
	n.log("waiting for the app to answer")
	expectBoot(port, n.expectation(portWasFree))
	return WaitReadyNative(ctx, port, nativeReadyTimeout)
}

// expectation is what this start's health check accepts (ready.go): the answer carrying this
// supervisor's boot id — and, for an ordinary start only, an older app's answer without one.
func (n *Native) expectation(portWasFree bool) bootExpectation {
	supervisor := n.supervisor
	alive := func() bool {
		running, ok := supervisor.(interface{ Running() bool })
		return ok && running.Running()
	}
	return bootExpectation{id: n.bootID, legacy: !n.strictHealth, portWasFree: portWasFree, alive: alive}
}

// clearTheWay is the first step of a start: what a launcher that died left running is stopped, and
// then the app's port has to be free — or an answer on it after the start would not be this start's.
// A supervisor this launcher already runs is the one exception: Start is idempotent.
func (n *Native) clearTheWay(ctx context.Context) (string, bool, error) {
	if err := StopOrphans(ctx, n.paths, n.log); err != nil {
		return "", false, err
	}
	port := APIPort(n.paths)
	if n.supervisor != nil {
		return port, true, nil
	}
	if portAnswers(port) {
		return port, false, fmt.Errorf("something this launcher did not start already answers on the app's port %s; stop it (or change API_PORT) and start again", port)
	}
	return port, true, nil
}

// newPtyd is the terminal daemon's process, or nil when this build carries none; then the run
// directory is left saying so, and the app shows host terminals unavailable with that reason.
func (n *Native) newPtyd() child {
	exe, _ := os.Executable()
	binary := ptydBinary(exe, os.Getenv, exists)
	run := ptydRunDir(n.paths)
	if binary == "" {
		n.log("%s", noPtydReason)
		if err := markPtyd(run, noPtydReason); err != nil {
			n.log("the terminal daemon's directory: %v", err)
		}
		return nil
	}
	if err := markPtyd(run, ""); err != nil {
		n.log("the terminal daemon's directory: %v", err)
	}
	base := environWithout("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_PYTHON", "PYTHONPATH")
	return &Process{
		Name:    "terminal daemon",
		Argv:    ptydArgv(binary, n.paths),
		Dir:     n.paths.Data,
		Env:     ptydEnv(base, n.runtimeTools()),
		LogPath: filepath.Join(n.paths.RuntimeLogs, "ptyd.log"),
		Log:     n.log,
		PidFile: filepath.Join(pidsDir(n.paths), "ptyd.json"),
	}
}

// runtimeTools are the runtime's own directories of programs: ripgrep, and on Windows MinGit, and
// node once it is installed.
func (n *Native) runtimeTools() []string {
	dirs := []string{n.paths.RuntimeBin}
	if runtime.GOOS == "windows" {
		dirs = append(dirs, filepath.Join(n.paths.RuntimeGit, "cmd"))
	}
	if exists(n.paths.RuntimeNode) {
		node := n.paths.RuntimeNode
		if runtime.GOOS != "windows" {
			node = filepath.Join(node, "bin")
		}
		dirs = append(dirs, node)
	}
	return dirs
}

// bundledApp is the prebuilt Mini App shipped next to the launcher, or an empty string when this
// build was not packaged with one — a `go build` in the source tree, most often.
func bundledApp() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	candidates := []string{filepath.Join(filepath.Dir(exe), "miniapp-dist")}
	if app, ok := bundleRoot(exe); ok {
		candidates = append(candidates, filepath.Join(app, "Contents", "Resources", "miniapp-dist"))
	}
	for _, dir := range candidates {
		if exists(filepath.Join(dir, "index.html")) {
			return dir
		}
	}
	return ""
}

// ensureApp makes sure something will serve /app. The checkout carries no built bundle — it is not
// in git — so it is either the one packaged with the launcher, or one built by node. Where there is
// neither, node is fetched: an installation that reaches this point has been promised a working app
// by one binary, and 58 MB is the honest price of keeping that promise.
func (n *Native) ensureApp(ctx context.Context) error {
	if exists(filepath.Join(n.paths.Bot, "miniapp", "dist", "index.html")) || bundledApp() != "" {
		return nil
	}
	if _, err := exec.LookPath("npm"); err == nil {
		return nil
	}
	if exists(n.paths.RuntimeNode) {
		return nil
	}
	n.log("this build carries no prebuilt app and there is no node to build one; fetching node")
	return n.InstallExtra(ctx, "node")
}

// nativeReadyTimeout is shorter than the Docker one: there is no image to pull and no virtual
// machine to wake, and the environment was already built before the supervisor was started.
const nativeReadyTimeout = 90 * time.Second

// Stop ends the three processes. Native mode does not leave the agent running behind a closed launcher,
// and that is deliberate rather than a setting: a container is visible in `docker ps` and has a
// restart policy of its own, while a supervisor started from here is an ordinary process with
// nothing above it and no window to say it is there. An agent the operator cannot see is one they
// cannot stop. A run in flight is not lost — the supervisor gives the bot 25 seconds to drain, the
// run is snapshotted, and it resumes on the next start.
//
// The supervisor goes first, so the agent detaches from its terminals and browsers cleanly and
// records them as ended rather than finding a daemon gone under it; then the browser daemon, which
// closes every browser; then the terminal daemon, which ends every host terminal; then the key
// proxy, which the agent needed until it stopped.
func (n *Native) Stop(ctx context.Context) {
	for _, c := range []child{n.supervisor, n.browserd, n.ptyd, n.keyproxy} {
		if c != nil {
			c.Stop(ctx)
		}
	}
	// All three are forgotten rather than kept for the next Start. A Process reads its environment
	// once, when it is built, while APIPort and WaitReadyNative read the env file every time: a stop,
	// an edit to the ports and a start would otherwise bring the old ports back up and then wait on
	// the new ones, which is a launcher hanging on a port nothing is serving.
	n.supervisor, n.keyproxy, n.ptyd, n.browserd = nil, nil, nil, nil
}

// Running counts what is answering rather than what this process started. A `status` from a second
// terminal has no children of its own and would otherwise report an installation that is plainly
// serving the app as nothing running at all — which is the question the operator was asking.
func (n *Native) Running() int {
	count := 0
	if n.SupervisorReachable() {
		count++
	}
	settings := readEnv(readFile(n.paths.Env))
	for _, port := range []string{parsePort(settings["API_PORT"], "8765"), parsePort(settings["KEYPROXY_PORT"], defaultKeyproxyPort)} {
		if portAnswers(port) {
			count++
		}
	}
	for _, run := range []string{ptydRunDir(n.paths), browserdRunDir(n.paths)} {
		if ptydAnswers(run) {
			count++
		}
	}
	return count
}

// portAnswers reports whether something is listening on a loopback port. It is a connection and not
// a request: what is on the other end says what it is in its own log, and the launcher only needs to
// know that it is there.
func portAnswers(port string) bool {
	conn, err := net.DialTimeout("tcp", "127.0.0.1:"+port, time.Second)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// Apply asks the supervisor to take the change in the checkout — it preflights the commit on a
// detached copy of itself and re-execs the bot only if that passes. The launcher deliberately does
// not stop and start the process instead: that would put the change live with nothing having looked
// at it, which is the difference between applying a change and merely restarting.
func (n *Native) Apply(ctx context.Context, reason string) (string, error) {
	return n.RunPython(ctx, "-m", "daedalus", "self", "restart", "--reason", reason)
}

// RunPython runs a command of the installation's own — minting a pairing link, asking the supervisor
// for something — in the environment the bot itself runs in.
func (n *Native) RunPython(ctx context.Context, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, n.venvPython(), args...)
	cmd.Dir = n.paths.Bot
	base := append(environWithout("PATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"), "PATH="+n.searchPath())
	cmd.Env = botEnv(n.paths, supervisorEnv(n.paths, base, readEnv(readFile(n.paths.Env))))
	out, err := runCmd(cmd)
	return strings.TrimSpace(out), err
}

// botEnv adds the names the agent reads its own settings under. The supervisor translates its
// DAEDALUS_* variables into these before it starts the bot, so a command the launcher runs for the
// installation — minting a pairing link, asking for a restart — has to be given the same pair of
// spellings, or it reads the defaults of a container that is not there and finds no database and no
// supervisor to talk to.
func botEnv(p Paths, env []string) []string {
	out := append([]string(nil), env...)
	out = append(out,
		"STATE_DIR="+p.State,
		"WORKSPACES_DIR="+p.Workspaces,
		"BOT_REPO_DIR="+p.Bot,
		"CORE_REPO_DIR="+p.Core,
	)
	if runtime.GOOS == "windows" {
		out = append(out, "SUPERVISOR_TCP="+envValue(env, "DAEDALUS_SUPERVISOR_TCP"))
	} else {
		out = append(out, "SUPERVISOR_SOCKET="+envValue(env, "DAEDALUS_SUPERVISOR_SOCKET"))
	}
	return out
}

// envValue reads one name back out of an environment that has already been built.
func envValue(env []string, name string) string {
	for i := len(env) - 1; i >= 0; i-- {
		if key, value, _ := strings.Cut(env[i], "="); key == name {
			return value
		}
	}
	return ""
}

// SupervisorReachable reports whether the supervisor is listening, which is what the status page
// needs to know before it offers a button that talks to it.
func (n *Native) SupervisorReachable() bool {
	if runtime.GOOS == "windows" {
		port := parsePort(readEnv(readFile(n.paths.Env))["DAEDALUS_SUPERVISOR_PORT"], defaultSupervisorPort)
		conn, err := net.DialTimeout("tcp", "127.0.0.1:"+port, 2*time.Second)
		if err != nil {
			return false
		}
		conn.Close()
		return true
	}
	// Dialled, not stat'ed: a supervisor that was killed leaves the socket file behind, and a status
	// page that reads the file offers buttons that talk to nothing.
	conn, err := net.DialTimeout("unix", filepath.Join(n.paths.State, "supervisor.sock"), 2*time.Second)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// InstallExtra fetches one of the optional pieces: node, for the four skills that shell out to npx
// and for rebuilding the Mini App, the browsers the agent and the browser skills drive, or the engine that
// recognises speech on this machine. None of the three is part of a first run, because an
// installation that never uses them should never pay for them.
func (n *Native) InstallExtra(ctx context.Context, name string) error {
	switch name {
	case "node":
		d, err := pick(nodeDownloads, runtime.GOOS, runtime.GOARCH)
		if err != nil {
			return fmt.Errorf("node: %w", err)
		}
		_, err = installTool(ctx, n.paths, d, n.paths.RuntimeNode, n.log)
		return err
	case "browser":
		// Playwright checks and unpacks its own browsers, so there is no second table for them here;
		// what this decides is where they land, which is inside the installation's folder. Both of
		// its Chromium builds: the full one the browser daemon runs, and the headless shell the
		// browser skills drive, which Playwright's headless launch looks for and will not do without.
		n.log("installing the browsers (about 320 MB to download, 650 MB on disk)")
		cmd := exec.CommandContext(ctx, uvBinary(n.paths), "run", "--frozen", "--extra", "browser", "python", "-m", "playwright", "install", "chromium")
		cmd.Dir = n.paths.Bot
		cmd.Env = append(n.runtimeEnv(), "PLAYWRIGHT_BROWSERS_PATH="+n.paths.RuntimeBrowsers)
		if out, err := runCmd(cmd); err != nil {
			return fmt.Errorf("the browser could not be installed: %s", strings.TrimSpace(out))
		}
		return nil
	case "speech":
		return n.InstallSpeech(ctx)
	default:
		return fmt.Errorf("no such runtime extra: %s (node, browser, speech)", name)
	}
}

// NativePorts is what the installation occupies on the loopback interface, for the status page.
func NativePorts(p Paths) string {
	settings := readEnv(readFile(p.Env))
	ports := []string{"app " + APIPort(p), "key proxy " + parsePort(settings["KEYPROXY_PORT"], defaultKeyproxyPort)}
	if runtime.GOOS == "windows" {
		ports = append(ports, "supervisor "+parsePort(settings["DAEDALUS_SUPERVISOR_PORT"], defaultSupervisorPort))
	}
	if services := strings.TrimSpace(settings["SERVICES_PORT_RANGE"]); services != "" {
		ports = append(ports, "services "+services)
	}
	return strings.Join(ports, ", ")
}

// dependencyDigest is the same digest the supervisor writes into the environment: the two files
// that declare what must be installed, hashed together, in the same order and with a missing file
// counting as empty. Matching it is what lets a first start skip a sync the launcher already did.
func dependencyDigest(repo string) (string, error) {
	digest := sha256.New()
	for _, name := range []string{"uv.lock", "pyproject.toml"} {
		body, err := os.ReadFile(filepath.Join(repo, name))
		if err != nil && !os.IsNotExist(err) {
			return "", err
		}
		digest.Write(body)
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

// pidOf is used by the tests and by the status line: the pid of a running child, or zero.
func (pr *Process) pidOf() int {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	if pr.cmd == nil || pr.cmd.Process == nil {
		return 0
	}
	return pr.cmd.Process.Pid
}

// parsePort is a small guard on a port read out of an env file: anything that is not a port is not
// used, because a process told to listen on nonsense listens nowhere and says nothing.
func parsePort(value, fallback string) string {
	n, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil || n < 1 || n > 65535 {
		return fallback
	}
	return strings.TrimSpace(value)
}

// newBootID is a fresh random id for one start of the supervisor.
func newBootID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("t%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b)
}
