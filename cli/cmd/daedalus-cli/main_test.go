package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/auth"
	"github.com/ascorblack/daedalus/cli/internal/config"
	"github.com/ascorblack/daedalus/cli/internal/fakeserver"
)

// harness is a machine with daedalus-cli configured against a fake server, working in a project
// folder under a home of its own.
type harness struct {
	srv  *fakeserver.Server
	root string
}

func newHarness(t *testing.T, token string) *harness {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("the fake agent's commands are POSIX shell")
	}
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv(config.EnvConfigDir, filepath.Join(home, "config"))
	t.Setenv(auth.EnvStore, "file")
	t.Setenv(auth.EnvToken, "")
	t.Setenv(config.EnvServer, "")
	t.Setenv(config.EnvProfile, "")
	root := filepath.Join(home, "project")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "notes.txt"), []byte("hi"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Chdir(root)
	srv := fakeserver.New()
	t.Cleanup(srv.Close)
	srv.AddToken("dcl_good", "dev-1")
	cfg, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	cfg.Put(config.DefaultProfile, config.Profile{Server: srv.URL, DeviceID: "dev-1"})
	if err := cfg.Save(); err != nil {
		t.Fatal(err)
	}
	if token != "" {
		dir, _ := config.Dir()
		store, err := auth.OpenStore(dir)
		if err != nil {
			t.Fatal(err)
		}
		if err := store.Set(config.DefaultProfile, token); err != nil {
			t.Fatal(err)
		}
	}
	return &harness{srv: srv, root: root}
}

func (h *harness) run(t *testing.T, args ...string) (int, string, string) {
	t.Helper()
	var out, errOut bytes.Buffer
	done := make(chan int, 1)
	go func() { done <- run(args, strings.NewReader(""), &out, &errOut) }()
	select {
	case code := <-done:
		return code, out.String(), errOut.String()
	case <-time.After(60 * time.Second):
		t.Fatalf("daedalus-cli %v did not finish", args)
	}
	return 0, "", ""
}

func listAndAnswer(t *fakeserver.Turn) {
	res, rpcErr, _ := t.Exec("ls -1")
	if rpcErr != nil {
		t.Say("could not list: " + rpcErr.Message)
		return
	}
	t.Say("I see: " + strings.TrimSpace(res["stdout"].(string)))
}

func TestHeadlessText(t *testing.T) {
	h := newHarness(t, "dcl_good")
	h.srv.Turn = listAndAnswer
	code, out, errOut := h.run(t, "-p", "what is here?")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	if strings.TrimSpace(out) != "I see: notes.txt" {
		t.Fatalf("stdout %q", out)
	}
	// The session was born here, bound to this folder, with the default guard.
	sessions := h.srv.Sessions()
	if len(sessions) != 1 || sessions[0].Cwd != h.root || sessions[0].Guard != "auto" {
		t.Fatalf("sessions: %+v", sessions)
	}
}

func TestHeadlessStreamJSON(t *testing.T) {
	h := newHarness(t, "dcl_good")
	h.srv.Turn = listAndAnswer
	code, out, errOut := h.run(t, "list the files", "--output-format", "stream-json", "-p")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	var types []string
	var last map[string]any
	scanner := bufio.NewScanner(strings.NewReader(out))
	for scanner.Scan() {
		var rec map[string]any
		if err := json.Unmarshal(scanner.Bytes(), &rec); err != nil {
			t.Fatalf("not one JSON object per line: %q", scanner.Text())
		}
		types = append(types, rec["type"].(string))
		last = rec
	}
	joined := strings.Join(types, ",")
	for _, want := range []string{"init", "tool_use", "tool_result", "text", "result"} {
		if !strings.Contains(joined, want) {
			t.Errorf("no %s record in %s", want, joined)
		}
	}
	if types[0] != "init" || last["type"] != "result" || last["subtype"] != "success" || last["result"] != "I see: notes.txt" || last["exit_code"] != float64(0) {
		t.Errorf("first %s, last %v", types[0], last)
	}
}

func TestHeadlessJSON(t *testing.T) {
	h := newHarness(t, "dcl_good")
	code, out, _ := h.run(t, "-p", "hello", "--output-format", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var rec map[string]any
	if err := json.Unmarshal([]byte(out), &rec); err != nil {
		t.Fatalf("not one object: %q", out)
	}
	if rec["type"] != "result" || rec["result"] != "ok: hello" || rec["is_error"] != false {
		t.Errorf("%v", rec)
	}
}

func TestHeadlessPlanGuardRefusesAndTheTurnGoesOn(t *testing.T) {
	h := newHarness(t, "dcl_good")
	h.srv.Turn = func(t *fakeserver.Turn) {
		_, rpcErr, _ := t.Exec("touch made.txt")
		if rpcErr != nil {
			t.Say("refused: " + rpcErr.Message)
			return
		}
		t.Say("made it")
	}
	code, out, errOut := h.run(t, "-p", "make a file", "--guard", "plan")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	if !strings.HasPrefix(strings.TrimSpace(out), "refused: ") || !strings.Contains(out, "plan mode") {
		t.Errorf("stdout %q", out)
	}
	if _, err := os.Stat(filepath.Join(h.root, "made.txt")); err == nil {
		t.Error("plan mode let the command write")
	}
	if g := h.srv.Sessions()[0].Guard; g != "plan" {
		t.Errorf("the session was created under guard %q", g)
	}
}

func TestHeadlessWaitsForAnApprovalExitCode(t *testing.T) {
	h := newHarness(t, "dcl_good")
	h.srv.Turn = func(t *fakeserver.Turn) {
		if t.Approval("Exec", "npm publish") == "grant" {
			t.Say("published")
		}
	}
	code, _, errOut := h.run(t, "-p", "publish it")
	if code != 5 || !strings.Contains(errOut, "npm publish") {
		t.Fatalf("exit %d: %s", code, errOut)
	}
}

func TestHeadlessCredentialExitCodes(t *testing.T) {
	h := newHarness(t, "dcl_revoked")
	if code, _, errOut := h.run(t, "-p", "hi"); code != 3 {
		t.Errorf("a refused token: exit %d (%s)", code, errOut)
	}
	newHarness(t, "")
	if code, _, errOut := newHarnessRun(t); code != 3 || !strings.Contains(errOut, "login") {
		t.Errorf("no token: exit %d (%s)", code, errOut)
	}
}

func newHarnessRun(t *testing.T) (int, string, string) {
	var out, errOut bytes.Buffer
	code := run([]string{"-p", "hi"}, strings.NewReader(""), &out, &errOut)
	return code, out.String(), errOut.String()
}

func TestUsageExitCodes(t *testing.T) {
	newHarness(t, "dcl_good")
	var out, errOut bytes.Buffer
	if code := run([]string{"-p"}, strings.NewReader(""), &out, &errOut); code != 2 {
		t.Errorf("-p without a prompt: %d", code)
	}
	if code := run([]string{"-p", "x", "--output-format", "yaml"}, strings.NewReader(""), &out, &errOut); code != 2 {
		t.Errorf("an unknown format: %d", code)
	}
	if code := run([]string{"--no-such-flag"}, strings.NewReader(""), &out, &errOut); code != 2 {
		t.Errorf("an unknown flag: %d", code)
	}
}

func TestLoginStatusLogout(t *testing.T) {
	h := newHarness(t, "")
	go func() {
		for i := 0; i < 200; i++ {
			if codes := h.srv.PendingCodes(); len(codes) > 0 {
				h.srv.Approve(codes[0])
				return
			}
			time.Sleep(20 * time.Millisecond)
		}
	}()
	code, out, errOut := h.run(t, "login", h.srv.URL, "--name", "laptop")
	if code != 0 {
		t.Fatalf("login: exit %d: %s", code, errOut)
	}
	if !strings.Contains(out, "Paired as \"laptop\"") || !strings.Contains(out, "█") {
		t.Errorf("login printed %q", out)
	}
	code, out, errOut = h.run(t, "status")
	if code != 0 || !strings.Contains(out, "contract 1") {
		t.Fatalf("status: exit %d: %s %s", code, out, errOut)
	}
	code, _, _ = h.run(t, "doctor")
	if code != 0 {
		t.Errorf("doctor: exit %d", code)
	}
	if code, _, _ := h.run(t, "logout"); code != 0 {
		t.Fatalf("logout: %d", code)
	}
	if code, _, _ := h.run(t, "-p", "hi"); code != 3 {
		t.Errorf("after logout: exit %d", code)
	}
}

func TestSessionsAndContinue(t *testing.T) {
	h := newHarness(t, "dcl_good")
	if code, _, errOut := h.run(t, "-p", "first"); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	code, out, _ := h.run(t, "sessions")
	if code != 0 || !strings.Contains(out, h.srv.Sessions()[0].ID) {
		t.Fatalf("sessions: %d %q", code, out)
	}
	code, out, errOut := h.run(t, "-c", "-p", "second")
	if code != 0 || strings.TrimSpace(out) != "ok: second" {
		t.Fatalf("continue: exit %d %q %s", code, out, errOut)
	}
	if n := len(h.srv.Sessions()); n != 1 {
		t.Errorf("-c started a new session: %d sessions", n)
	}
}

// The operator's rule end to end: both connections drop in the middle of a turn, the client comes
// back on its own, the turn finishes, and the command it was running ran exactly once.
func TestHeadlessSurvivesADropMidTurn(t *testing.T) {
	h := newHarness(t, "dcl_good")
	h.srv.Turn = func(t *fakeserver.Turn) {
		res, rpcErr, outcome := t.Exec("echo ran >> ran.log; sleep 1; echo finished")
		if rpcErr != nil || outcome != "ok" {
			t.Fail("the call ended " + outcome)
			return
		}
		t.Say("it " + strings.TrimSpace(res["stdout"].(string)))
	}
	marker := filepath.Join(h.root, "ran.log")
	go func() {
		for i := 0; i < 500; i++ {
			if b, _ := os.ReadFile(marker); len(b) > 0 {
				h.srv.DropStreams()
				h.srv.DropRelay()
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	}()
	code, out, errOut := h.run(t, "-p", "run it", "--output-format", "stream-json")
	if code != 0 {
		t.Fatalf("exit %d: %s\n%s", code, errOut, out)
	}
	if !strings.Contains(out, `"result":"it finished"`) {
		t.Errorf("no final answer in %s", out)
	}
	if !strings.Contains(out, `"state":"reconnecting"`) {
		t.Errorf("the drop was not reported: %s", out)
	}
	if b, _ := os.ReadFile(marker); string(b) != "ran\n" {
		t.Errorf("the command ran %d times", strings.Count(string(b), "ran"))
	}
	if n := len(h.srv.Hellos()); n < 2 {
		t.Errorf("the device did not reconnect: %d hellos", n)
	}
}
