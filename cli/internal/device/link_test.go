package device_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/client"
	"github.com/ascorblack/daedalus/cli/internal/device"
	"github.com/ascorblack/daedalus/cli/internal/exec"
	"github.com/ascorblack/daedalus/cli/internal/fakeserver"
	"github.com/ascorblack/daedalus/cli/internal/guard"
)

type rig struct {
	srv     *fakeserver.Server
	root    string
	session string
	guard   *guard.Guard
	link    *device.Link
	cancel  context.CancelFunc
	done    chan error
	mu      sync.Mutex
	states  []device.State
}

func newRig(t *testing.T, level guard.Level, prompter guard.Prompter) *rig {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("the calls below are POSIX shell")
	}
	home := t.TempDir()
	t.Setenv("HOME", home)
	root := filepath.Join(home, "project")
	if err := os.Mkdir(root, 0o755); err != nil {
		t.Fatal(err)
	}
	srv := fakeserver.New()
	t.Cleanup(srv.Close)
	srv.AddToken("dcl_test", "dev-1")
	c := client.New(srv.URL, "dcl_test")
	ref, err := c.CreateSession(context.Background(), client.NewSession{Kind: "remote", Cwd: root})
	if err != nil {
		t.Fatal(err)
	}
	ex, err := exec.New(nil)
	if err != nil {
		t.Fatal(err)
	}
	r := &rig{srv: srv, root: root, session: ref.ID, guard: guard.New(level, prompter), done: make(chan error, 1)}
	r.link = device.New(device.Options{Server: srv.URL, Token: "dcl_test", ClientVersion: "test", Executor: ex, Guard: r.guard,
		MinBackoff: 20 * time.Millisecond, MaxBackoff: 100 * time.Millisecond,
		Sessions: func() []device.SessionGuard {
			return []device.SessionGuard{{SessionID: ref.ID, Guard: r.guard.Level().String()}}
		},
		OnStatus: func(s device.Status) {
			r.mu.Lock()
			r.states = append(r.states, s.State)
			r.mu.Unlock()
		}})
	ctx, cancel := context.WithCancel(context.Background())
	r.cancel = cancel
	go func() { r.done <- r.link.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		<-r.done
	})
	wctx, wcancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer wcancel()
	if !srv.WaitRelay(wctx) {
		t.Fatal("the device never connected")
	}
	return r
}

func (r *rig) call(t *testing.T, method, intent string, params map[string]any) (map[string]any, *fakeserver.RPCError, string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	params["_meta"] = map[string]any{"session_id": r.session, "tool_call_id": "c", "tool": "Exec", "intent": intent}
	return r.srv.Call(ctx, r.session+":c:"+strings.ReplaceAll(t.Name(), "/", "-")+method+time.Now().Format("150405.000000000"), method, params)
}

func TestExecAndFilesOverTheDeviceConnection(t *testing.T) {
	r := newRig(t, guard.Auto, nil)
	res, rpcErr, _ := r.call(t, "exec.run", "exec", map[string]any{"argv": []string{"bash", "-c", "echo hello from $(basename $PWD)"}})
	if rpcErr != nil {
		t.Fatal(rpcErr)
	}
	if strings.TrimSpace(res["stdout"].(string)) != "hello from project" || res["exit_code"] != float64(0) {
		t.Fatalf("exec: %v", res)
	}
	target := filepath.Join(r.root, "notes.txt")
	if _, rpcErr, _ := r.call(t, "fs.write", "write", map[string]any{"path": target, "offset": 0, "data_b64": []byte("hi"), "final": true}); rpcErr != nil {
		t.Fatal(rpcErr)
	}
	got, rpcErr, _ := r.call(t, "fs.read", "read", map[string]any{"path": target})
	if rpcErr != nil || got["data_b64"] != "aGk=" {
		t.Fatalf("read: %v %v", got, rpcErr)
	}
	_, rpcErr, _ = r.call(t, "fs.read", "read", map[string]any{"path": "/etc/hostname"})
	if rpcErr == nil || rpcErr.Code != 1004 || rpcErr.Reason() != "outside_root" {
		t.Fatalf("outside the folder: %v", rpcErr)
	}
	// The hello named the session this process shows and its guard.
	hellos := r.srv.Hellos()
	if len(hellos) == 0 || len(hellos[0].Sessions) != 1 || hellos[0].Sessions[0]["guard"] != "auto" || hellos[0].Protocol != 1 {
		t.Errorf("hello: %+v", hellos)
	}
}

func TestPlanGuardRefusesWritesAndCommands(t *testing.T) {
	r := newRig(t, guard.Plan, nil)
	_, rpcErr, _ := r.call(t, "exec.run", "exec", map[string]any{"argv": []string{"bash", "-c", "touch made"}})
	if rpcErr == nil || rpcErr.Code != 1004 || rpcErr.Reason() != "plan_guard" {
		t.Fatalf("a command ran in plan mode: %v", rpcErr)
	}
	if _, err := os.Stat(filepath.Join(r.root, "made")); err == nil {
		t.Fatal("the file was made")
	}
	_, rpcErr, _ = r.call(t, "fs.write", "write", map[string]any{"path": filepath.Join(r.root, "w"), "offset": 0, "data_b64": []byte("x"), "final": true})
	if rpcErr == nil || rpcErr.Reason() != "plan_guard" {
		t.Fatalf("a write went through in plan mode: %v", rpcErr)
	}
	// A read through the shell, with the server's prelude in front, still runs.
	res, rpcErr, _ := r.call(t, "exec.run", "read", map[string]any{"argv": []string{"bash", "-c", "for profile in \"$HOME/.bash_profile\" \"$HOME/.bash_login\" \"$HOME/.profile\"; do\n  if [ -r \"$profile\" ]; then . \"$profile\" >/dev/null 2>&1 </dev/null; break; fi\ndone\nexec 2>&1\nls -1"}})
	if rpcErr != nil {
		t.Fatalf("a read-only command was refused: %v", rpcErr)
	}
	_ = res
}

type refuser struct{ asked int }

func (f *refuser) Confirm(ctx context.Context, a guard.Action) (guard.Decision, error) {
	f.asked++
	return guard.Decision{Allow: false, Reason: "not on my laptop"}, nil
}

func TestAskGuardCarriesTheOperatorsRefusal(t *testing.T) {
	p := &refuser{}
	r := newRig(t, guard.Ask, p)
	_, rpcErr, _ := r.call(t, "exec.run", "exec", map[string]any{"argv": []string{"bash", "-c", "rm -f x"}})
	if rpcErr == nil || rpcErr.Reason() != "refused_by_operator" || !strings.Contains(rpcErr.Message, "not on my laptop") {
		t.Fatalf("%v", rpcErr)
	}
	if p.asked != 1 {
		t.Errorf("asked %d times", p.asked)
	}
}

// The heart of the disconnect contract: a call running when the link drops finishes once, and the
// server gets its result after the reconnect without the command running again.
func TestReconnectNeverRunsACallTwice(t *testing.T) {
	cases := []struct {
		name   string
		resend bool
		// offline keeps the device away until the command has finished, so the hello says done.
		offline bool
	}{
		{name: "running at the reconnect"},
		{name: "finished while offline", offline: true},
		{name: "server re-sends the same id", resend: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := newRig(t, guard.Auto, nil)
			r.srv.ResendOnReconnect = tc.resend
			marker := filepath.Join(r.root, "ran.log")
			type outcome struct {
				res     map[string]any
				err     *fakeserver.RPCError
				outcome string
			}
			got := make(chan outcome, 1)
			id := r.session + ":call-1:1"
			go func() {
				ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
				defer cancel()
				res, err, o := r.srv.Call(ctx, id, "exec.run", map[string]any{
					"argv":  []string{"bash", "-c", "echo ran >> ran.log; sleep 1; echo finished"},
					"_meta": map[string]any{"session_id": r.session, "tool_call_id": "call-1", "intent": "exec"}})
				got <- outcome{res, err, o}
			}()
			// Wait for the command to start, then cut the link under it.
			deadline := time.Now().Add(5 * time.Second)
			for {
				if b, _ := os.ReadFile(marker); len(b) > 0 {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("the command never started")
				}
				time.Sleep(10 * time.Millisecond)
			}
			if tc.offline {
				r.srv.RefuseRelay(1000)
			}
			r.srv.DropRelay()
			if tc.offline {
				time.Sleep(1500 * time.Millisecond)
				r.srv.RefuseRelay(0)
			}
			var o outcome
			select {
			case o = <-got:
			case <-time.After(20 * time.Second):
				t.Fatal("no result after the reconnect")
			}
			if o.err != nil || o.outcome != "ok" {
				t.Fatalf("outcome %q, error %v", o.outcome, o.err)
			}
			if strings.TrimSpace(o.res["stdout"].(string)) != "finished" {
				t.Errorf("result: %v", o.res)
			}
			if b, _ := os.ReadFile(marker); string(b) != "ran\n" {
				t.Errorf("the command ran %d times", strings.Count(string(b), "ran"))
			}
			hellos := r.srv.Hellos()
			if len(hellos) < 2 {
				t.Fatalf("no second hello: %+v", hellos)
			}
			last := hellos[len(hellos)-1]
			want := "running"
			if tc.offline {
				want = "done"
			}
			found := false
			for _, k := range last.Known {
				if k["id"] == id {
					found = true
					if k["state"] != want {
						t.Errorf("the hello reported %s, want %s", k["state"], want)
					}
				}
			}
			if !found {
				t.Errorf("the hello did not list the call: %+v", last.Known)
			}
			if tc.resend && r.srv.Sent(id) != 2 {
				t.Errorf("the request was sent %d times, want 2 for this case", r.srv.Sent(id))
			}
		})
	}
}

func TestRevokedTokenStopsTheLink(t *testing.T) {
	r := newRig(t, guard.Auto, nil)
	r.srv.Revoke()
	select {
	case err := <-r.done:
		if !errors.Is(err, device.ErrUnauthorized) {
			t.Fatalf("%v", err)
		}
		r.done <- err
	case <-time.After(5 * time.Second):
		t.Fatal("the link kept going after its token was revoked")
	}
}

func TestDescribeStripsTheServerPrelude(t *testing.T) {
	a := device.Describe("exec.run", []byte(`{"argv":["bash","-c","for profile in \"$HOME/.bash_profile\" \"$HOME/.bash_login\" \"$HOME/.profile\"; do\n  if [ -r \"$profile\" ]; then . \"$profile\" >/dev/null 2>&1 </dev/null; break; fi\ndone\nexec 2>&1\nnpm test"],"_meta":{"session_id":"s9"}}`))
	if a.Command != "npm test" || a.Session != "s9" || a.Kind != guard.Exec {
		t.Errorf("%+v", a)
	}
	if guard.ReadOnlyCommand(device.Script([]string{"bash", "-c", "x; ls"})) {
		t.Error("an unknown program passed as read-only")
	}
}

func TestCancelEndsTheCommandAndOutputStreams(t *testing.T) {
	r := newRig(t, guard.Auto, nil)
	id := r.session + ":call-c:1"
	got := make(chan map[string]any, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		res, _, _ := r.srv.Call(ctx, id, "exec.run", map[string]any{
			"argv":  []string{"bash", "-c", "echo started; sleep 30; echo never"},
			"_meta": map[string]any{"session_id": r.session, "tool_call_id": "call-c", "intent": "exec"}})
		got <- res
	}()
	deadline := time.Now().Add(5 * time.Second)
	for !strings.Contains(r.srv.Output(id), "started") {
		if time.Now().After(deadline) {
			t.Fatal("no output was streamed")
		}
		time.Sleep(20 * time.Millisecond)
	}
	start := time.Now()
	if err := r.srv.Cancel(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	select {
	case res := <-got:
		if res["cancelled"] != true || strings.Contains(res["stdout"].(string), "never") {
			t.Errorf("result: %v", res)
		}
		if time.Since(start) > 10*time.Second {
			t.Errorf("the cancel took %s", time.Since(start))
		}
	case <-time.After(15 * time.Second):
		t.Fatal("the command outlived its cancel")
	}
}

// A quiet link to a server that never pings stays up on the client's own pings.
func TestAQuietLinkStaysUp(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("rig is POSIX")
	}
	home := t.TempDir()
	t.Setenv("HOME", home)
	srv := fakeserver.New()
	defer srv.Close()
	srv.AddToken("dcl_test", "dev-1")
	ex, err := exec.New(nil)
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	connects := 0
	link := device.New(device.Options{Server: srv.URL, Token: "dcl_test", ClientVersion: "test", Executor: ex, Silence: 400 * time.Millisecond,
		MinBackoff: 20 * time.Millisecond, OnStatus: func(s device.Status) {
			if s.State == device.Connected {
				mu.Lock()
				connects++
				mu.Unlock()
			}
		}})
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = link.Run(ctx)
	mu.Lock()
	defer mu.Unlock()
	if connects != 1 {
		t.Errorf("the link connected %d times in 2 s of silence", connects)
	}
}

// A command still running at its hand-over time is answered at that moment and kept as a job: the
// server reads its status and its log afterwards, between turns, and hears its exit.
func TestAForegroundCommandIsKeptAsAJobAndReportsItsEnd(t *testing.T) {
	r := newRig(t, guard.Ask, &refuser{})
	log := filepath.Join(r.root, ".jobs", "job-1-1.log")
	// The ask guard refuses commands, so this one runs under auto; the job calls below run under ask.
	r.guard.Set(guard.Auto)
	res, rpcErr, _ := r.call(t, "exec.run", "exec", map[string]any{
		"argv": []string{"bash", "-c", "echo early; sleep 1; echo late; exit 4"},
		"job":  map[string]any{"id": "job-1-1", "log": log, "after_ms": 300}})
	if rpcErr != nil {
		t.Fatal(rpcErr)
	}
	if res["handed_over"] != true || res["pid"] == nil || !strings.Contains(res["stdout"].(string), "early") || strings.Contains(res["stdout"].(string), "late") {
		t.Fatalf("exec: %v", res)
	}
	r.guard.Set(guard.Ask)
	status, rpcErr, _ := r.call(t, "job.status", "read", map[string]any{"job": "job-1-1"})
	if rpcErr != nil || status["state"] != "running" {
		t.Fatalf("status while running: %v %v", status, rpcErr)
	}
	deadline := time.Now().Add(10 * time.Second)
	for status["state"] == "running" && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
		status, rpcErr, _ = r.call(t, "job.status", "read", map[string]any{"job": "job-1-1"})
		if rpcErr != nil {
			t.Fatal(rpcErr)
		}
	}
	if status["state"] != "exited" || status["exit_code"] != float64(4) {
		t.Fatalf("status at the end: %v", status)
	}
	tail, rpcErr, _ := r.call(t, "job.tail", "read", map[string]any{"job": "job-1-1", "lines": 10})
	if rpcErr != nil || tail["text"] != "early\nlate" {
		t.Fatalf("tail: %v %v", tail, rpcErr)
	}
	if _, rpcErr, _ := r.call(t, "job.status", "read", map[string]any{"job": "job-9-9"}); rpcErr == nil || rpcErr.Code != 1001 {
		t.Fatalf("an unknown job: %v", rpcErr)
	}
	hellos := r.srv.Hellos()
	if len(hellos) == 0 || hellos[0].Capabilities["jobs"] != true {
		t.Errorf("the hello did not say this client keeps jobs: %+v", hellos)
	}
}

// A background job is a command handed over at once; job.kill ends it whatever the guard.
func TestABackgroundJobStartsAtOnceAndJobKillEndsIt(t *testing.T) {
	r := newRig(t, guard.Auto, nil)
	log := filepath.Join(r.root, ".jobs", "job-2-2.log")
	res, rpcErr, _ := r.call(t, "exec.run", "exec", map[string]any{
		"argv": []string{"bash", "-c", "echo serving; sleep 30"},
		"job":  map[string]any{"id": "job-2-2", "log": log, "after_ms": 0}})
	if rpcErr != nil || res["handed_over"] != true {
		t.Fatalf("exec: %v %v", res, rpcErr)
	}
	r.guard.Set(guard.Plan)
	killed, rpcErr, _ := r.call(t, "job.kill", "read", map[string]any{"job": "job-2-2"})
	if rpcErr != nil || killed["state"] != "exited" {
		t.Fatalf("kill: %v %v", killed, rpcErr)
	}
	again, rpcErr, _ := r.call(t, "job.status", "read", map[string]any{"job": "job-2-2"})
	if rpcErr != nil || again["state"] != "exited" || again["signal"] == "" {
		t.Fatalf("status after the kill: %v %v", again, rpcErr)
	}
}
