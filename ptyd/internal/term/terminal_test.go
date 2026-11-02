//go:build unix

package term

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/fake"
	"github.com/ascorblack/daedalus/ptyd/internal/logx"
	"github.com/ascorblack/daedalus/ptyd/internal/procenv"
	"github.com/ascorblack/daedalus/ptyd/proto/procstat/proctest"
)

type recorded struct {
	typ  string
	id   string
	data any
}

type recorder struct {
	mu  sync.Mutex
	evs []recorded
}

func (r *recorder) Publish(typ, id string, data any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.evs = append(r.evs, recorded{typ, id, data})
}

func (r *recorder) Flush(string) {}

func (r *recorder) types(id string) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []string
	for _, e := range r.evs {
		if e.id == id {
			out = append(out, e.typ)
		}
	}
	return out
}

func (r *recorder) find(id, typ string) (recorded, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, e := range r.evs {
		if e.id == id && e.typ == typ {
			return e, true
		}
	}
	return recorded{}, false
}

type harness struct {
	reg   *Registry
	rec   *recorder
	fakes chan *fake.Emulator
	dir   string
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	h := &harness{rec: &recorder{}, fakes: make(chan *fake.Emulator, 16), dir: t.TempDir()}
	journal, err := logx.OpenRotating(filepath.Join(h.dir, "journal.jsonl"), 1<<20, 2)
	if err != nil {
		t.Fatal(err)
	}
	h.reg = NewRegistry(Deps{
		Emulator:  fake.Factory(func(e *fake.Emulator) { h.fakes <- e }),
		Events:    h.rec,
		Journal:   logx.NewJournal(journal),
		Clock:     RealClock{},
		Log:       slog.New(slog.NewTextHandler(io.Discard, nil)),
		KillGrace: time.Second,
	}, 8)
	t.Cleanup(func() {
		h.reg.Shutdown(100 * time.Millisecond)
		journal.Close()
	})
	return h
}

func (h *harness) start(t *testing.T, id string, argv ...string) *Terminal {
	t.Helper()
	env := procenv.BuildEnv(os.Environ(), nil, map[string]string{"PS1": "$ ", "DAEDALUS_SI_NONCE": testNonce}, id)
	path, ok := procenv.LookPath(argv[0], env, "/")
	if !ok {
		t.Skipf("%s is not installed", argv[0])
	}
	term, err := h.reg.Create(Spec{ID: id, Path: path, Argv: argv, Cwd: h.dir, Env: env, Cols: 80, Rows: 24,
		RingBytes: 1 << 20, InputIdle: 0, Nonce: testNonce})
	if err != nil {
		t.Fatal(err)
	}
	return term
}

// testNonce is the shell-integration nonce of the terminals these tests start.
const testNonce = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"

func waitDone(t *testing.T, term *Terminal) {
	t.Helper()
	select {
	case <-term.Done():
	case <-time.After(10 * time.Second):
		t.Fatalf("terminal %s did not exit", term.ID)
	}
}

// waitOutput polls the ring until it contains want.
func waitOutput(t *testing.T, term *Terminal, want string) string {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		data, _, _, _ := term.ReadOutput(0, 1<<20, false)
		if strings.Contains(string(data), want) {
			return string(data)
		}
		if time.Now().After(deadline) {
			t.Fatalf("output never contained %q; it is %q", want, data)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestOutputReachesTheRingAndTheEmulator(t *testing.T) {
	h := newHarness(t)
	term := h.start(t, "out", "sh", "-c", `printf 'hello\n'; printf 'x\033[1000000000b'`)
	waitDone(t, term)
	data, from, to, gap := term.ReadOutput(0, 1<<20, false)
	if from != 0 || gap || to != int64(len(data)) {
		t.Fatalf("read %d..%d gap %v", from, to, gap)
	}
	if !strings.HasPrefix(string(data), "hello\r\n") {
		t.Fatalf("output %q", data)
	}
	// The clamp applies before anything else sees the bytes: ring and emulator alike.
	if !strings.Contains(string(data), "\x1b[10000b") || strings.Contains(string(data), "1000000000") {
		t.Fatalf("the parameter was not clamped: %q", data)
	}
	emu := <-h.fakes
	if string(emu.Fed()) != string(data) {
		t.Fatalf("the emulator saw %q, the ring holds %q", emu.Fed(), data)
	}
	if stripped, _, _, _ := term.ReadOutput(0, 1<<20, true); string(stripped) != "hello\nx" {
		t.Fatalf("stripped %q", stripped)
	}
}

func TestExitCodeAndSignal(t *testing.T) {
	h := newHarness(t)
	a := h.start(t, "code", "sh", "-c", "exit 3")
	b := h.start(t, "sig", "sh", "-c", "kill -TERM $$")
	waitDone(t, a)
	waitDone(t, b)
	if e, ok := a.Exit(); !ok || e.Code != 3 || e.Signal != "" {
		t.Fatalf("exit 3 reported as %+v", e)
	}
	if e, _ := b.Exit(); e.Code != -1 || e.Signal != "TERM" {
		t.Fatalf("SIGTERM reported as %+v", e)
	}
	ev, ok := h.rec.find("code", "terminal.exited")
	if !ok || ev.data.(map[string]any)["exit_code"] != 3 {
		t.Fatalf("exit event %+v", ev)
	}
	info := a.Info()
	if info.Status != "exited" || info.ExitCode == nil || *info.ExitCode != 3 || info.ExitedAt == nil {
		t.Fatalf("info after exit: %+v", info)
	}
}

func TestEventsArePublishedBeforeTheExit(t *testing.T) {
	h := newHarness(t)
	term := h.start(t, "ev", "sh", "-c", `printf '\033]0;my title\007\033]7;file://h/tmp\007\007\033]133;C;k=%s\007\033]133;D;4;k=%s\007' "$DAEDALUS_SI_NONCE" "$DAEDALUS_SI_NONCE"`)
	waitDone(t, term)
	types := h.rec.types("ev")
	want := []string{"terminal.created", "terminal.title", "terminal.cwd", "terminal.bell", "terminal.command", "terminal.exited"}
	if strings.Join(types, " ") != strings.Join(want, " ") {
		t.Fatalf("events %v, want %v", types, want)
	}
	info := term.Info()
	if info.Title != "my title" || info.Cwd != "/tmp" || info.LastCommand == nil || *info.LastCommand.ExitCode != 4 {
		t.Fatalf("info %+v", info)
	}
}

// alive reports whether pid is a live process (a zombie is not: nobody may have reaped it yet).
func alive(pid int) bool {
	if syscall.Kill(pid, 0) != nil {
		return false
	}
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return true // not Linux: trust kill(0)
	}
	s := string(data)
	i := strings.LastIndexByte(s, ')')
	return i < 0 || i+2 >= len(s) || s[i+2] != 'Z'
}

func readPid(t *testing.T, path string) int {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		data, err := os.ReadFile(path)
		if err == nil {
			if pid, err := strconv.Atoi(strings.TrimSpace(string(data))); err == nil && pid > 0 {
				return pid
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s never held a pid", path)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestKillEndsEscapedDescendants(t *testing.T) {
	proctest.Setsid(t)
	h := newHarness(t)
	// Three ways out of the terminal's process group: its own session (setsid), its own group
	// (setpgid through job control), and its own session with a parent that has already exited, so
	// that neither the tree nor the session leads to it any more.
	script := filepath.Join(h.dir, "escape.sh")
	body := `#!/bin/sh
d=$1
setsid sh -c 'echo $$ > '"$d"'/a; exec sleep 1000' &
sh -c 'set -m; sleep 1000 & echo $! > '"$d"'/b; wait' &
sh -c 'setsid sh -c "echo \$\$ > '"$d"'/c; exec sleep 1000" & exit 0'
sleep 1000
`
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	term := h.start(t, "kill", "sh", script, h.dir)
	pids := map[string]int{}
	for _, name := range []string{"a", "b", "c"} {
		pids[name] = readPid(t, filepath.Join(h.dir, name))
	}
	for name, pid := range pids {
		if !alive(pid) {
			t.Fatalf("%s (%d) is not running before the kill", name, pid)
		}
	}
	exit := term.Kill(200 * time.Millisecond)
	if exit.Signal == "" {
		t.Fatalf("killed terminal reported %+v", exit)
	}
	deadline := time.Now().Add(5 * time.Second)
	for name, pid := range pids {
		for alive(pid) {
			if time.Now().After(deadline) {
				_ = syscall.Kill(pid, syscall.SIGKILL)
				t.Fatalf("%s (%d) survived the kill", name, pid)
			}
			time.Sleep(10 * time.Millisecond)
		}
	}
}

func TestEnvironmentOfTheProgram(t *testing.T) {
	t.Setenv("DAEDALUS_PTYD_SECRET", "x")
	t.Setenv("CLAUDECODE", "1")
	h := newHarness(t)
	term := h.start(t, "env", "env")
	waitDone(t, term)
	out, _, _, _ := term.ReadOutput(0, 1<<20, true)
	s := string(out)
	for _, want := range []string{"TERM=xterm-256color", "COLORTERM=truecolor", "DAEDALUS_TERMINAL_ID=env"} {
		if !strings.Contains(s, want+"\n") {
			t.Errorf("missing %s", want)
		}
	}
	for _, gone := range []string{"DAEDALUS_PTYD_SECRET", "CLAUDECODE"} {
		if strings.Contains(s, gone) {
			t.Errorf("%s reached the program", gone)
		}
	}
	utf8 := false
	for _, line := range strings.Split(s, "\n") {
		for _, k := range []string{"LC_ALL=", "LC_CTYPE=", "LANG="} {
			if strings.HasPrefix(line, k) && procenv.IsUTF8Locale(line) {
				utf8 = true
			}
		}
	}
	if !utf8 {
		t.Errorf("no UTF-8 locale in %q", s)
	}
}

func TestAgentWriteReachesTheProgramAndTheJournal(t *testing.T) {
	h := newHarness(t)
	term := h.start(t, "cat", "cat")
	r, err := term.AgentWrite(context.Background(), "text", []byte("ping\r"), Origin{Actor: "tester"}, true, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if r.Bytes != 5 || r.DeliveredAt.IsZero() {
		t.Fatalf("receipt %+v", r)
	}
	waitOutput(t, term, "ping\r\nping\r\n") // the echo, then cat's copy
	data, _ := os.ReadFile(filepath.Join(h.dir, "journal.jsonl"))
	if !strings.Contains(string(data), `"actor":"tester"`) || !strings.Contains(string(data), `"text":"ping\r"`) {
		t.Fatalf("journal %s", data)
	}
	term.Kill(100 * time.Millisecond)
	if _, err := term.AgentWrite(context.Background(), "text", []byte("x"), Origin{Actor: "t"}, false, time.Second); err != ErrExited {
		t.Fatalf("write after exit: %v", err)
	}
}

func TestResizeAndForegroundSignal(t *testing.T) {
	h := newHarness(t)
	term := h.start(t, "sz", "sh")
	if err := term.Resize(100, 30, 0, 0); err != nil {
		t.Fatal(err)
	}
	emu := <-h.fakes
	if c, r := emu.Size(); c != 100 || r != 30 {
		t.Fatalf("emulator size %dx%d", c, r)
	}
	if _, err := term.AgentWrite(context.Background(), "text", []byte("stty size\r"), Origin{Actor: "t"}, false, time.Second); err != nil {
		t.Fatal(err)
	}
	waitOutput(t, term, "30 100")
	// A foreground job gets the interrupt; the shell survives it.
	if _, err := term.AgentWrite(context.Background(), "text", []byte("sleep 1000\r"), Origin{Actor: "t"}, false, time.Second); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for !term.Info().Busy {
		if time.Now().After(deadline) {
			t.Fatal("the sleep never showed as busy")
		}
		time.Sleep(10 * time.Millisecond)
	}
	// The job is in the foreground a moment before it is sleep: the shell hands the terminal to the
	// new group while its child is still a copy of the shell, which has not yet put the interrupt
	// back to the default. An interrupt in that moment is lost, as a Ctrl+C typed then would be (the
	// macOS runner lands in it now and then), so it is sent again until the job ends, as a person
	// would press it again.
	var sent time.Time
	for ; term.Info().Busy; time.Sleep(10 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("the interrupt did not end the foreground job")
		}
		if time.Since(sent) > 500*time.Millisecond {
			if err := term.Signal(syscall.SIGINT, true); err != nil {
				t.Fatal(err)
			}
			sent = time.Now()
		}
	}
	if !term.Running() {
		t.Fatal("the interrupt ended the shell")
	}
}

func TestRegistryRules(t *testing.T) {
	h := newHarness(t)
	a := h.start(t, "one", "sleep", "1000")
	if _, err := h.reg.Create(Spec{ID: "one"}); err != ErrExists {
		t.Fatalf("duplicate id: %v", err)
	}
	if err := h.reg.Forget("one"); err != ErrRunning {
		t.Fatalf("forget running: %v", err)
	}
	a.Kill(0)
	if err := h.reg.Forget("one"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.reg.Get("one"); err != ErrNotFound {
		t.Fatalf("after forget: %v", err)
	}
	if err := a.WithEmulator(func(emulator.Emulator) {}); err != ErrForgotten {
		t.Fatalf("emulator after forget: %v", err)
	}
	emu := <-h.fakes
	deadline := time.Now().Add(5 * time.Second)
	for !emu.Closed() {
		if time.Now().After(deadline) {
			t.Fatal("the emulator was not closed")
		}
		time.Sleep(10 * time.Millisecond)
	}
	for i := 0; i < 8; i++ {
		h.start(t, "n"+strconv.Itoa(i), "sleep", "1000")
	}
	if _, err := h.reg.Create(Spec{ID: "over"}); err != ErrLimit {
		t.Fatalf("over the limit: %v", err)
	}
}

func TestExitIsNotHeldByABackgroundJob(t *testing.T) {
	h := newHarness(t)
	pidFile := filepath.Join(h.dir, "bg")
	// The job ignores the hangup and keeps the PTY open after the program exits.
	term := h.start(t, "bg", "sh", "-c", `(trap "" HUP; echo $$ > `+pidFile+`; exec sleep 1000) & sleep 0.2; exit 5`)
	bg := readPid(t, pidFile)
	defer syscall.Kill(bg, syscall.SIGKILL)
	start := time.Now()
	waitDone(t, term)
	if e, _ := term.Exit(); e.Code != 5 {
		t.Fatalf("exit %+v", e)
	}
	if d := time.Since(start); d > 3*time.Second {
		t.Fatalf("the exit took %s", d)
	}
}
