package toolsmcp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/hooks"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
)

// recorder stands in for the daemon's event log.
type recorder struct {
	mu     sync.Mutex
	events []map[string]any
	wake   chan struct{}
}

func (r *recorder) PublishSized(typ, terminalID string, data any, size int) events.Event {
	r.mu.Lock()
	defer r.mu.Unlock()
	if typ == "hook" {
		r.events = append(r.events, data.(map[string]any))
	}
	select {
	case r.wake <- struct{}{}:
	default:
	}
	return events.Event{Type: typ}
}

func (r *recorder) hooks() []map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]map[string]any(nil), r.events...)
}

// hookN waits for the n-th hook event (1-based).
func (r *recorder) hookN(t *testing.T, n int) map[string]any {
	t.Helper()
	deadline := time.After(10 * time.Second)
	for {
		if got := r.hooks(); len(got) >= n {
			return got[n-1]
		}
		select {
		case <-r.wake:
		case <-time.After(20 * time.Millisecond):
		case <-deadline:
			t.Fatalf("waited for hook event %d", n)
		}
	}
}

type fixture struct {
	reg    *hooks.Registry
	rec    *recorder
	launch hooks.Registered
}

func listener(t *testing.T) *fixture {
	t.Helper()
	rec := &recorder{wake: make(chan struct{}, 1)}
	// The daemon's path must be a file: on Windows the hook command is a hard link to it, or a copy.
	// Not the test binary, though: Windows refuses to delete a link to a program that is running,
	// and the test's directory could not be removed. Nothing here runs the hook command.
	daemon := filepath.Join(t.TempDir(), "ptyd.exe")
	if err := os.WriteFile(daemon, []byte("stands in for the daemon"), 0o755); err != nil {
		t.Fatal(err)
	}
	reg, err := hooks.NewRegistry(t.TempDir(), daemon, rec, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	srv, _, err := reg.Listen("127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		reg.Close()
		srv.Close()
	})
	l, err := reg.Register(hooks.Spec{ID: "L1", TerminalID: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	return &fixture{reg: reg, rec: rec, launch: l}
}

func (f *fixture) env(extra ...string) map[string]string {
	env := map[string]string{"DAEDALUS_HOOK_URL": f.launch.HookURL, "DAEDALUS_HOOK_TOKEN": f.launch.Token}
	for i := 0; i+1 < len(extra); i += 2 {
		env[extra[i]] = extra[i+1]
	}
	return env
}

func (f *fixture) reply(t *testing.T, ev map[string]any, body string) {
	t.Helper()
	id, _ := ev["reply_id"].(string)
	if id == "" {
		t.Fatalf("the post was not held: %v", ev)
	}
	if err := f.reg.Reply("L1", id, hooks.Reply{Status: 200, ContentType: "application/json", Body: []byte(body)}); err != nil {
		t.Fatal(err)
	}
}

// client drives Serve the way a CLI does: one JSON object per line each way.
type client struct {
	t      *testing.T
	in     *io.PipeWriter
	mu     sync.Mutex
	got    []map[string]any
	wake   chan struct{}
	done   chan error
	stderr *syncBuffer
}

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func serve(t *testing.T, env map[string]string) *client {
	t.Helper()
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	c := &client{t: t, in: inW, wake: make(chan struct{}, 1), done: make(chan error, 1), stderr: &syncBuffer{}}
	go func() {
		c.done <- Serve(context.Background(), func(k string) string { return env[k] }, inR, outW, c.stderr)
		outW.Close()
	}()
	go func() {
		sc := bufio.NewScanner(outR)
		sc.Buffer(nil, 8<<20)
		for sc.Scan() {
			var m map[string]any
			if err := json.Unmarshal(sc.Bytes(), &m); err != nil {
				panic(fmt.Sprintf("the server wrote %q", sc.Text()))
			}
			c.mu.Lock()
			c.got = append(c.got, m)
			c.mu.Unlock()
			select {
			case c.wake <- struct{}{}:
			default:
			}
		}
	}()
	t.Cleanup(func() { inW.Close() })
	return c
}

func (c *client) send(v any) {
	c.t.Helper()
	b, _ := json.Marshal(v)
	if _, err := c.in.Write(append(b, '\n')); err != nil {
		c.t.Fatal(err)
	}
}

func (c *client) request(id any, method string, params any) {
	c.t.Helper()
	c.send(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
}

// await is the response with this id (JSON numbers decode as float64).
func (c *client) await(id any) map[string]any {
	c.t.Helper()
	deadline := time.After(10 * time.Second)
	for {
		c.mu.Lock()
		for _, m := range c.got {
			if fmt.Sprint(m["id"]) == fmt.Sprint(id) {
				c.mu.Unlock()
				return m
			}
		}
		c.mu.Unlock()
		select {
		case <-c.wake:
		case <-time.After(20 * time.Millisecond):
		case <-deadline:
			c.t.Fatalf("no response to %v; have %v", id, c.got)
		}
	}
}

func (c *client) has(id any) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, m := range c.got {
		if fmt.Sprint(m["id"]) == fmt.Sprint(id) {
			return true
		}
	}
	return false
}

func (c *client) call(id any, tool string, args any) {
	c.t.Helper()
	c.request(id, "tools/call", map[string]any{"name": tool, "arguments": args})
}

// result is a tools/call's text and whether it is an error.
func (c *client) result(id any) (string, bool) {
	c.t.Helper()
	m := c.await(id)
	r, ok := m["result"].(map[string]any)
	if !ok {
		c.t.Fatalf("not a result: %v", m)
	}
	content := r["content"].([]any)[0].(map[string]any)
	return content["text"].(string), r["isError"].(bool)
}

// asMap is a hook event's body as a map, whether the recorder kept it raw or decoded.
func asMap(body any) map[string]any {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil
	}
	var m map[string]any
	_ = json.Unmarshal(raw, &m)
	return m
}

var callIDShape = regexp.MustCompile(`^(L1|nolaunch):[0-9a-f]{8}:[0-9]+$`)

// withoutCallID is a posted body as JSON without its call id, which is checked for its shape: the
// launch, this process, the call's number.
func withoutCallID(t *testing.T, body any) string {
	t.Helper()
	m := asMap(body)
	id, _ := m["call_id"].(string)
	if !callIDShape.MatchString(id) {
		t.Fatalf("call id %q in %v", id, body)
	}
	rest := map[string]any{}
	for k, v := range m {
		if k != "call_id" {
			rest[k] = v
		}
	}
	b, _ := json.Marshal(rest)
	return string(b)
}

func errorCode(m map[string]any) float64 {
	e, _ := m["error"].(map[string]any)
	code, _ := e["code"].(float64)
	return code
}

func TestInitializeListAndProtocol(t *testing.T) {
	f := listener(t)
	c := serve(t, f.env())
	c.request(1, "initialize", map[string]any{"protocolVersion": "2025-06-18", "capabilities": map[string]any{},
		"clientInfo": map[string]any{"name": "claude-code", "version": "2.1.281"}})
	init := c.await(1)["result"].(map[string]any)
	if init["protocolVersion"] != "2025-06-18" || init["serverInfo"].(map[string]any)["name"] != "daedalus_team" {
		t.Fatalf("%v", init)
	}
	if _, ok := init["capabilities"].(map[string]any)["tools"]; !ok {
		t.Fatalf("no tools capability: %v", init)
	}
	c.send(map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"})

	// A revision it does not know is answered with the newest it does, and logged.
	c.request(2, "initialize", map[string]any{"protocolVersion": "2031-01-01", "clientInfo": map[string]any{"name": "future"}})
	if v := c.await(2)["result"].(map[string]any)["protocolVersion"]; v != knownVersions[0] {
		t.Fatalf("answered %v", v)
	}
	if !strings.Contains(c.stderr.String(), `"2031-01-01"`) {
		t.Fatalf("not logged: %q", c.stderr.String())
	}

	c.request("list", "tools/list", map[string]any{})
	tools := c.await("list")["result"].(map[string]any)["tools"].([]any)
	names := map[string][]any{}
	for _, raw := range tools {
		tl := raw.(map[string]any)
		schema := tl["inputSchema"].(map[string]any)
		names[tl["name"].(string)] = schema["required"].([]any)
	}
	if fmt.Sprint(names["Report"]) != "[kind note]" || fmt.Sprint(names["AskOrchestrator"]) != "[question]" || len(names) != 2 {
		t.Fatalf("%v", names)
	}
	if desc := tools[1].(map[string]any)["description"].(string); !strings.Contains(desc, "up to 5 minutes") {
		t.Fatalf("the ask's description does not say how long it waits: %q", desc)
	}

	c.request(3, "ping", nil)
	if r := c.await(3); r["result"] == nil || r["error"] != nil {
		t.Fatalf("%v", r)
	}
	c.request(4, "resources/list", nil)
	if code := errorCode(c.await(4)); code != codeNoMethod {
		t.Fatalf("an unknown method: %v", code)
	}
	c.call(5, "Exec", map[string]any{})
	if code := errorCode(c.await(5)); code != codeInvalidParams {
		t.Fatalf("an unknown tool: %v", code)
	}
	if _, err := c.in.Write([]byte("not json\n[{\"id\":9}]\n")); err != nil {
		t.Fatal(err)
	}
	c.request(6, "ping", nil)
	c.await(6)
	var codes []float64
	c.mu.Lock()
	for _, m := range c.got {
		if m["id"] == nil {
			codes = append(codes, errorCode(m))
		}
	}
	c.mu.Unlock()
	if fmt.Sprint(codes) != fmt.Sprint([]float64{codeParse, codeInvalidRequest}) {
		t.Fatalf("errors for unreadable lines: %v", codes)
	}
	// Loading the tools is announced — twice initialized, once listed — and nothing else is posted.
	var stages []string
	for i := 1; i <= 3; i++ {
		ev := f.rec.hookN(t, i)
		body := asMap(ev["body"])
		if ev["name"] != "team" || body["tool"] != "hello" || ev["reply_id"] != nil {
			t.Fatalf("an announcement: %v", ev)
		}
		stages = append(stages, body["stage"].(string))
		if body["stage"] == "initialize" && body["client"].(map[string]any)["name"] == "claude-code" && body["client"].(map[string]any)["version"] != "2.1.281" {
			t.Fatalf("the client is not named: %v", body)
		}
	}
	sort.Strings(stages)
	if fmt.Sprint(stages) != "[initialize initialize tools/list]" || len(f.rec.hooks()) != 3 {
		t.Fatalf("announcements %v, hooks %v", stages, f.rec.hooks())
	}

	c.in.Close()
	select {
	case err := <-c.done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the server did not end with its input")
	}
}

func TestReportRoundTrip(t *testing.T) {
	f := listener(t)
	c := serve(t, f.env("DAEDALUS_REPORT_HOLD_MS", "10000"))
	c.call(1, "Report", map[string]any{"kind": "done", "note": "the task is finished", "artifacts": []string{"README.md"}, "remember": "tests live in tests/"})
	ev := f.rec.hookN(t, 1)
	if ev["name"] != "team" || ev["terminal_id"] != "t1" || ev["hold_ms"] != int64(10000) {
		t.Fatalf("%v", ev)
	}
	want := `{"artifacts":["README.md"],"kind":"done","note":"the task is finished","remember":"tests live in tests/","tool":"report"}`
	if got := withoutCallID(t, ev["body"]); got != want {
		t.Fatalf("body %s", got)
	}
	first := asMap(ev["body"])["call_id"]
	// The host refuses "done" with a dirty worktree: the worker reads why.
	f.reply(t, ev, `{"text":"the worktree has uncommitted changes; commit them and report again","error":true}`)
	if text, isErr := c.result(1); !isErr || !strings.Contains(text, "commit them") {
		t.Fatalf("%q %v", text, isErr)
	}
	c.call(2, "Report", map[string]any{"kind": "checkpoint", "note": "halfway"})
	ev = f.rec.hookN(t, 2)
	if got := withoutCallID(t, ev["body"]); got != `{"artifacts":[],"kind":"checkpoint","note":"halfway","tool":"report"}` {
		t.Fatalf("optional fields: %s", got)
	}
	if asMap(ev["body"])["call_id"] == first {
		t.Fatal("two calls share an id")
	}
	f.reply(t, ev, `{"text":"recorded"}`)
	if text, isErr := c.result(2); isErr || text != "recorded" {
		t.Fatalf("%q %v", text, isErr)
	}
}

func TestReportCarriesEvidenceAndAcknowledgements(t *testing.T) {
	f := listener(t)
	c := serve(t, f.env("DAEDALUS_REPORT_HOLD_MS", "10000"))
	c.call(1, "Report", map[string]any{
		"kind": "done", "note": "script.md written",
		"evidence":     []map[string]any{{"item": "C1", "how": "read aloud with a timer", "result": "52 s"}},
		"acknowledged": []string{"R2"},
	})
	ev := f.rec.hookN(t, 1)
	want := `{"acknowledged":["R2"],"artifacts":[],"evidence":[{"how":"read aloud with a timer","item":"C1","result":"52 s"}],"kind":"done","note":"script.md written","tool":"report"}`
	if got := withoutCallID(t, ev["body"]); got != want {
		t.Fatalf("body %s", got)
	}
	f.reply(t, ev, `{"text":"reported done"}`)
	if text, isErr := c.result(1); isErr || text != "reported done" {
		t.Fatalf("%q %v", text, isErr)
	}
	// Evidence of the wrong shape is refused with what it must be, before anything reaches the host.
	c.call(2, "Report", map[string]any{"kind": "done", "note": "x", "evidence": "C1 passed"})
	if text, isErr := c.result(2); !isErr || !strings.Contains(text, "evidence must be a list of objects with item, how and result") {
		t.Fatalf("%q %v", text, isErr)
	}
}

func TestReportWithASilentHostIsRecorded(t *testing.T) {
	f := listener(t)
	c := serve(t, f.env("DAEDALUS_REPORT_HOLD_MS", "150"))
	c.call(1, "Report", map[string]any{"kind": "stuck", "note": "the registry is down"})
	if text, isErr := c.result(1); isErr || text != reportRecorded {
		t.Fatalf("%q %v", text, isErr)
	}
	if len(f.rec.hooks()) != 1 {
		t.Fatal("the report was not published")
	}
}

func TestAskHeldAnsweredAndExpired(t *testing.T) {
	f := listener(t)
	c := serve(t, f.env("DAEDALUS_ASK_HOLD_MS", "20000"))
	c.call("a", "AskOrchestrator", map[string]any{"question": "Which branch?", "options": []string{"main", "dev"}, "context": "both build"})
	ev := f.rec.hookN(t, 1)
	if ev["hold_ms"] != int64(20000) {
		t.Fatalf("%v", ev)
	}
	if got := withoutCallID(t, ev["body"]); got != `{"context":"both build","options":["main","dev"],"question":"Which branch?","tool":"ask"}` {
		t.Fatalf("body %s", got)
	}
	// While the question waits, the server still answers: a ping, and a second call.
	c.request("p", "ping", nil)
	c.await("p")
	c.call("b", "Report", map[string]any{"kind": "checkpoint", "note": "waiting on a branch"})
	f.reply(t, f.rec.hookN(t, 2), `"noted"`)
	if text, _ := c.result("b"); text != "noted" {
		t.Fatalf("a JSON string reply: %q", text)
	}
	if c.has("a") {
		t.Fatal("the question was answered before the host replied")
	}
	f.reply(t, ev, `{"text":"dev"}`)
	if text, isErr := c.result("a"); isErr || text != "dev" {
		t.Fatalf("%q %v", text, isErr)
	}

	short := serve(t, f.env("DAEDALUS_ASK_HOLD_MS", "150"))
	short.call(1, "AskOrchestrator", map[string]any{"question": "Anyone there?"})
	if text, isErr := short.result(1); isErr || text != askExpired {
		t.Fatalf("expired: %q %v", text, isErr)
	}
}

func TestCancelledAskReleasesItsHold(t *testing.T) {
	f := listener(t)
	c := serve(t, f.env())
	c.call(7, "AskOrchestrator", map[string]any{"question": "Deploy now?"})
	f.rec.hookN(t, 1)
	if f.reg.Held() != 1 {
		t.Fatalf("%d held", f.reg.Held())
	}
	c.send(map[string]any{"jsonrpc": "2.0", "method": "notifications/cancelled", "params": map[string]any{"requestId": 7, "reason": "user pressed Esc"}})
	deadline := time.Now().Add(5 * time.Second)
	for f.reg.Held() != 0 {
		if time.Now().After(deadline) {
			t.Fatal("the held post outlived its cancelled call")
		}
		time.Sleep(10 * time.Millisecond)
	}
	c.request(8, "ping", nil)
	c.await(8)
	if c.has(7) {
		t.Fatal("a cancelled call was answered")
	}
}

func TestArgumentsAreChecked(t *testing.T) {
	f := listener(t)
	c := serve(t, f.env())
	cases := []struct {
		tool string
		args any
		says string
	}{
		{"Report", map[string]any{"kind": "finished", "note": "x"}, "kind must be one of checkpoint, needs_input, stuck, done"},
		{"Report", map[string]any{"kind": "done"}, "note is empty"},
		{"Report", map[string]any{"kind": "done", "note": "x", "artifacts": "a.txt"}, "artifacts must be a list of strings"},
		{"Report", "done", "must be an object"},
		{"AskOrchestrator", map[string]any{"question": "  "}, "question is empty"},
		{"AskOrchestrator", map[string]any{"question": "q", "options": []int{1}}, "options must be a list of strings"},
		{"AskOrchestrator", map[string]any{"question": strings.Repeat("x", 1<<20)}, "larger than a megabyte"},
	}
	for i, tc := range cases {
		c.call(i, tc.tool, tc.args)
		if text, isErr := c.result(i); !isErr || !strings.Contains(text, tc.says) {
			t.Errorf("%s %v: %q %v", tc.tool, tc.args, text, isErr)
		}
	}
	if len(f.rec.hooks()) != 0 {
		t.Fatal("a refused call was posted")
	}
}

func TestAnEndedLaunchIsSaidSo(t *testing.T) {
	f := listener(t)
	c := serve(t, f.env())
	f.reg.Unregister("L1", "ended")
	c.call(1, "Report", map[string]any{"kind": "done", "note": "x"})
	if text, isErr := c.result(1); !isErr || !strings.Contains(text, "no longer connected") {
		t.Fatalf("%q %v", text, isErr)
	}
	wrong := serve(t, map[string]string{"DAEDALUS_HOOK_URL": f.launch.HookURL, "DAEDALUS_HOOK_TOKEN": "forged"})
	wrong.call(1, "Report", map[string]any{"kind": "done", "note": "x"})
	if _, isErr := wrong.result(1); !isErr {
		t.Fatal("a wrong token was accepted")
	}
	dead := serve(t, map[string]string{"DAEDALUS_HOOK_URL": "http://127.0.0.1:1/hook/L1", "DAEDALUS_HOOK_TOKEN": "x"})
	dead.call(1, "Report", map[string]any{"kind": "done", "note": "x"})
	if text, isErr := dead.result(1); !isErr || !strings.Contains(text, "could not be reached") || strings.Contains(text, "127.0.0.1") {
		t.Fatalf("nothing listening: %q %v", text, isErr)
	}
}

func TestWithoutALaunchTheToolsStillList(t *testing.T) {
	c := serve(t, map[string]string{})
	c.request(1, "tools/list", nil)
	if n := len(c.await(1)["result"].(map[string]any)["tools"].([]any)); n != 2 {
		t.Fatalf("%d tools", n)
	}
	c.call(2, "Report", map[string]any{"kind": "done", "note": "x"})
	if text, isErr := c.result(2); !isErr || !strings.Contains(text, "not a launch") {
		t.Fatalf("%q %v", text, isErr)
	}
}

func TestOversizedLineIsSkipped(t *testing.T) {
	f := listener(t)
	c := serve(t, f.env())
	go func() {
		c.in.Write(bytes.Repeat([]byte("x"), maxLine+10))
		c.in.Write([]byte("\n"))
		b, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": "ping"})
		c.in.Write(append(b, '\n'))
	}()
	c.await(1)
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.got) != 2 || errorCode(c.got[0]) != codeInvalidRequest {
		t.Fatalf("%v", c.got)
	}
}

func TestTheHostsOwnTeamRoute(t *testing.T) {
	var mu sync.Mutex
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		seen = append(seen, r.URL.Path+" "+r.Header.Get("X-Daedalus-Team-Token")+" "+string(b))
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Header.Get("X-Daedalus-Team-Token") != "tok":
			w.WriteHeader(401)
			w.Write([]byte(`{"detail":"wrong token"}`))
		case strings.HasSuffix(r.URL.Path, "/ask"):
			w.Write([]byte(`{"ask_id":"a1","short_id":"q7k2m","text":"Asked. End your turn: the answer arrives as a message."}`))
		case strings.Contains(string(b), `"done"`):
			w.WriteHeader(400)
			w.Write([]byte(`{"detail":"the worktree has uncommitted changes"}`))
		default:
			w.Write([]byte(`{"ok":true,"text":"recorded"}`))
		}
	}))
	defer srv.Close()
	base := srv.URL + "/api/team/s1"
	// Both routes are named; the host's own wins, and the listener's variables are not used.
	c := serve(t, map[string]string{"DAEDALUS_TEAM_URL": base, "DAEDALUS_TEAM_TOKEN": "tok",
		"DAEDALUS_HOOK_URL": "http://127.0.0.1:1/hook/L1", "DAEDALUS_HOOK_TOKEN": "x"})
	c.call(1, "Report", map[string]any{"kind": "checkpoint", "note": "halfway"})
	if text, isErr := c.result(1); isErr || text != "recorded" {
		t.Fatalf("%q %v", text, isErr)
	}
	c.call(2, "AskOrchestrator", map[string]any{"question": "Which branch?"})
	if text, isErr := c.result(2); isErr || !strings.Contains(text, "arrives as a message") {
		t.Fatalf("%q %v", text, isErr)
	}
	c.call(3, "Report", map[string]any{"kind": "done", "note": "finished"})
	if text, isErr := c.result(3); !isErr || !strings.Contains(text, "uncommitted changes") {
		t.Fatalf("%q %v", text, isErr)
	}
	mu.Lock()
	first := seen[0]
	mu.Unlock()
	path, body, _ := strings.Cut(strings.TrimPrefix(first, "/api/team/s1/report tok "), "{")
	var posted map[string]any
	if path != "" || json.Unmarshal([]byte("{"+body), &posted) != nil || withoutCallID(t, posted) != `{"artifacts":[],"kind":"checkpoint","note":"halfway"}` {
		t.Fatalf("%s", first)
	}

	bad := serve(t, map[string]string{"DAEDALUS_TEAM_URL": base})
	bad.call(1, "Report", map[string]any{"kind": "checkpoint", "note": "x"})
	if text, isErr := bad.result(1); !isErr || !strings.Contains(text, "DAEDALUS_TEAM_TOKEN") {
		t.Fatalf("%q %v", text, isErr)
	}
}
