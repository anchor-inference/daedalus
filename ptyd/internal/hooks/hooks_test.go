//go:build unix

package hooks

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
)

// recorder is the event log's stand-in: it keeps what was published and wakes whoever waits.
type recorder struct {
	mu     sync.Mutex
	events []events.Event
	wake   chan struct{}
}

func newRecorder() *recorder { return &recorder{wake: make(chan struct{}, 1024)} }

func (r *recorder) PublishSized(typ, terminalID string, data any, size int) events.Event {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := events.Event{Seq: int64(len(r.events) + 1), Type: typ, TerminalID: terminalID, Data: data}
	r.events = append(r.events, e)
	r.wake <- struct{}{}
	return e
}

func (r *recorder) of(typ string) []map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []map[string]any
	for _, e := range r.events {
		if e.Type == typ {
			out = append(out, e.Data.(map[string]any))
		}
	}
	return out
}

func (r *recorder) wait(t *testing.T, typ string, n int) []map[string]any {
	t.Helper()
	deadline := time.After(10 * time.Second)
	for {
		if got := r.of(typ); len(got) >= n {
			return got
		}
		select {
		case <-r.wake:
		case <-deadline:
			t.Fatalf("waited for %d %s events, have %d", n, typ, len(r.of(typ)))
		}
	}
}

type fixture struct {
	reg *Registry
	rec *recorder
	url string
	dir string
}

func start(t *testing.T) *fixture {
	t.Helper()
	dir := t.TempDir()
	rec := newRecorder()
	reg, err := NewRegistry(dir, "/opt/ptyd/ptyd", rec, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	srv, ln, err := reg.Listen("127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		reg.Close()
		srv.Close()
	})
	return &fixture{reg: reg, rec: rec, url: "http://" + ln.Addr().String(), dir: dir}
}

func post(t *testing.T, url, token string, body []byte) (int, string) {
	t.Helper()
	code, b, err := postErr(url, token, body)
	if err != nil {
		t.Fatal(err)
	}
	return code, b
}

// postErr is post for goroutines, which may not end the test themselves.
func postErr(url, token string, body []byte) (int, string, error) {
	req, _ := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b), nil
}

func TestRegisterWritesFilesAndUnregisterRemovesThem(t *testing.T) {
	f := start(t)
	r, err := f.reg.Register(Spec{ID: "L1", TerminalID: "t1", Files: map[string][]byte{"settings.json": []byte(`{"hooks":{}}`), ".mcp.json": []byte("{}")}, Ports: []int{4242}})
	if err != nil {
		t.Fatal(err)
	}
	if r.HookURL != f.url+"/hook/L1" || len(r.Token) != 64 || r.Dir != filepath.Join(f.dir, "launches", "L1") {
		t.Fatalf("%+v", r)
	}
	want := map[string]string{
		"DAEDALUS_LAUNCH_ID": "L1", "DAEDALUS_HOOK_URL": r.HookURL, "DAEDALUS_HOOK_TOKEN": r.Token,
		"DAEDALUS_HOOK_CMD": filepath.Join(f.dir, "bin", "hook-post"), "DAEDALUS_PTYD_BIN": "/opt/ptyd/ptyd",
		"DAEDALUS_DIAL_DIR": r.DialDir, "DAEDALUS_LAUNCH_DIR": r.Dir,
	}
	for k, v := range want {
		if r.Env[k] != v {
			t.Errorf("%s = %q, want %q", k, r.Env[k], v)
		}
	}
	st, err := os.Stat(filepath.Join(r.Dir, "settings.json"))
	if err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", st, err)
	}
	if st, err := os.Stat(r.Dir); err != nil || st.Mode().Perm() != 0o700 {
		t.Fatalf("launch dir %v %v", st, err)
	}
	if st, err := os.Stat(r.DialDir); err != nil || st.Mode().Perm() != 0o700 {
		t.Fatalf("dial dir %v %v", st, err)
	}
	l, _ := f.reg.Get("L1")
	if !l.Allowed(4242) || l.Allowed(4243) {
		t.Fatal("ports")
	}
	if !f.reg.Unregister("L1", "unregistered") {
		t.Fatal("not unregistered")
	}
	if _, err := os.Stat(r.Dir); !os.IsNotExist(err) {
		t.Fatalf("launch dir left behind: %v", err)
	}
	if _, err := os.Stat(r.DialDir); !os.IsNotExist(err) {
		t.Fatalf("dial dir left behind: %v", err)
	}
	if l.Allowed(4242) {
		t.Fatal("a port outlived its launch")
	}
	ended := f.rec.of("launch.ended")
	if len(ended) != 1 || ended[0]["launch_id"] != "L1" {
		t.Fatalf("%v", ended)
	}
	if f.reg.Unregister("L1", "again") {
		t.Fatal("unregistered twice")
	}
}

func TestNestedFilesAndFilesAddedLater(t *testing.T) {
	f := start(t)
	skill := ".claude/skills/daedalus-team/SKILL.md"
	r, err := f.reg.Register(Spec{ID: "L1", Files: map[string][]byte{skill: []byte("# team"), ".claude/skills/other/SKILL.md": []byte("x"), "settings.json": []byte("{}")}})
	if err != nil {
		t.Fatal(err)
	}
	if b, err := os.ReadFile(filepath.Join(r.Dir, ".claude", "skills", "daedalus-team", "SKILL.md")); err != nil || string(b) != "# team" {
		t.Fatalf("%q %v", b, err)
	}
	if st, err := os.Stat(filepath.Join(r.Dir, ".claude", "skills")); err != nil || st.Mode().Perm() != 0o700 {
		t.Fatalf("%v %v", st, err)
	}
	path, err := f.reg.PutFile("L1", "message-sm-1.md", []byte("a long message"), false)
	if err != nil || path != filepath.Join(r.Dir, "message-sm-1.md") {
		t.Fatalf("%q %v", path, err)
	}
	if st, err := os.Stat(path); err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", st, err)
	}
	// A file added later is one new plain name: never a path, never over something already there,
	// never through a link the launch's programs planted.
	for _, name := range []string{"message-sm-1.md", "a/b.md", "../x", ""} {
		if _, err := f.reg.PutFile("L1", name, []byte("x"), false); err == nil {
			t.Errorf("%q was written", name)
		}
	}
	if err := os.Symlink("/tmp/elsewhere", filepath.Join(r.Dir, "planted.md")); err != nil {
		t.Fatal(err)
	}
	if _, err := f.reg.PutFile("L1", "planted.md", []byte("x"), false); err == nil {
		t.Error("a planted link was followed")
	}
	if _, err := f.reg.PutFile("L1", "big.md", make([]byte, config.MaxLaunchFileBytes+1), false); err == nil {
		t.Error("an oversized file was written")
	}
	// Replacing is the one way over an existing name: the new content whole, and a planted link
	// replaced rather than followed.
	if p, err := f.reg.PutFile("L1", "message-sm-1.md", []byte("the second value"), true); err != nil || p != path {
		t.Fatalf("%q %v", p, err)
	}
	if b, err := os.ReadFile(path); err != nil || string(b) != "the second value" {
		t.Fatalf("%q %v", b, err)
	}
	if st, err := os.Stat(path); err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", st, err)
	}
	if _, err := f.reg.PutFile("L1", "planted.md", []byte("x"), true); err != nil {
		t.Fatal(err)
	}
	if st, err := os.Lstat(filepath.Join(r.Dir, "planted.md")); err != nil || st.Mode()&os.ModeSymlink != 0 {
		t.Fatalf("the planted link is still there: %v %v", st, err)
	}
	entries, _ := os.ReadDir(r.Dir)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".put-") {
			t.Errorf("a temporary file was left: %s", e.Name())
		}
	}
	f.reg.Unregister("L1", "done")
	if _, err := f.reg.PutFile("L1", "late.md", []byte("x"), false); !errors.Is(err, ErrNoLaunch) {
		t.Fatalf("a file for an ended launch: %v", err)
	}
}

func TestRegisterRefusesBadInput(t *testing.T) {
	f := start(t)
	for _, s := range []Spec{
		{ID: "../x"},
		{ID: "ok", Files: map[string][]byte{"../escape": nil}},
		{ID: "ok", Files: map[string][]byte{"a/../../b": nil}},
		{ID: "ok", Files: map[string][]byte{"/etc/passwd": nil}},
		{ID: "ok", Files: map[string][]byte{"a//b": nil}},
		{ID: "ok", Files: map[string][]byte{"a/b/c/d/e/f/g": nil}},
		{ID: "ok", Files: map[string][]byte{"..": nil}},
		{ID: "ok", Files: map[string][]byte{"big": make([]byte, config.MaxLaunchFileBytes+1)}},
		{ID: "ok", Ports: []int{70000}},
	} {
		if _, err := f.reg.Register(s); err == nil {
			t.Errorf("%+v was accepted", s.ID)
		}
	}
	if _, err := f.reg.Register(Spec{ID: "dup"}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.reg.Register(Spec{ID: "dup"}); err != ErrLaunchExists {
		t.Fatalf("a second register: %v", err)
	}
	// An id is picked when none is given.
	r, err := f.reg.Register(Spec{})
	if err != nil || r.LaunchID == "" {
		t.Fatalf("%+v %v", r, err)
	}
}

func TestHookStatuses(t *testing.T) {
	f := start(t)
	r, err := f.reg.Register(Spec{ID: "L1", TerminalID: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	if code, _ := post(t, r.HookURL+"/Stop", r.Token, []byte(`{"x":1}`)); code != http.StatusNoContent {
		t.Fatalf("204: %d", code)
	}
	ev := f.rec.wait(t, "hook", 1)[0]
	if ev["launch_id"] != "L1" || ev["terminal_id"] != "t1" || ev["name"] != "Stop" || string(ev["body"].(json.RawMessage)) != `{"x":1}` {
		t.Fatalf("%v", ev)
	}
	if code, _ := post(t, r.HookURL+"/Stop", "wrong", nil); code != http.StatusUnauthorized {
		t.Fatalf("401: %d", code)
	}
	if code, _ := post(t, r.HookURL+"/Stop", "", nil); code != http.StatusUnauthorized {
		t.Fatalf("401 without a token: %d", code)
	}
	if code, _ := post(t, r.HookURL+"/Stop", r.Token, make([]byte, config.HookBodyBytes+1)); code != http.StatusRequestEntityTooLarge {
		t.Fatalf("413: %d", code)
	}
	if code, _ := post(t, f.url+"/hook/L1/../../x", r.Token, nil); code != http.StatusNotFound {
		t.Fatalf("a bad name: %d", code)
	}
	if len(f.rec.of("hook")) != 1 {
		t.Fatal("a refused post published an event")
	}
	// Faster than a launch may post.
	limited := false
	for i := 0; i < config.HookBurst*3 && !limited; i++ {
		code, _ := post(t, r.HookURL+"/Tick", r.Token, nil)
		limited = code == http.StatusTooManyRequests
	}
	if !limited {
		t.Fatal("never rate-limited")
	}
	f.reg.Unregister("L1", "unregistered")
	if code, _ := post(t, r.HookURL+"/Stop", r.Token, nil); code != http.StatusGone {
		t.Fatalf("410 after unregister: %d", code)
	}
	if code, _ := post(t, f.url+"/hook/never/Stop", r.Token, nil); code != http.StatusGone {
		t.Fatalf("410 for an unknown launch: %d", code)
	}
}

func TestHeldPostGetsTheReply(t *testing.T) {
	f := start(t)
	r, _ := f.reg.Register(Spec{ID: "L1", TerminalID: "t1"})
	type result struct {
		code int
		body string
	}
	done := make(chan result, 1)
	go func() {
		code, body, _ := postErr(r.HookURL+"/PermissionRequest?wait_ms=10000", r.Token, []byte(`{"tool_name":"Bash"}`))
		done <- result{code, body}
	}()
	ev := f.rec.wait(t, "hook", 1)[0]
	replyID, _ := ev["reply_id"].(string)
	if replyID == "" || ev["hold_ms"] != int64(10000) {
		t.Fatalf("%v", ev)
	}
	// A reply naming another launch does not reach it.
	if err := f.reg.Reply("other", replyID, Reply{Status: 200}); err != ErrNoReply {
		t.Fatalf("a reply across launches: %v", err)
	}
	if err := f.reg.Reply("L1", replyID, Reply{Status: 200, ContentType: "application/json", Body: []byte(`{"decision":"allow"}`)}); err != nil {
		t.Fatal(err)
	}
	got := <-done
	if got.code != 200 || got.body != `{"decision":"allow"}` {
		t.Fatalf("%+v", got)
	}
	if err := f.reg.Reply("L1", replyID, Reply{Status: 200}); err != ErrNoReply {
		t.Fatalf("a second reply: %v", err)
	}
	if f.reg.Held() != 0 {
		t.Fatalf("%d still held", f.reg.Held())
	}
}

func TestHeldPostTimesOutEmpty(t *testing.T) {
	f := start(t)
	r, _ := f.reg.Register(Spec{ID: "L1", HoldMax: 300 * time.Millisecond})
	start := time.Now()
	// The body's own field asks for a minute; the launch's ceiling makes it 300 ms.
	code, body := post(t, r.HookURL+"/team", r.Token, []byte(`{"tool":"ask","daedalus_hold_ms":60000}`))
	if code != http.StatusNoContent || body != "" {
		t.Fatalf("%d %q", code, body)
	}
	if took := time.Since(start); took < 250*time.Millisecond || took > 5*time.Second {
		t.Fatalf("held %s", took)
	}
	ev := f.rec.wait(t, "hook", 1)[0]
	if err := f.reg.Reply("L1", ev["reply_id"].(string), Reply{Status: 200}); err != ErrNoReply {
		t.Fatalf("a reply after the hold: %v", err)
	}
	if f.reg.Held() != 0 {
		t.Fatalf("%d still held", f.reg.Held())
	}
}

// A CLI whose own timer on a tool call is shorter than the hold gives up on the post and closes the
// connection. The answer that comes later must be refused, not swallowed: the host then sends it as
// a message, and the question is still answered exactly once.
func TestAHeldPostWhoseCallerGaveUpTakesNoReply(t *testing.T) {
	f := start(t)
	r, _ := f.reg.Register(Spec{ID: "L1"})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, r.HookURL+"/team?wait_ms=60000", strings.NewReader(`{"tool":"ask"}`))
		req.Header.Set("Authorization", "Bearer "+r.Token)
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			resp.Body.Close()
		}
		done <- err
	}()
	ev := f.rec.wait(t, "hook", 1)[0]
	cancel()
	if err := <-done; err == nil {
		t.Fatal("the cancelled post came back with an answer")
	}
	deadline := time.Now().Add(5 * time.Second)
	for f.reg.Held() != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("%d still held after its caller left", f.reg.Held())
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := f.reg.Reply("L1", ev["reply_id"].(string), Reply{Status: 200, Body: []byte(`{"text":"left"}`)}); err != ErrNoReply {
		t.Fatalf("a reply to a post nobody waits on: %v", err)
	}
}

func TestHeldPostEndsWithItsLaunch(t *testing.T) {
	f := start(t)
	r, _ := f.reg.Register(Spec{ID: "L1"})
	done := make(chan int, 1)
	go func() {
		code, _, _ := postErr(r.HookURL+"/ask?wait_ms=60000", r.Token, nil)
		done <- code
	}()
	f.rec.wait(t, "hook", 1)
	f.reg.Unregister("L1", "unregistered")
	select {
	case code := <-done:
		if code != http.StatusGone {
			t.Fatalf("%d", code)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the held post outlived its launch")
	}
}

func TestLaunchEndsAfterItsTerminals(t *testing.T) {
	f := start(t)
	f.reg.Grace = 100 * time.Millisecond
	r, _ := f.reg.Register(Spec{ID: "L1"})
	if !f.reg.Bind("L1", "t1") || !f.reg.Bind("L1", "t2") {
		t.Fatal("bind")
	}
	l, _ := f.reg.Get("L1")
	if l.Primary() != "t1" {
		t.Fatalf("primary %q", l.Primary())
	}
	f.reg.TerminalExited("L1", "t1")
	time.Sleep(300 * time.Millisecond)
	if _, ok := f.reg.Get("L1"); !ok {
		t.Fatal("ended while a terminal still ran")
	}
	f.reg.TerminalExited("L1", "t2")
	// Within the grace, a CLI's last hooks still land.
	if code, _ := post(t, r.HookURL+"/SessionEnd", r.Token, nil); code != http.StatusNoContent {
		t.Fatalf("within the grace: %d", code)
	}
	ended := f.rec.wait(t, "launch.ended", 1)
	if ended[0]["reason"] != "terminal_exited" {
		t.Fatalf("%v", ended)
	}
	if code, _ := post(t, r.HookURL+"/Stop", r.Token, nil); code != http.StatusGone {
		t.Fatalf("after the grace: %d", code)
	}
}

func TestUnboundLaunchExpires(t *testing.T) {
	f := start(t)
	if _, err := f.reg.Register(Spec{ID: "L1", TTL: 100 * time.Millisecond}); err != nil {
		t.Fatal(err)
	}
	if ended := f.rec.wait(t, "launch.ended", 1); ended[0]["reason"] != "expired" {
		t.Fatalf("%v", ended)
	}
	// A bound one does not.
	f.reg.Register(Spec{ID: "L2", TTL: 100 * time.Millisecond})
	f.reg.Bind("L2", "t")
	time.Sleep(300 * time.Millisecond)
	if _, ok := f.reg.Get("L2"); !ok {
		t.Fatal("a bound launch expired")
	}
}

func TestBigBodiesKeepTheirKeys(t *testing.T) {
	huge := strings.Repeat("x", 900<<10)
	raw, _ := json.Marshal(map[string]any{"tool_use_id": "toolu_1", "hook_event_name": "PostToolUse", "tool_response": map[string]any{"stdout": huge}})
	body, size, truncated := eventBody(raw, config.HookEventBody)
	if !truncated || size > config.HookEventBody {
		t.Fatalf("%d bytes, truncated %v", size, truncated)
	}
	var back map[string]any
	if err := json.Unmarshal(body.(json.RawMessage), &back); err != nil {
		t.Fatal(err)
	}
	if back["tool_use_id"] != "toolu_1" || back["hook_event_name"] != "PostToolUse" {
		t.Fatalf("%v", back)
	}
	// Text is text, and bad UTF-8 is replaced.
	b, _, tr := eventBody([]byte("plain \xff text"), 1000)
	if b != "plain � text" || tr {
		t.Fatalf("%q %v", b, tr)
	}
	// A structure too big for any shortening says so rather than carrying half of itself.
	many := "[" + strings.Repeat("1,", 200000) + "1]"
	if b, _, tr := eventBody([]byte(many), 1000); b != nil || !tr {
		t.Fatalf("%v %v", b, tr)
	}
}

func TestHookPostCommand(t *testing.T) {
	f := start(t)
	r, _ := f.reg.Register(Spec{ID: "L1"})
	run := func(env map[string]string, args ...string) (int, string, string) {
		var out, errOut bytes.Buffer
		code := HookPost(args, func(k string) string { return env[k] }, strings.NewReader(`{"x":1}`), &out, &errOut)
		return code, out.String(), errOut.String()
	}
	env := map[string]string{"DAEDALUS_HOOK_URL": r.HookURL, "DAEDALUS_HOOK_TOKEN": r.Token}
	if code, out, _ := run(env, "Stop"); code != 0 || out != "" {
		t.Fatalf("%d %q", code, out)
	}
	// Held, answered by the host: the reply is printed.
	go func() {
		for len(f.rec.of("hook")) < 2 {
			time.Sleep(5 * time.Millisecond)
		}
		ev := f.rec.of("hook")[1]
		f.reg.Reply("L1", ev["reply_id"].(string), Reply{Status: 200, Body: []byte("answer")})
	}()
	if code, out, _ := run(env, "ask", "--wait-ms", "10000"); code != 0 || out != "answer" {
		t.Fatalf("%d %q", code, out)
	}
	bad := map[string]string{"DAEDALUS_HOOK_URL": r.HookURL, "DAEDALUS_HOOK_TOKEN": "wrong"}
	if code, out, errOut := run(bad, "Stop"); code != 2 || out != "" || !strings.Contains(errOut, "401") {
		t.Fatalf("401: %d %q %q", code, out, errOut)
	}
	if code, _, _ := run(nil, "Stop"); code != 1 {
		t.Fatalf("no environment: %d", code)
	}
	if code, _, _ := run(env); code != 1 {
		t.Fatalf("no name: %d", code)
	}
	f.reg.Unregister("L1", "unregistered")
	if code, _, _ := run(env, "Stop"); code != 2 {
		t.Fatalf("410: %d", code)
	}
	// Nothing listening at all is neither success nor "your launch is over".
	dead := map[string]string{"DAEDALUS_HOOK_URL": "http://127.0.0.1:1/hook/L1", "DAEDALUS_HOOK_TOKEN": "x"}
	if code, _, _ := run(dead, "Stop"); code != 1 {
		t.Fatalf("no listener: %d", code)
	}
}

func TestBucket(t *testing.T) {
	now := time.Unix(0, 0)
	b := newBucket(10, 2)
	b.now = func() time.Time { return now }
	if !b.allow() || !b.allow() || b.allow() {
		t.Fatal("burst")
	}
	now = now.Add(100 * time.Millisecond)
	if !b.allow() || b.allow() {
		t.Fatal("refill")
	}
}
