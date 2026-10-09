//go:build unix

package rpc_test

import (
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/fake"
	"github.com/ascorblack/daedalus/ptyd/internal/hooks"
	"github.com/ascorblack/daedalus/ptyd/internal/logx"
	"github.com/ascorblack/daedalus/ptyd/internal/rpc"
	"github.com/ascorblack/daedalus/ptyd/internal/sandbox"
	"github.com/ascorblack/daedalus/ptyd/internal/shellint"
	"github.com/ascorblack/daedalus/ptyd/internal/sidechan"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// TestMain lets the test binary stand in for `ptyd hook-post`: a launch's terminals are given this
// binary as their hook command, so the acceptance test runs the real command without a build.
func TestMain(m *testing.M) {
	if filepath.Base(os.Args[0]) == "hook-post" {
		os.Exit(hooks.HookPost(os.Args[1:], os.Getenv, os.Stdin, os.Stdout, os.Stderr))
	}
	os.Exit(m.Run())
}

// startSide is start with the side channels: a hook listener, a launch registry whose hook command
// is this test binary, a reader over a project root, and the dialer.
func startSide(t *testing.T) (*fixture, *rpc.Side) {
	t.Helper()
	return startSideWith(t, nil)
}

// startSideWith is startSide with a sandbox prober; nil offers no sandbox.
func startSideWith(t *testing.T, box *sandbox.Prober) (*fixture, *rpc.Side) {
	t.Helper()
	base, err := os.MkdirTemp("", "ptyd")
	if err != nil {
		t.Fatal(err)
	}
	base, _ = filepath.EvalSymlinks(base)
	f := &fixture{dir: base}
	cfg, err := config.Parse([]string{"--env", "container", "--run-dir", filepath.Join(base, "run"),
		"--state-dir", filepath.Join(base, "state"), "--home", filepath.Join(base, "home")})
	if err != nil {
		t.Fatal(err)
	}
	for _, d := range []string{cfg.StateDir, cfg.Home, filepath.Join(base, "project")} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	journal, err := logx.OpenRotating(filepath.Join(cfg.StateDir, "agent-writes.jsonl"), 1<<20, 2)
	if err != nil {
		t.Fatal(err)
	}
	evlog := events.NewLog(1000)
	registry := term.NewRegistry(term.Deps{
		Emulator: fake.Factory(nil), Events: events.NewDebouncer(evlog, events.Policies),
		Journal: logx.NewJournal(journal), Clock: term.RealClock{}, Log: log, KillGrace: 200 * time.Millisecond,
	}, 8)
	exe, _ := os.Executable()
	launches, err := hooks.NewRegistry(cfg.StateDir, exe, evlog, log)
	if err != nil {
		t.Fatal(err)
	}
	launches.Grace = 200 * time.Millisecond
	hsrv, hln, err := launches.Listen("127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	fsys, err := sidechan.NewFS(nil, nil, []string{cfg.RunDir, cfg.StateDir}, cfg.Home)
	if err != nil {
		t.Fatal(err)
	}
	side := &rpc.Side{Exec: sidechan.NewExec(nil, os.Environ(), cfg.Home, log), FS: fsys,
		Dialer: sidechan.NewDialer(launches), Launches: launches, Listen: hln.Addr().String(), StateDir: cfg.StateDir}
	ep, err := server.Prepare(cfg.RunDir, "unix", "ptyd")
	if err != nil {
		t.Fatal(err)
	}
	f.daemon = &rpc.Daemon{Config: cfg, Instance: "inst1", StartedAt: time.Now().UTC(), Registry: registry,
		Events: evlog, Log: log, EmulatorName: "fake@0", Environ: os.Environ(), Side: side, Sandbox: box}
	if f.daemon.ShellDir, err = shellint.Install(filepath.Join(cfg.StateDir, "shell")); err != nil {
		t.Fatal(err)
	}
	srv := server.New(ep.Token, log, f.daemon.Hello)
	f.daemon.Register(srv)
	go srv.Serve(ep.Listener)
	f.client, err = clienttest.Dial(cfg.RunDir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		f.client.Close()
		ep.Listener.Close()
		registry.Shutdown(100 * time.Millisecond)
		launches.Close()
		hsrv.Close()
		srv.Close()
		ep.Release()
		journal.Close()
		os.RemoveAll(base)
	})
	return f, side
}

type registered struct {
	LaunchID string            `json:"launch_id"`
	HookURL  string            `json:"hook_url"`
	Token    string            `json:"hook_token"`
	Dir      string            `json:"dir"`
	DialDir  string            `json:"dial_dir"`
	Env      map[string]string `json:"env"`
}

func TestDaemonInfoNamesTheHookListener(t *testing.T) {
	f, side := startSide(t)
	var info struct {
		Hooks struct {
			Listen string `json:"listen"`
		} `json:"hooks"`
		Side struct {
			ExecAllow []string `json:"exec_allow"`
			StateDir  string   `json:"state_dir"`
		} `json:"side_channels"`
	}
	f.call(t, "daemon.info", nil, &info)
	if info.Hooks.Listen != side.Listen || !strings.HasPrefix(info.Hooks.Listen, "127.0.0.1:") || info.Side.StateDir == "" {
		t.Fatalf("%+v", info)
	}
	if !strings.Contains(strings.Join(info.Side.ExecAllow, ","), "claude") {
		t.Fatalf("%v", info.Side.ExecAllow)
	}
}

func TestExecAndFilesOverTheSocket(t *testing.T) {
	f, _ := startSide(t)
	if we := f.callErr("exec.run", map[string]any{"argv": []string{"sh", "-c", "true"}}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("sh: %v", we)
	}
	if we := f.callErr("exec.run", map[string]any{"argv": []string{"git", "--version"},
		"resources": map[string]any{"scope": map[string]any{"attempt_id": "attempt_123"}}}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("strict side-channel process escaped its sandbox: %v", we)
	}
	if _, err := exec.LookPath("git"); err == nil {
		var res struct {
			ExitCode int    `json:"exit_code"`
			Stdout   string `json:"stdout"`
		}
		f.call(t, "exec.run", map[string]any{"argv": []string{"git", "--version"}, "timeout_ms": 20000}, &res)
		if res.ExitCode != 0 || !strings.HasPrefix(res.Stdout, "git version") {
			t.Fatalf("%+v", res)
		}
	}
	project := filepath.Join(f.dir, "project")
	if err := os.WriteFile(filepath.Join(project, "notes.txt"), []byte("abc"), 0o600); err != nil {
		t.Fatal(err)
	}
	// Before any root is set, nothing is readable.
	if we := f.callErr("fs.read", map[string]any{"path": filepath.Join(project, "notes.txt")}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("no roots: %v", we)
	}
	var roots struct {
		Accepted []string           `json:"accepted"`
		Refused  []sidechan.Refusal `json:"refused"`
	}
	f.call(t, "fs.set_roots", map[string]any{"roots": []string{project, "/"}}, &roots)
	if len(roots.Accepted) != 1 || len(roots.Refused) != 1 {
		t.Fatalf("%+v", roots)
	}
	var read struct {
		Data []byte `json:"data_b64"`
		EOF  bool   `json:"eof"`
	}
	f.call(t, "fs.read", map[string]any{"path": filepath.Join(project, "notes.txt"), "max": 4 << 20}, &read)
	if string(read.Data) != "abc" || !read.EOF {
		t.Fatalf("%+v", read)
	}
	var list struct {
		Entries []sidechan.Entry `json:"entries"`
	}
	f.call(t, "fs.list", map[string]any{"path": project}, &list)
	if len(list.Entries) != 1 || list.Entries[0].Name != "notes.txt" {
		t.Fatalf("%+v", list)
	}
	if we := f.callErr("fs.read", map[string]any{"path": filepath.Join(f.dir, "state", "agent-writes.jsonl")}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("the state directory: %v", we)
	}
	if we := f.callErr("fs.stat", map[string]any{"path": filepath.Join(project, "x"), "extra": 1}); we == nil || we.Code != wire.CodeInvalidParams {
		t.Fatalf("an unknown field: %v", we)
	}
}

func TestAFolderIsCheckedAndMadeOverTheSocket(t *testing.T) {
	f, _ := startSide(t)
	folder := filepath.Join(f.dir, "elsewhere", "site")
	if we := f.callErr("fs.stat", map[string]any{"path": folder}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("a plain stat outside the roots: %v", we)
	}
	var st struct {
		Exists   bool   `json:"exists"`
		Type     string `json:"type"`
		Writable *bool  `json:"writable"`
		Created  bool   `json:"created"`
	}
	f.call(t, "fs.stat", map[string]any{"path": folder, "as_root": true}, &st)
	if st.Exists {
		t.Fatalf("%+v", st)
	}
	f.call(t, "fs.mkdir", map[string]any{"path": folder}, &st)
	if !st.Exists || !st.Created || st.Type != "dir" || st.Writable == nil || !*st.Writable {
		t.Fatalf("%+v", st)
	}
	st.Created = false
	f.call(t, "fs.mkdir", map[string]any{"path": folder}, &st)
	if st.Created {
		t.Fatalf("made twice: %+v", st)
	}
	for _, p := range []string{filepath.Join(f.dir, "home"), filepath.Join(f.dir, "state", "x"), "/"} {
		if we := f.callErr("fs.mkdir", map[string]any{"path": p}); we == nil || we.Code != wire.CodeForbidden {
			t.Fatalf("%s: %v", p, we)
		}
	}
	if we := f.callErr("fs.mkdir", map[string]any{"path": "relative"}); we == nil || we.Code != wire.CodeInvalidParams {
		t.Fatalf("relative: %v", we)
	}
}

func TestFoldersAreBrowsedOverTheSocket(t *testing.T) {
	f, _ := startSide(t)
	home := filepath.Join(f.dir, "home")
	for _, d := range []string{filepath.Join(home, "repo", ".git"), filepath.Join(home, ".ssh")} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	// No root is set: browsing is not held to them, so a folder for a new project can be found.
	var b sidechan.Browse
	f.call(t, "fs.browse", map[string]any{"path": "~", "hidden": true}, &b)
	if b.Path != home || len(b.Entries) != 1 || b.Entries[0].Name != "repo" || !b.Entries[0].IsGit || len(b.Places) == 0 {
		t.Fatalf("%+v", b)
	}
	f.call(t, "fs.browse", map[string]any{"path": f.dir}, &b)
	for _, e := range b.Entries {
		if e.Name == "state" || e.Name == "run" {
			t.Fatalf("the daemon's own %s listed", e.Name)
		}
	}
	for _, p := range []string{filepath.Join(f.dir, "state"), filepath.Join(home, ".ssh")} {
		if we := f.callErr("fs.browse", map[string]any{"path": p}); we == nil || we.Code != wire.CodeForbidden {
			t.Fatalf("%s: %v", p, we)
		}
	}
	if we := f.callErr("fs.browse", map[string]any{"path": filepath.Join(f.dir, "missing")}); we == nil || we.Code != wire.CodeNotFound {
		t.Fatalf("missing: %v", we)
	}
	if we := f.callErr("fs.browse", map[string]any{"path": home, "extra": 1}); we == nil || we.Code != wire.CodeInvalidParams {
		t.Fatalf("an unknown field: %v", we)
	}
	if we := f.callErr("fs.browse", map[string]any{"path": "relative"}); we == nil || we.Code != wire.CodeInvalidParams {
		t.Fatalf("relative: %v", we)
	}
}

func TestAFileIsWrittenIntoAnInboxOverTheSocket(t *testing.T) {
	f, _ := startSide(t)
	project := filepath.Join(f.dir, "project")
	var roots map[string]any
	f.call(t, "fs.set_roots", map[string]any{"roots": []string{project}}, &roots)
	target := filepath.Join(project, ".agents", "inbox", "t1", "spec.md")
	var w struct {
		Size    int64 `json:"size"`
		Created bool  `json:"created"`
	}
	f.call(t, "fs.write", map[string]any{"path": target, "offset": 0, "data_b64": []byte("hello ")}, &w)
	if !w.Created || w.Size != 6 {
		t.Fatalf("%+v", w)
	}
	f.call(t, "fs.write", map[string]any{"path": target, "offset": 6, "data_b64": []byte("world")}, &w)
	if w.Created || w.Size != 11 {
		t.Fatalf("%+v", w)
	}
	if body, _ := os.ReadFile(target); string(body) != "hello world" {
		t.Fatalf("%q", body)
	}
	if we := f.callErr("fs.write", map[string]any{"path": target, "offset": 0, "data_b64": []byte("x")}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("overwrite: %v", we)
	}
	if we := f.callErr("fs.write", map[string]any{"path": filepath.Join(project, "README.md"), "offset": 0, "data_b64": []byte("x")}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("outside an inbox: %v", we)
	}
	if we := f.callErr("fs.write", map[string]any{"path": filepath.Join(f.dir, "home", ".agents", "inbox", "t", "a"), "offset": 0, "data_b64": []byte("x")}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("outside the roots: %v", we)
	}
}

// echoServer answers every connection by echoing what it reads, until the listener closes.
func echoServer(t *testing.T, ln net.Listener) {
	t.Helper()
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				io.Copy(c, c)
			}()
		}
	}()
}

// frameOn waits for the next frame of a channel.
func frameOn(t *testing.T, f *fixture, channel uint32) []byte {
	t.Helper()
	deadline := time.After(10 * time.Second)
	for {
		select {
		case fr := <-f.client.Frames:
			if fr.Channel == channel {
				return fr.Payload
			}
		case <-deadline:
			t.Fatalf("no frame on channel %d", channel)
		}
	}
}

func TestNetDialOnlyWhatALaunchRegistered(t *testing.T) {
	f, _ := startSide(t)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	echoServer(t, ln)
	port := ln.Addr().(*net.TCPAddr).Port
	target := "tcp:127.0.0.1:" + strconv.Itoa(port)

	if we := f.callErr("net.dial", map[string]any{"target": target, "launch_id": "nope"}); we == nil || we.Code != wire.CodeStaleLaunch {
		t.Fatalf("an unknown launch: %v", we)
	}
	var r registered
	f.call(t, "hooks.register_launch", map[string]any{"launch_id": "L1"}, &r)
	if we := f.callErr("net.dial", map[string]any{"target": target, "launch_id": "L1"}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("an unregistered port: %v", we)
	}
	for _, bad := range []string{"tcp:10.0.0.1:80", "tcp:localhost:" + strconv.Itoa(port), "unix:../../run/ptyd.sock", "unix:/etc/passwd", "http://x"} {
		if we := f.callErr("net.dial", map[string]any{"target": bad, "launch_id": "L1"}); we == nil {
			t.Fatalf("%s was dialled", bad)
		}
	}
	f.call(t, "net.allow", map[string]any{"launch_id": "L1", "port": port}, nil)
	var dial struct {
		Channel uint32 `json:"channel"`
	}
	f.call(t, "net.dial", map[string]any{"target": target, "launch_id": "L1"}, &dial)
	if err := f.client.Send(dial.Channel, []byte("ping")); err != nil {
		t.Fatal(err)
	}
	if got := frameOn(t, f, dial.Channel); string(got) != "ping" {
		t.Fatalf("%q", got)
	}
	// Ending the launch closes its streams.
	f.call(t, "hooks.unregister_launch", map[string]any{"launch_id": "L1"}, nil)
	if got := frameOn(t, f, dial.Channel); len(got) != 0 {
		t.Fatalf("expected the close, got %q", got)
	}
	if we := f.callErr("net.dial", map[string]any{"target": target, "launch_id": "L1"}); we == nil || we.Code != wire.CodeStaleLaunch {
		t.Fatalf("after unregister: %v", we)
	}
}

func TestNetDialUnixSocketInTheDialDirectory(t *testing.T) {
	f, _ := startSide(t)
	var r registered
	f.call(t, "hooks.register_launch", map[string]any{"launch_id": "L2", "ports": []int{}}, &r)
	if r.Env["DAEDALUS_DIAL_DIR"] != r.DialDir || r.DialDir == "" {
		t.Fatalf("%+v", r)
	}
	ln, err := net.Listen("unix", filepath.Join(r.DialDir, "bridge.sock"))
	if err != nil {
		t.Fatal(err)
	}
	echoServer(t, ln)
	var dial struct {
		Channel uint32 `json:"channel"`
	}
	f.call(t, "net.dial", map[string]any{"target": "unix:bridge.sock", "launch_id": "L2"}, &dial)
	msg := make([]byte, 100<<10)
	for i := range msg {
		msg[i] = byte(i)
	}
	if err := f.client.Send(dial.Channel, msg); err != nil {
		t.Fatal(err)
	}
	var got []byte
	for len(got) < len(msg) {
		got = append(got, frameOn(t, f, dial.Channel)...)
	}
	if string(got) != string(msg) {
		t.Fatal("the bytes came back different")
	}
	// A dial directory replaced by a link is not followed to another socket.
	outside := filepath.Join(f.dir, "elsewhere")
	os.Mkdir(outside, 0o700)
	ln2, err := net.Listen("unix", filepath.Join(outside, "bridge.sock"))
	if err != nil {
		t.Fatal(err)
	}
	echoServer(t, ln2)
	f.call(t, "hooks.register_launch", map[string]any{"launch_id": "L3"}, &r)
	os.Remove(r.DialDir)
	if err := os.Symlink(outside, r.DialDir); err != nil {
		t.Fatal(err)
	}
	if we := f.callErr("net.dial", map[string]any{"target": "unix:bridge.sock", "launch_id": "L3"}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("through a swapped dial directory: %v", we)
	}
	// The host closing the channel closes the socket: the echo ends, and the daemon answers the close.
	if err := f.client.Send(dial.Channel, nil); err != nil {
		t.Fatal(err)
	}
	if got := frameOn(t, f, dial.Channel); len(got) != 0 {
		t.Fatalf("expected the answering close, got %d bytes", len(got))
	}
}

// Codex from 0.157 on leaves a link at the socket path it is given, to a socket it binds in a private
// directory of its own. The link is followed there, and only there.
func TestNetDialFollowsALinkToAPrivateSocket(t *testing.T) {
	f, _ := startSide(t)
	var r registered
	f.call(t, "hooks.register_launch", map[string]any{"launch_id": "L4"}, &r)
	private, err := os.MkdirTemp("", "cxd")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(private) })
	os.Chmod(private, 0o700)
	// A link that names nothing yet is a socket not made yet: the host dials again.
	if err := os.Symlink(filepath.Join(private, "app.sock"), filepath.Join(r.DialDir, "codex.sock")); err != nil {
		t.Fatal(err)
	}
	if we := f.callErr("net.dial", map[string]any{"target": "unix:codex.sock", "launch_id": "L4"}); we == nil || we.Code != wire.CodeNotFound {
		t.Fatalf("a dangling link: %v", we)
	}
	ln, err := net.Listen("unix", filepath.Join(private, "app.sock"))
	if err != nil {
		t.Fatal(err)
	}
	echoServer(t, ln)
	var dial struct {
		Channel uint32 `json:"channel"`
	}
	f.call(t, "net.dial", map[string]any{"target": "unix:codex.sock", "launch_id": "L4"}, &dial)
	if err := f.client.Send(dial.Channel, []byte("ping")); err != nil {
		t.Fatal(err)
	}
	if got := frameOn(t, f, dial.Channel); string(got) != "ping" {
		t.Fatalf("%q", got)
	}

	// Not to a socket in a directory others may write: anyone could have put it there.
	open, err := os.MkdirTemp("", "cxo")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(open) })
	ln2, err := net.Listen("unix", filepath.Join(open, "app.sock"))
	if err != nil {
		t.Fatal(err)
	}
	echoServer(t, ln2)
	os.Chmod(open, 0o777)
	if err := os.Symlink(filepath.Join(open, "app.sock"), filepath.Join(r.DialDir, "open.sock")); err != nil {
		t.Fatal(err)
	}
	if we := f.callErr("net.dial", map[string]any{"target": "unix:open.sock", "launch_id": "L4"}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("a socket in a shared directory: %v", we)
	}

	// Not to another launch's socket, and not to a file that is no socket.
	var other registered
	f.call(t, "hooks.register_launch", map[string]any{"launch_id": "L5"}, &other)
	ln3, err := net.Listen("unix", filepath.Join(other.DialDir, "codex.sock"))
	if err != nil {
		t.Fatal(err)
	}
	echoServer(t, ln3)
	if err := os.Symlink(filepath.Join(other.DialDir, "codex.sock"), filepath.Join(r.DialDir, "theirs.sock")); err != nil {
		t.Fatal(err)
	}
	if we := f.callErr("net.dial", map[string]any{"target": "unix:theirs.sock", "launch_id": "L4"}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("another launch's socket: %v", we)
	}
	plain := filepath.Join(private, "plain")
	os.WriteFile(plain, []byte("x"), 0o600)
	if err := os.Symlink(plain, filepath.Join(r.DialDir, "plain.sock")); err != nil {
		t.Fatal(err)
	}
	if we := f.callErr("net.dial", map[string]any{"target": "unix:plain.sock", "launch_id": "L4"}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("a link to a plain file: %v", we)
	}
}

func TestCreateWithAnUnknownLaunchIsRefused(t *testing.T) {
	f, _ := startSide(t)
	we := f.callErr("terminal.create", map[string]any{"id": "x1", "argv": []string{"sh"}, "launch_id": "never"})
	if we == nil || we.Code != wire.CodeStaleLaunch {
		t.Fatalf("%v", we)
	}
}

// The acceptance: a real shell started with a launch posts a hook through the command its
// environment names, the event carries the terminal and the launch, and once the terminal has
// exited (and the grace passed) the same command is told the launch is over.
func TestHookFromARealTerminal(t *testing.T) {
	f, _ := startSide(t)
	f.call(t, "events.subscribe", map[string]any{"after_seq": 0}, nil)
	var r registered
	f.call(t, "hooks.register_launch", map[string]any{"launch_id": "run1", "files": map[string][]byte{"settings.json": []byte(`{"a":1}`)}}, &r)
	f.call(t, "terminal.create", map[string]any{"id": "sh1", "argv": []string{"sh"}, "launch_id": "run1",
		"env": map[string]string{"DAEDALUS_HOOK_TOKEN": "forged"}}, nil)
	origin := map[string]any{"kind": "agent", "actor": "test"}
	f.call(t, "terminal.write", map[string]any{"id": "sh1", "origin": origin, "wait": "none",
		"text": `echo '{"x":1}' | "$DAEDALUS_HOOK_CMD" Stop; echo "rc=$?"; cat "$DAEDALUS_LAUNCH_DIR/settings.json"; echo` + "\r"}, nil)
	ev, err := f.client.WaitEvent("hook", "sh1", 20*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var data struct {
		LaunchID   string          `json:"launch_id"`
		TerminalID string          `json:"terminal_id"`
		Name       string          `json:"name"`
		Body       json.RawMessage `json:"body"`
	}
	if err := json.Unmarshal(ev.Data, &data); err != nil {
		t.Fatal(err)
	}
	if data.LaunchID != "run1" || data.TerminalID != "sh1" || data.Name != "Stop" || string(data.Body) != `{"x":1}` {
		t.Fatalf("%+v %s", data, data.Body)
	}
	f.waitOutput(t, "sh1", "rc=0")
	f.waitOutput(t, "sh1", `{"a":1}`)

	f.call(t, "terminal.write", map[string]any{"id": "sh1", "origin": origin, "wait": "none", "text": "exit 0\r"}, nil)
	if _, err := f.client.WaitEvent("launch.ended", "sh1", 20*time.Second); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(r.Env["DAEDALUS_HOOK_CMD"], "Stop")
	cmd.Env = append(os.Environ(), "DAEDALUS_HOOK_URL="+r.HookURL, "DAEDALUS_HOOK_TOKEN="+r.Token)
	cmd.Stdin = strings.NewReader(`{"x":1}`)
	err = cmd.Run()
	if ee, ok := err.(*exec.ExitError); !ok || ee.ExitCode() != 2 {
		t.Fatalf("after the terminal exited: %v", err)
	}
	if _, err := os.Stat(r.Dir); !os.IsNotExist(err) {
		t.Fatalf("the launch directory outlived the launch: %v", err)
	}
}

func TestLaunchFilesNestedAndAddedLaterOverTheSocket(t *testing.T) {
	f, _ := startSide(t)
	var r registered
	f.call(t, "hooks.register_launch", map[string]any{"launch_id": "files1", "files": map[string][]byte{".claude/skills/team/SKILL.md": []byte("# team")}}, &r)
	if b, err := os.ReadFile(filepath.Join(r.Dir, ".claude", "skills", "team", "SKILL.md")); err != nil || string(b) != "# team" {
		t.Fatalf("%q %v", b, err)
	}
	var put struct {
		Path string `json:"path"`
	}
	f.call(t, "hooks.put_file", map[string]any{"launch_id": "files1", "name": "message-1.md", "data": []byte("long")}, &put)
	if b, err := os.ReadFile(put.Path); err != nil || string(b) != "long" || filepath.Dir(put.Path) != r.Dir {
		t.Fatalf("%q %q %v", put.Path, b, err)
	}
	if we := f.callErr("hooks.put_file", map[string]any{"launch_id": "files1", "name": "message-1.md", "data": []byte("again")}); we == nil || we.Code != wire.CodeInvalidParams {
		t.Fatalf("an existing file was overwritten unasked: %v", we)
	}
	f.call(t, "hooks.put_file", map[string]any{"launch_id": "files1", "name": "message-1.md", "data": []byte("again"), "replace": true}, &put)
	if b, err := os.ReadFile(put.Path); err != nil || string(b) != "again" {
		t.Fatalf("%q %v", b, err)
	}
	if we := f.callErr("hooks.put_file", map[string]any{"launch_id": "files1", "name": "../x", "data": []byte("x")}); we == nil || we.Code != wire.CodeInvalidParams {
		t.Fatalf("a path was written: %v", we)
	}
	if we := f.callErr("hooks.put_file", map[string]any{"launch_id": "nope", "name": "m.md", "data": []byte("x")}); we == nil || we.Code != wire.CodeStaleLaunch {
		t.Fatalf("a file for no launch: %v", we)
	}
}

func TestHookReplyOverTheSocket(t *testing.T) {
	f, _ := startSide(t)
	f.call(t, "events.subscribe", map[string]any{"after_seq": 0}, nil)
	var r registered
	f.call(t, "hooks.register_launch", map[string]any{"launch_id": "q1", "terminal_id": "t9", "hold_max_ms": 30000}, &r)
	type answer struct {
		code int
		body string
		ct   string
	}
	done := make(chan answer, 1)
	go func() {
		req, _ := newPost(r.HookURL+"/PermissionRequest?wait_ms=20000", r.Token, `{"tool_name":"Bash"}`)
		resp, err := httpClient.Do(req)
		if err != nil {
			done <- answer{}
			return
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		done <- answer{resp.StatusCode, string(b), resp.Header.Get("Content-Type")}
	}()
	ev, err := f.client.WaitEvent("hook", "t9", 10*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var data struct {
		ReplyID string `json:"reply_id"`
	}
	json.Unmarshal(ev.Data, &data)
	if we := f.callErr("hooks.reply", map[string]any{"launch_id": "other", "reply_id": data.ReplyID, "body": map[string]any{}}); we == nil || we.Code != wire.CodeNotFound {
		t.Fatalf("a reply across launches: %v", we)
	}
	f.call(t, "hooks.reply", map[string]any{"launch_id": "q1", "reply_id": data.ReplyID, "status": 200,
		"body": map[string]any{"hookSpecificOutput": map[string]any{"decision": map[string]any{"behavior": "allow"}}}}, nil)
	got := <-done
	if got.code != 200 || got.ct != "application/json" || !strings.Contains(got.body, `"behavior":"allow"`) {
		t.Fatalf("%+v", got)
	}
	if we := f.callErr("hooks.reply", map[string]any{"reply_id": data.ReplyID}); we == nil || we.Code != wire.CodeNotFound {
		t.Fatalf("a second reply: %v", we)
	}
}

func TestEventLogBoundedByBytes(t *testing.T) {
	l := events.NewLog(100)
	l.SetMaxBytes(1000)
	for i := 0; i < 10; i++ {
		l.PublishSized("hook", "", i, 300)
	}
	// Three of 300 fit in 1000; the rest fell off, and a subscriber from the start is told.
	if l.Oldest() != 8 || l.Last() != 10 {
		t.Fatalf("oldest %d last %d", l.Oldest(), l.Last())
	}
	if _, resync := l.Start(0); !resync {
		t.Fatal("no resync")
	}
	// One event bigger than the whole bound is still kept: the newest is never dropped.
	l.PublishSized("hook", "", "big", 5000)
	if l.Oldest() != 11 {
		t.Fatalf("oldest %d", l.Oldest())
	}
}

var httpClient = &http.Client{Timeout: time.Minute}

func newPost(url, token, body string) (*http.Request, error) {
	req, err := http.NewRequest(http.MethodPost, url, strings.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	return req, nil
}
