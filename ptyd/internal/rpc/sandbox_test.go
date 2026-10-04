//go:build unix

package rpc_test

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/sandbox"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// realSandbox is a prober for this machine's bubblewrap, or a skip naming why there is none: the
// tests below need namespaces, which a default container and a machine that restricts
// unprivileged user namespaces both refuse.
func realSandbox(t *testing.T) *sandbox.Prober {
	t.Helper()
	bwrap, err := exec.LookPath("bwrap")
	if err != nil {
		t.Skip("bwrap is not installed here; the sandbox is checked where it is (a container with SYS_ADMIN)")
	}
	p := sandbox.NewProber(bwrap)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if status := p.Status(ctx); status != sandbox.OK {
		t.Skipf("the sandbox cannot run here: %s", status)
	}
	return p
}

// outsideTmp is a fresh directory that is not under /tmp. A sandboxed program has a private /tmp,
// so a folder there would be missing inside rather than read-only, and the wall a test means to
// show would be a different one.
func outsideTmp(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	dir, err := os.MkdirTemp(wd, ".sandbox-test-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	dir, _ = filepath.EvalSymlinks(dir)
	return dir
}

func (f *fixture) type_(t *testing.T, id, text string) {
	t.Helper()
	f.call(t, "terminal.write", map[string]any{"id": id, "text": text + "\r", "wait": "none",
		"origin": map[string]any{"kind": "agent", "actor": "test"}}, nil)
}

func TestSandboxedShellWritesOnlyWhereItMay(t *testing.T) {
	box := realSandbox(t)
	f, _ := startSideWith(t, box)
	root := outsideTmp(t)
	project, other := filepath.Join(root, "project"), filepath.Join(root, "other")
	for _, d := range []string{project, other} {
		if err := os.Mkdir(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	link := filepath.Join(root, "link")
	if err := os.Symlink(other, link); err != nil {
		t.Fatal(err)
	}
	token := filepath.Join(f.daemon.Config.RunDir, "token")
	if _, err := os.Stat(token); err != nil {
		t.Fatalf("the token should exist outside the sandbox: %v", err)
	}

	var created struct {
		Cwd     string `json:"cwd"`
		Sandbox struct {
			Writable []string       `json:"writable"`
			Skipped  []sandbox.Skip `json:"skipped"`
		} `json:"sandbox"`
	}
	f.call(t, "terminal.create", map[string]any{"id": "sb", "argv": []string{"bash", "--norc", "--noprofile"},
		"cwd": project, "env": map[string]string{"PS1": "$ "},
		"sandbox": map[string]any{"writable": []string{project, link, filepath.Join(root, "missing")}}}, &created)
	if created.Cwd != project || len(created.Sandbox.Writable) != 1 || created.Sandbox.Writable[0] != project {
		t.Fatalf("create: %+v", created)
	}
	if len(created.Sandbox.Skipped) != 2 || created.Sandbox.Skipped[0].Reason != "a symbolic link" ||
		created.Sandbox.Skipped[1].Reason != "missing" {
		t.Fatalf("skipped: %+v", created.Sandbox.Skipped)
	}

	f.type_(t, "sb", `touch "`+project+`/inside"; echo W1=$?`)
	f.waitOutput(t, "sb", "W1=0")
	f.type_(t, "sb", `touch "`+other+`/outside" 2>/dev/null; echo W2=$?`)
	f.waitOutput(t, "sb", "W2=1")
	// The symlinked folder was not bound: writing through it is writing to the read-only other.
	f.type_(t, "sb", `touch "`+link+`/through" 2>/dev/null; echo W3=$?`)
	f.waitOutput(t, "sb", "W3=1")
	// Of the state directory only the shell-integration scripts are there, bound back.
	f.type_(t, "sb", `test -e "`+token+`"; echo T=$?; echo "S=$(ls -A "`+f.daemon.Config.StateDir+`" | tr '\n' ,)"`)
	f.waitOutput(t, "sb", "T=1")
	f.waitOutput(t, "sb", "S=shell,\n")
	f.type_(t, "sb", `echo "H=$HISTFILE"; echo "TTY=$(tty)"`)
	f.waitOutput(t, "sb", "H="+sandbox.HistoryFile)
	if out := f.waitOutput(t, "sb", "TTY=/"); !regexp.MustCompile(`TTY=/dev/pts/\d+`).MatchString(out) {
		t.Fatalf("tty inside the sandbox: %q", out)
	}
	if _, err := os.Stat(filepath.Join(project, "inside")); err != nil {
		t.Fatalf("the write inside did not reach the folder: %v", err)
	}
	if _, err := os.Stat(filepath.Join(other, "outside")); err == nil {
		t.Fatal("the write outside reached the folder")
	}

	var info term.Info
	f.call(t, "terminal.get", map[string]any{"id": "sb"}, &info)
	if !info.Sandbox || len(info.Argv) == 0 || filepath.Base(info.Argv[0]) != "bash" {
		t.Fatalf("info: sandbox %v argv %v", info.Sandbox, info.Argv)
	}
}

func TestSandboxedShellKeepsJobControl(t *testing.T) {
	box := realSandbox(t)
	f, _ := startSideWith(t, box)
	dir := outsideTmp(t)
	f.call(t, "events.subscribe", map[string]any{"after_seq": 0}, nil)
	f.call(t, "terminal.create", map[string]any{"id": "jc", "argv": []string{"bash", "--norc", "--noprofile"},
		"cwd": dir, "env": map[string]string{"PS1": "$ "}, "sandbox": map[string]any{"writable": []string{dir}}}, nil)
	f.type_(t, "jc", "echo ready")
	f.waitOutput(t, "jc", "\nready")
	busy := func() bool {
		var info term.Info
		f.call(t, "terminal.get", map[string]any{"id": "jc"}, &info)
		return info.Busy
	}
	// At the prompt the shell holds the foreground with its own group, which is not bubblewrap's.
	waitFor(t, "an idle sandboxed shell reads as idle", func() bool { return !busy() })

	f.type_(t, "jc", "sleep 100 &")
	f.waitOutput(t, "jc", "[1] ")
	f.type_(t, "jc", "fg")
	f.waitOutput(t, "jc", "sleep 100\n")
	waitFor(t, "a job in the foreground reads as busy", busy)
	f.call(t, "terminal.write", map[string]any{"id": "jc", "keys": []string{"C-c"}, "wait": "none",
		"origin": map[string]any{"kind": "agent", "actor": "test"}}, nil)
	if !eventuallyTrue(func() bool { return !busy() }) {
		var info term.Info
		f.call(t, "terminal.get", map[string]any{"id": "jc"}, &info)
		t.Fatalf("Ctrl+C did not give the shell the foreground again: %+v\n%s", info, f.waitOutput(t, "jc", ""))
	}
	f.type_(t, "jc", "echo J=$? $(jobs | wc -l)")
	f.waitOutput(t, "jc", "J=130 0")

	// The shell exits through bubblewrap with its own status.
	f.type_(t, "jc", "exit 7")
	ev, err := f.client.WaitEvent("terminal.exited", "jc", 10*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var data struct {
		ExitCode int `json:"exit_code"`
	}
	_ = json.Unmarshal(ev.Data, &data)
	if data.ExitCode != 7 {
		t.Fatalf("exit through the sandbox: %s", ev.Data)
	}
}

func TestSandboxedLaunchReachesItsOwnFilesAndHooks(t *testing.T) {
	box := realSandbox(t)
	f, _ := startSideWith(t, box)
	dir := outsideTmp(t)
	f.call(t, "events.subscribe", map[string]any{"after_seq": 0}, nil)
	var r registered
	f.call(t, "hooks.register_launch", map[string]any{"launch_id": "sbl", "files": map[string][]byte{"settings.json": []byte(`{"a":1}`)}}, &r)
	f.call(t, "terminal.create", map[string]any{"id": "sl", "argv": []string{"sh"}, "launch_id": "sbl", "cwd": dir,
		"env": map[string]string{"PS1": "$ "}, "sandbox": map[string]any{"writable": []string{dir}}}, nil)
	f.type_(t, "sl", `cat "$DAEDALUS_LAUNCH_DIR/settings.json"; echo`)
	f.waitOutput(t, "sl", `{"a":1}`)
	// The overlay is read-only; the dial directory is where the program puts its sockets.
	f.type_(t, "sl", `touch "$DAEDALUS_LAUNCH_DIR/x" 2>/dev/null; echo L=$?; touch "$DAEDALUS_DIAL_DIR/s"; echo D=$?`)
	f.waitOutput(t, "sl", "L=1")
	f.waitOutput(t, "sl", "D=0")
	f.type_(t, "sl", `test -e "`+filepath.Join(f.daemon.Config.StateDir, "agent-writes.jsonl")+`"; echo J=$?`)
	f.waitOutput(t, "sl", "J=1")
	f.type_(t, "sl", `echo '{"x":1}' | "$DAEDALUS_HOOK_CMD" Stop; echo rc=$?`)
	ev, err := f.client.WaitEvent("hook", "sl", 20*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var data struct {
		Name string          `json:"name"`
		Body json.RawMessage `json:"body"`
	}
	_ = json.Unmarshal(ev.Data, &data)
	if data.Name != "Stop" || string(data.Body) != `{"x":1}` {
		t.Fatalf("hook from the sandbox: %s", ev.Data)
	}
	f.waitOutput(t, "sl", "rc=0")
	if _, err := os.Stat(filepath.Join(r.DialDir, "s")); err != nil {
		t.Fatalf("the socket placeholder did not reach the dial directory: %v", err)
	}
}

func TestSandboxedShellKeepsItsIntegration(t *testing.T) {
	box := realSandbox(t)
	f, _ := startSideWith(t, box)
	dir := outsideTmp(t)
	home := shellHome(t, map[string]string{".bashrc": "HISTFILE=\n"})
	s := &shellSession{t: t, f: f, id: "si", home: home}
	var created struct {
		ShellIntegration string `json:"shell_integration"`
	}
	f.call(t, "terminal.create", map[string]any{"id": "si", "shell": map[string]any{"program": "bash"}, "cwd": dir,
		"env": map[string]string{"HOME": home}, "sandbox": map[string]any{"writable": []string{dir}}}, &created)
	if created.ShellIntegration != "bash" {
		t.Fatalf("shell_integration %q", created.ShellIntegration)
	}
	// The scripts are in the daemon's state directory, which the sandbox hides but for them: the
	// shell reads its init file, marks its commands, and cannot change the scripts.
	if r := s.run("true"); r.ExitCode == nil || *r.ExitCode != 0 {
		t.Fatalf("true: %+v\n%s", r, f.waitOutput(t, "si", ""))
	}
	r := s.run(`echo x >> "` + filepath.Join(f.daemon.ShellDir, "bash", "init.sh") + `"`)
	if r.ExitCode == nil || *r.ExitCode == 0 {
		t.Fatalf("the integration scripts were writable from the sandbox: %+v", r)
	}
	if got := s.commands(false); len(got) != 2 {
		t.Fatalf("records: %+v", got)
	}
}

func TestSandboxRefusals(t *testing.T) {
	// Without a prober the build offers no sandbox, and says so.
	f, _ := startSide(t)
	for _, requested := range []string{"credentials", "typo"} {
		we := f.callErr("terminal.create", map[string]any{"id": "scoped", "sandbox": map[string]any{
			"read_isolation": requested, "writable": []string{},
		}})
		code := wire.CodeInvalidParams
		if requested == "credentials" {
			code = wire.CodeUnsupported
		}
		if we == nil || we.Code != code {
			t.Fatalf("read isolation %q: %v", requested, we)
		}
	}
	var listed struct {
		Terminals []term.Info `json:"terminals"`
	}
	f.call(t, "terminal.list", nil, &listed)
	if len(listed.Terminals) != 0 {
		t.Fatalf("a refused scoped request spawned a terminal: %+v", listed.Terminals)
	}
	if we := f.callErr("terminal.create", map[string]any{"id": "a", "sandbox": map[string]any{"writable": []string{}}}); we == nil || we.Code != wire.CodeUnsupported {
		t.Fatalf("no prober: %v", we)
	}
	var info struct {
		Capabilities map[string]any `json:"capabilities"`
	}
	f.call(t, "daemon.info", nil, &info)
	if info.Capabilities["sandbox"] != "not available in this build" {
		t.Fatalf("capability: %v", info.Capabilities["sandbox"])
	}
	if info.Capabilities["credential_read_isolation"] != false {
		t.Fatalf("credential read isolation was advertised: %v", info.Capabilities)
	}

	// A machine without namespaces: refused with the probe's reason, which daemon.info reports too.
	noNamespaces := func(context.Context, []string) (string, bool) {
		return "bwrap: No permissions to creating new namespace", false
	}
	refuse := sandbox.NewTestProber("/usr/bin/bwrap", "linux", noNamespaces, time.Now)
	g, _ := startSideWith(t, refuse)
	we := g.callErr("terminal.create", map[string]any{"id": "a", "sandbox": map[string]any{"writable": []string{}}})
	if we == nil || we.Code != wire.CodeUnsupported || !strings.Contains(we.Message, "No permissions") {
		t.Fatalf("no namespaces: %v", we)
	}
	g.call(t, "daemon.info", nil, &info)
	if s, _ := info.Capabilities["sandbox"].(string); !strings.Contains(s, "No permissions") {
		t.Fatalf("capability: %v", info.Capabilities["sandbox"])
	}

	// A working directory inside the daemon's own directories would be an empty one.
	ok := sandbox.NewTestProber("/usr/bin/bwrap", "linux", func(context.Context, []string) (string, bool) { return "", true }, time.Now)
	h, _ := startSideWith(t, ok)
	if we := h.callErr("terminal.create", map[string]any{"id": "a", "cwd": h.daemon.Config.StateDir,
		"sandbox": map[string]any{"writable": []string{}}}); we == nil || we.Code != wire.CodeInvalidParams {
		t.Fatalf("hidden cwd: %v", we)
	}
	if we := h.callErr("terminal.create", map[string]any{"id": "a", "sandbox": map[string]any{"writable": []string{}, "extra": 1}}); we == nil || we.Code != wire.CodeInvalidParams {
		t.Fatalf("unknown sandbox field: %v", we)
	}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	if !eventuallyTrue(cond) {
		t.Fatalf("never: %s", what)
	}
}

func eventuallyTrue(cond func() bool) bool {
	deadline := time.Now().Add(10 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(50 * time.Millisecond)
	}
	return true
}
