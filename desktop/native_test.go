package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// The delay before another start doubles and then stops doubling: fast enough that a transient
// failure costs nothing, slow enough that a process which cannot start at all does not spin.
func TestTheRestartDelayDoublesAndIsCapped(t *testing.T) {
	want := []time.Duration{time.Second, time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second}
	for i, expected := range want {
		if got := backoffFor(i); got != expected {
			t.Errorf("backoffFor(%d) = %s, want %s", i, got, expected)
		}
	}
	if got := backoffFor(20); got != 60*time.Second {
		t.Fatalf("the delay is not capped: backoffFor(20) = %s", got)
	}
}

// The log is rolled before a start, never while it is being written: the launcher runs for weeks and
// a supervisor log that grows without a limit is a disk that fills without a warning.
func TestTheLogIsRolledOverOnceItIsLarge(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "supervisor.log")
	if err := os.WriteFile(path, []byte("small"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := rotateLog(path, 1024, 3); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path + ".1"); err == nil {
		t.Fatal("a small log was rolled over")
	}
	for round := 1; round <= 4; round++ {
		if err := os.WriteFile(path, []byte(strings.Repeat("x", 2048)), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := rotateLog(path, 1024, 3); err != nil {
			t.Fatal(err)
		}
		if _, err := os.Stat(path); err == nil {
			t.Fatal("the log was not moved out of the way")
		}
	}
	if _, err := os.Stat(path + ".3"); err != nil {
		t.Fatalf("the kept copies are missing: %v", err)
	}
	if _, err := os.Stat(path + ".4"); err == nil {
		t.Fatal("more copies are kept than were asked for")
	}
}

// A child that dies is started again, and a child that is stopped is not. Both with a shell in the
// place of the supervisor: what is being tested is the loop, not what it runs.
func TestAChildThatDiesIsStartedAgain(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the fake child is a POSIX shell")
	}
	dir := t.TempDir()
	counter := filepath.Join(dir, "starts")
	pr := &Process{
		Name:    "fake supervisor",
		Argv:    []string{"sh", "-c", fmt.Sprintf("echo . >> %q; exit 1", counter)},
		LogPath: filepath.Join(dir, "child.log"),
		Log:     func(string, ...any) {},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pr.Start(ctx)
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) && pr.Starts() < 2 {
		time.Sleep(50 * time.Millisecond)
	}
	if pr.Starts() < 2 {
		t.Fatalf("a child that exited was started %d time(s), not again", pr.Starts())
	}
	pr.Stop(ctx)
	after := pr.Starts()
	time.Sleep(2500 * time.Millisecond)
	if pr.Starts() != after {
		t.Fatalf("a stopped child was started again (%d → %d)", after, pr.Starts())
	}
	if _, err := os.Stat(pr.LogPath); err != nil {
		t.Fatalf("the child's output was not kept: %v", err)
	}
}

// Stopping ends the whole tree: the supervisor starts the bot in a session of its own, so
// signalling only the process the launcher knows about would leave the agent running behind it.
func TestStoppingEndsWhatTheChildStarted(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the fake child is a POSIX shell")
	}
	dir := t.TempDir()
	marker := filepath.Join(dir, "grandchild-alive")
	pr := &Process{
		Name: "fake supervisor",
		// A child that starts a child of its own and then waits, as the supervisor does.
		Argv:    []string{"sh", "-c", fmt.Sprintf("(while true; do touch %q; sleep 0.2; done) & wait", marker)},
		LogPath: filepath.Join(dir, "child.log"),
		Log:     func(string, ...any) {},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pr.Start(ctx)
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(marker); err == nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Skip("the fake child never got going on this machine")
	}
	pr.Stop(ctx)
	if err := os.Remove(marker); err != nil {
		t.Fatal(err)
	}
	time.Sleep(time.Second)
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("something the stopped child started is still running")
	}
}

// The supervisor reads its whole world from the environment, which is what lets one supervisor serve
// both modes. These are the names that have to be in it, and the values that have to be this
// installation's own rather than a container's.
func TestTheSupervisorIsGivenThisInstallationsPaths(t *testing.T) {
	data := t.TempDir()
	paths, err := NewPaths(data)
	if err != nil {
		t.Fatal(err)
	}
	env := envMap(supervisorEnv(paths, nil, map[string]string{"API_PORT": "19985", "SERVICES_PORT_RANGE": "19170-19179", "USD_PER_DAY": "20"}))
	for key, want := range map[string]string{
		"DAEDALUS_NATIVE":        "1",
		"DAEDALUS_BOT_REPO":      paths.Bot,
		"DAEDALUS_CORE_REPO":     paths.Core,
		"DAEDALUS_STATE":         paths.State,
		"DAEDALUS_WORKSPACES":    paths.Workspaces,
		"UV_PROJECT_ENVIRONMENT": paths.RuntimeVenv,
		"SERVICES_PUBLIC_HOST":   "127.0.0.1",
		"API_PORT":               "19985",
		"SERVICES_PORT_RANGE":    "19170-19179",
	} {
		if env[key] != want {
			t.Errorf("%s = %q, want %q", key, env[key], want)
		}
	}
	if !strings.Contains(env["DAEDALUS_BOT_CMD"], paths.RuntimeVenv) {
		t.Errorf("the bot is not started out of the runtime's environment: %q", env["DAEDALUS_BOT_CMD"])
	}
	if strings.Contains(strings.Join(supervisorEnv(paths, nil, nil), " "), "/srv/") {
		t.Error("a container path reached a native installation's environment")
	}
	// Windows, and a socket path too long for sun_path (a deep temporary folder does it), get a port.
	if supervisorOverTCP(paths, runtime.GOOS) {
		if !strings.HasPrefix(env["DAEDALUS_SUPERVISOR_TCP"], "127.0.0.1:") {
			t.Errorf("no loopback port where a socket cannot be: %q", env["DAEDALUS_SUPERVISOR_TCP"])
		}
		if env["DAEDALUS_SUPERVISOR_SOCKET"] != "" {
			t.Error("a unix socket path was given as well as a port")
		}
	} else {
		// In the local state folder: a stale socket left by a killed supervisor is a special file,
		// and one inside the data folder would refuse every fenced switch.
		if env["DAEDALUS_SUPERVISOR_SOCKET"] != paths.SupervisorSocket || !within(paths.SupervisorSocket, paths.Local) {
			t.Errorf("the socket is not in the local state folder: %q", env["DAEDALUS_SUPERVISOR_SOCKET"])
		}
		if env["DAEDALUS_SUPERVISOR_TCP"] != "" {
			t.Error("a loopback port was opened where a socket file will do")
		}
	}
}

// The port the launcher hands out is the one in the env file when that is a port, and the pinned
// default otherwise: a process told to listen on nonsense listens nowhere and says nothing.
func TestAPortIsOnlyTakenFromTheFileWhenItIsAPort(t *testing.T) {
	for _, bad := range []string{"", "  ", "0", "-1", "70000", "3201abc", "http://x"} {
		if got := parsePort(bad, "3201"); got != "3201" {
			t.Errorf("parsePort(%q) = %q, want the default", bad, got)
		}
	}
	if got := parsePort(" 19160 ", "3201"); got != "19160" {
		t.Errorf("parsePort of a real port = %q", got)
	}
}

// The provider keys reach the key proxy and nothing else. This is the one part of the container's
// isolation that survives without a container, so it is asserted rather than assumed.
func TestTheKeysGoToTheKeyProxyAndNotToTheAgent(t *testing.T) {
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	keys := map[string]string{"DEEPSEEK_API_KEY": "sk-secret", "OPENROUTER_API_KEY": "or-secret"}
	settings := map[string]string{"API_PORT": "19985", "KEYPROXY_PORT": "19160"}
	proxy := envMap(keyproxyEnv(paths, nil, keys, settings, "/home/someone"))
	if proxy["DEEPSEEK_API_KEY"] != "sk-secret" {
		t.Error("the key proxy was not given the key it exists to hold")
	}
	if proxy["KEYPROXY_PORT"] != "19160" {
		t.Errorf("the key proxy is on %q, not the port the file names", proxy["KEYPROXY_PORT"])
	}
	agent := envMap(supervisorEnv(paths, nil, settings))
	for name := range keys {
		if _, found := agent[name]; found {
			t.Errorf("%s reached the agent's environment", name)
		}
	}
	if agent["KEYPROXY_BASE_URL"] != "http://127.0.0.1:19160" {
		t.Errorf("the agent was pointed at %q", agent["KEYPROXY_BASE_URL"])
	}
}

// The runtime's own binaries come first on PATH: the pinned ripgrep is the one Search finds, not
// whatever happens to be installed on the machine.
func TestTheRuntimesBinariesComeFirstOnPath(t *testing.T) {
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	n := NewNative(paths, func(string, ...any) {})
	dirs := strings.Split(n.searchPath(), string(os.PathListSeparator))
	if dirs[0] != paths.RuntimeBin {
		t.Fatalf("PATH starts with %q, not the runtime's binaries", dirs[0])
	}
	if !strings.Contains(n.searchPath(), filepath.Dir(venvPython(paths))) {
		t.Fatal("the environment's own scripts are not on PATH")
	}
}

// A path with a space in it is a Windows installation more often than not, and the bot command is a
// command line rather than an argument list.
func TestASpaceInThePathIsQuotedInTheBotCommand(t *testing.T) {
	if got := quoteArgv(`C:\Program Files\Daedalus\python.exe`); !strings.HasPrefix(got, `"`) {
		t.Fatalf("a path with a space came out unquoted: %s", got)
	}
	if got := quoteArgv("/home/x/venv/bin/python"); strings.Contains(got, `"`) {
		t.Fatalf("a path with no space was quoted: %s", got)
	}
}

// The launcher runs commands of the installation's own — a pairing link, a restart request — and
// those read their settings under the names the agent uses, not the ones the supervisor is given.
// Both spellings have to be there or the command reads the defaults of a container that is not here.
func TestACommandRunForTheInstallationSeesBothSpellings(t *testing.T) {
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	env := envMap(botEnv(paths, supervisorEnv(paths, nil, map[string]string{"API_PORT": "19985"})))
	for key, want := range map[string]string{
		"STATE_DIR":      paths.State,
		"WORKSPACES_DIR": paths.Workspaces,
		"BOT_REPO_DIR":   paths.Bot,
		"CORE_REPO_DIR":  paths.Core,
	} {
		if env[key] != want {
			t.Errorf("%s = %q, want %q", key, env[key], want)
		}
	}
	if supervisorOverTCP(paths, runtime.GOOS) {
		if env["SUPERVISOR_TCP"] == "" {
			t.Error("the command has no supervisor to talk to")
		}
	} else if env["SUPERVISOR_SOCKET"] != paths.SupervisorSocket {
		t.Errorf("the command was pointed at %q", env["SUPERVISOR_SOCKET"])
	}
}

func envMap(env []string) map[string]string {
	out := map[string]string{}
	for _, kv := range env {
		key, value, _ := strings.Cut(kv, "=")
		out[key] = value
	}
	return out
}

// The one value a command is run for is on the first line that carries anything; what comes before
// it is the environment clearing its throat. (It once called itself, which no test had asked it to
// do and which took the launcher down with a stack overflow the moment a token was read.)
func TestTheFirstLineThatCarriesSomethingIsTheAnswer(t *testing.T) {
	cases := map[string]string{
		"":                                    "",
		"\n\n":                                "",
		"token-abc\n":                         "token-abc",
		"\n  warning: something\ntoken-abc\n": "warning: something",
		"  token-abc  ":                       "token-abc",
	}
	for out, want := range cases {
		if got := firstLine(out); got != want {
			t.Errorf("firstLine(%q) = %q, want %q", out, got, want)
		}
	}
}

// The agent is told where the host terminal daemon keeps its endpoint, and that is inside the local
// state folder, which the agent's policy seals: the token there opens a shell as the operator.
func TestTheAgentIsToldWhereTheHostTerminalsAre(t *testing.T) {
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	env := envMap(supervisorEnv(paths, nil, nil))
	if env["TERMINALS_HOST_DIR"] != ptydRunDir(paths) {
		t.Fatalf("TERMINALS_HOST_DIR = %q", env["TERMINALS_HOST_DIR"])
	}
	if !within(env["TERMINALS_HOST_DIR"], env["DAEDALUS_LOCAL"]) {
		t.Fatalf("the run directory %q is outside the sealed local state folder", env["TERMINALS_HOST_DIR"])
	}
	argv := ptydArgv("/opt/ptyd", paths)
	want := []string{"/opt/ptyd", "serve", "--env", "host", "--run-dir", ptydRunDir(paths), "--state-dir", ptydStateDir(paths), "--listen"}
	if strings.Join(argv[:len(want)], " ") != strings.Join(want, " ") {
		t.Fatalf("argv %q", argv)
	}
}

type recorded struct {
	name string
	log  *[]string
}

func (r recorded) Start(context.Context) { *r.log = append(*r.log, "start "+r.name) }
func (r recorded) Stop(context.Context)  { *r.log = append(*r.log, "stop "+r.name) }

// The agent detaches from its terminals and browsers before the daemons go, and the key proxy it
// needed until then goes last; all four are forgotten, so the next start reads the env file again.
func TestStoppingTheAgentComesBeforeItsTerminals(t *testing.T) {
	var log []string
	n := &Native{supervisor: recorded{"supervisor", &log}, browserd: recorded{"browserd", &log}, ptyd: recorded{"ptyd", &log}, keyproxy: recorded{"keyproxy", &log}}
	n.Stop(context.Background())
	if got := strings.Join(log, ", "); got != "stop supervisor, stop browserd, stop ptyd, stop keyproxy" {
		t.Fatalf("stopped in the order %s", got)
	}
	if n.supervisor != nil || n.ptyd != nil || n.keyproxy != nil || n.browserd != nil {
		t.Fatal("a stopped process was kept for the next start")
	}
	// A build without a daemon has none to stop.
	log = nil
	n = &Native{supervisor: recorded{"supervisor", &log}, keyproxy: recorded{"keyproxy", &log}}
	n.Stop(context.Background())
	if got := strings.Join(log, ", "); got != "stop supervisor, stop keyproxy" {
		t.Fatalf("without a daemon: %s", got)
	}
}

// DAEDALUS_PTYD wins; then the daemon beside the launcher, which in a macOS bundle is
// Contents/MacOS; then nothing.
func TestTheDaemonIsFoundWhereTheReleasePutsIt(t *testing.T) {
	name := "ptyd"
	if runtime.GOOS == "windows" {
		name = "ptyd.exe"
	}
	exe := filepath.Join("apps", "Daedalus.app", "Contents", "MacOS", "daedalus-desktop")
	beside := filepath.Join(filepath.Dir(exe), name)
	env := func(value string) func(string) string {
		return func(key string) string {
			if key == "DAEDALUS_PTYD" {
				return value
			}
			return ""
		}
	}
	has := func(paths ...string) func(string) bool {
		return func(p string) bool {
			for _, q := range paths {
				if p == q {
					return true
				}
			}
			return false
		}
	}
	if got := ptydBinary(exe, env("/built/ptyd"), has(beside)); got != "/built/ptyd" {
		t.Errorf("DAEDALUS_PTYD: %q", got)
	}
	if got := ptydBinary(exe, env(""), has(beside)); got != beside {
		t.Errorf("beside the launcher: %q", got)
	}
	if got := ptydBinary(exe, env(""), has()); got != "" {
		t.Errorf("none: %q", got)
	}
}

// A build without a daemon says so where the agent looks, and the note goes once a daemon starts.
func TestAMissingDaemonLeavesItsReason(t *testing.T) {
	run := filepath.Join(t.TempDir(), "run")
	if err := markPtyd(run, noPtydReason); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(filepath.Join(run, unavailableFile))
	if err != nil || strings.TrimSpace(string(body)) != noPtydReason {
		t.Fatalf("note %q, %v", body, err)
	}
	if err := markPtyd(run, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(run, unavailableFile)); err == nil {
		t.Fatal("the note outlived the daemon's start")
	}
}

// A host terminal is the operator's shell: their PATH first, the runtime's tools after it.
func TestAHostTerminalFindsTheOperatorsToolsFirst(t *testing.T) {
	sep := string(os.PathListSeparator)
	env := envMap(ptydEnv([]string{"HOME=/home/someone", "PATH=/usr/local/bin" + sep + "/usr/bin"}, []string{"/data/runtime/bin"}))
	if env["PATH"] != "/usr/local/bin"+sep+"/usr/bin"+sep+"/data/runtime/bin" {
		t.Fatalf("PATH = %q", env["PATH"])
	}
	if env["HOME"] != "/home/someone" {
		t.Fatal("the operator's environment was not passed on")
	}
}

// A socket path too long for a unix socket makes the daemon listen on loopback TCP instead of
// failing to start.
func TestADeepFolderMakesTheDaemonListenOnTCP(t *testing.T) {
	if runtime.GOOS == "windows" {
		if ptydListen(`C:\d`) != "tcp:127.0.0.1:0" {
			t.Fatal("Windows is not on TCP")
		}
		return
	}
	if got := ptydListen("/d/runtime/ptyd/run"); got != "unix" {
		t.Errorf("short: %q", got)
	}
	if got := ptydListen("/" + strings.Repeat("deep/", 20) + "runtime/ptyd/run"); got != "tcp:127.0.0.1:0" {
		t.Errorf("long: %q", got)
	}
}

// The status counts the daemon only when something answers at its endpoint.
func TestTheDaemonCountsOnlyWhenItAnswers(t *testing.T) {
	run := t.TempDir()
	if ptydAnswers(run) {
		t.Fatal("an empty directory answered")
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(run, "endpoint"), []byte("tcp:"+ln.Addr().String()+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if !ptydAnswers(run) {
		t.Fatal("a listening daemon did not count")
	}
	ln.Close()
	if ptydAnswers(run) {
		t.Fatal("an endpoint left by a stopped daemon counted")
	}
	if err := os.WriteFile(filepath.Join(run, "endpoint"), []byte("tcp:192.0.2.1:1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if ptydAnswers(run) {
		t.Fatal("an endpoint off the loopback interface counted")
	}
}

// A crashed daemon is started again with the same backoff as the supervisor.
func TestACrashedDaemonIsStartedAgain(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the fake daemon is a POSIX shell")
	}
	dir := t.TempDir()
	paths, err := NewPaths(dir)
	if err != nil {
		t.Fatal(err)
	}
	fake := filepath.Join(dir, "ptyd")
	if err := os.WriteFile(fake, []byte("#!/bin/sh\necho started >> \""+filepath.Join(dir, "starts")+"\"\nexit 3\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DAEDALUS_PTYD", fake)
	n := NewNative(paths, func(string, ...any) {})
	pr, ok := n.newPtyd().(*Process)
	if !ok {
		t.Fatal("no daemon process for a daemon that exists")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pr.Start(ctx)
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) && pr.Starts() < 2 {
		time.Sleep(50 * time.Millisecond)
	}
	pr.Stop(ctx)
	if pr.Starts() < 2 {
		t.Fatalf("a daemon that exited was started %d time(s)", pr.Starts())
	}
	if _, err := os.Stat(filepath.Join(paths.RuntimeLogs, "ptyd.log")); err != nil {
		t.Fatalf("the daemon's output was not kept: %v", err)
	}
}

// The launcher gives the daemon a moment before the supervisor, and no more than that.
func TestTheLauncherWaitsBrieflyForTheDaemon(t *testing.T) {
	run := t.TempDir()
	start := time.Now()
	if waitForPtyd(context.Background(), run, 200*time.Millisecond) {
		t.Fatal("an empty directory was waited into an answer")
	}
	if waited := time.Since(start); waited > 2*time.Second {
		t.Fatalf("waited %s past the limit", waited)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		time.Sleep(100 * time.Millisecond)
		_ = os.WriteFile(filepath.Join(run, "endpoint"), []byte("tcp:"+ln.Addr().String()), 0o600)
	}()
	if !waitForPtyd(context.Background(), run, 5*time.Second) {
		t.Fatal("a daemon that came up was not found")
	}
}
