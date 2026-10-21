package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeDeskd speaks deskd's framing and handshake on a loopback port or a unix socket, with the
// operator's token in the state directory as the daemon leaves it, and answers each method with
// what the test gives it.
type fakeDeskd struct {
	t        *testing.T
	listener net.Listener
	token    string
	answer   func(method string, params json.RawMessage) (any, *deskdError)

	mu    sync.Mutex
	calls []fakeCall
	conns int
}

type fakeCall struct {
	method string
	params string
}

const fakeOperatorToken = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func startFakeDeskd(t *testing.T, paths Paths, network string, answer func(string, json.RawMessage) (any, *deskdError)) *fakeDeskd {
	t.Helper()
	run := deskdRunDir(paths)
	if err := os.MkdirAll(run, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(deskdStateDir(paths), 0o700); err != nil {
		t.Fatal(err)
	}
	var listener net.Listener
	var endpoint string
	var err error
	if network == "unix" {
		listener, err = net.Listen("unix", filepath.Join(run, "deskd.sock"))
		endpoint = "unix:deskd.sock"
	} else {
		listener, err = net.Listen("tcp", "127.0.0.1:0")
		if err == nil {
			endpoint = "tcp:" + listener.Addr().String()
		}
	}
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(run, "endpoint"), []byte(endpoint+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(deskdOperatorToken(paths), []byte(fakeOperatorToken+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	f := &fakeDeskd{t: t, listener: listener, token: fakeOperatorToken, answer: answer}
	go f.serve()
	t.Cleanup(func() { listener.Close() })
	return f
}

func (f *fakeDeskd) serve() {
	for {
		nc, err := f.listener.Accept()
		if err != nil {
			return
		}
		go f.handle(nc)
	}
}

func (f *fakeDeskd) handle(nc net.Conn) {
	defer nc.Close()
	first, err := readDeskdFrame(nc)
	f.mu.Lock()
	token := f.token
	f.mu.Unlock()
	if err != nil || strings.TrimSpace(string(first)) != token {
		return
	}
	f.mu.Lock()
	f.conns++
	f.mu.Unlock()
	hello, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "method": "hello", "params": map[string]any{"version": "test", "protocol": 1, "instance": "i", "role": "operator"}})
	if writeDeskdFrame(nc, hello) != nil {
		return
	}
	for {
		payload, err := readDeskdFrame(nc)
		if err != nil {
			return
		}
		var req struct {
			ID     int64           `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
		}
		if json.Unmarshal(payload, &req) != nil {
			return
		}
		f.mu.Lock()
		f.calls = append(f.calls, fakeCall{req.Method, string(req.Params)})
		f.mu.Unlock()
		result, refusal := f.answer(req.Method, req.Params)
		reply := map[string]any{"jsonrpc": "2.0", "id": req.ID}
		if refusal != nil {
			reply["error"] = refusal
		} else {
			reply["result"] = result
		}
		body, _ := json.Marshal(reply)
		// An event in between, which the client must step over.
		event, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "method": "event", "params": map[string]any{"seq": 1}})
		_ = writeDeskdFrame(nc, event)
		if writeDeskdFrame(nc, body) != nil {
			return
		}
	}
}

func (f *fakeDeskd) seen() []fakeCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]fakeCall(nil), f.calls...)
}

func computerServer(t *testing.T, mode Mode) (*Server, Paths) {
	t.Helper()
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{}, mode); err != nil {
		t.Fatal(err)
	}
	if err := StoreMode(paths, mode); err != nil {
		t.Fatal(err)
	}
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { server.Stop(context.Background()) })
	return server, paths
}

type computerAnswer struct {
	status int
	body   map[string]any
	raw    string
}

func computerRequest(t *testing.T, server *Server, method, route string, token string, body string) computerAnswer {
	t.Helper()
	req, err := http.NewRequest(method, server.URL()+route, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set(csrfHeader, token)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	answer := computerAnswer{status: resp.StatusCode, raw: string(raw)}
	_ = json.Unmarshal(raw, &answer.body)
	return answer
}

var computerRoutes = []struct{ method, route, body string }{
	{http.MethodGet, "api/computer", ""},
	{http.MethodGet, "api/computer/apps", ""},
	{http.MethodPost, "api/computer/grant", `{"session":"s1","scope":{"kind":"desktop"},"ttl_ms":60000}`},
	{http.MethodPost, "api/computer/revoke", `{}`},
	{http.MethodPost, "api/computer/stop", `{}`},
	{http.MethodPost, "api/computer/resume", `{}`},
	{http.MethodPost, "api/computer/permissions", `{"which":"screen"}`},
}

// Every route, reads included, asks for the launcher's token: window titles are the operator's, and
// the loopback port is open to every page they visit. A request aimed at another name for this
// address is refused too.
func TestEveryComputerRouteAsksForTheToken(t *testing.T) {
	server, paths := computerServer(t, ModeNative)
	fake := startFakeDeskd(t, paths, "tcp", func(string, json.RawMessage) (any, *deskdError) { return map[string]any{}, nil })
	for _, r := range computerRoutes {
		if got := computerRequest(t, server, r.method, r.route, "", r.body); got.status != http.StatusForbidden {
			t.Fatalf("%s without the token answered %d", r.route, got.status)
		}
		if got := computerRequest(t, server, r.method, r.route, "not-the-token", r.body); got.status != http.StatusForbidden {
			t.Fatalf("%s with the wrong token answered %d", r.route, got.status)
		}
	}
	// The right token, aimed at a name that is not this page's.
	req, _ := http.NewRequest(http.MethodGet, server.URL()+"api/computer", nil)
	req.Host = fmt.Sprintf("evil.example:%d", server.Port())
	req.Header.Set(csrfHeader, server.csrf)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("a request for another host answered %d", resp.StatusCode)
	}
	if calls := fake.seen(); len(calls) != 0 {
		t.Fatalf("the daemon was asked %v by requests that were refused", calls)
	}
}

// Docker mode has no desktop for the agent: 501, whatever the route.
func TestComputerUseIsNativeOnly(t *testing.T) {
	server, _ := computerServer(t, ModeDocker)
	for _, r := range computerRoutes {
		got := computerRequest(t, server, r.method, r.route, server.csrf, r.body)
		if got.status != http.StatusNotImplemented {
			t.Fatalf("%s in Docker mode answered %d", r.route, got.status)
		}
		if msg, _ := got.body["error"].(map[string]any)["message"].(string); msg == "" {
			t.Fatalf("%s in Docker mode says nothing: %s", r.route, got.raw)
		}
	}
}

// No daemon: 503 with the reason in a sentence — the note the launcher left when the build carries
// none, or plainly that it is not running.
func TestNoDesktopDaemonIsA503WithTheReason(t *testing.T) {
	server, paths := computerServer(t, ModeNative)
	got := computerRequest(t, server, http.MethodGet, "api/computer", server.csrf, "")
	if got.status != http.StatusServiceUnavailable || got.body["available"] != false || got.body["reason"] != "the desktop daemon is not running" {
		t.Fatalf("no daemon answered %d %s", got.status, got.raw)
	}
	if err := markPtyd(deskdRunDir(paths), noDeskdReason); err != nil {
		t.Fatal(err)
	}
	for _, r := range computerRoutes {
		got := computerRequest(t, server, r.method, r.route, server.csrf, r.body)
		if got.status != http.StatusServiceUnavailable || got.body["reason"] != noDeskdReason {
			t.Fatalf("%s without a daemon answered %d %s", r.route, got.status, got.raw)
		}
	}
}

// A refusal from the daemon reaches the host as it was given — the code, the message and the data —
// so the host can say it in its own words.
func TestADaemonRefusalIsPassedThroughAs409(t *testing.T) {
	server, paths := computerServer(t, ModeNative)
	startFakeDeskd(t, paths, "tcp", func(method string, _ json.RawMessage) (any, *deskdError) {
		if method == "lease.grant" {
			return nil, &deskdError{Code: 1203, Message: "that is Daedalus itself", Data: json.RawMessage(`{"app":"io.github.anchor-inference.daedalus"}`)}
		}
		if method == "resume" {
			return nil, &deskdError{Code: 1208, Message: "operator only"}
		}
		return map[string]any{}, nil
	})
	got := computerRequest(t, server, http.MethodPost, "api/computer/grant", server.csrf, `{"session":"s1","scope":{"kind":"app","app":"io.github.anchor-inference.daedalus"},"ttl_ms":60000}`)
	if got.status != http.StatusConflict {
		t.Fatalf("a refused grant answered %d %s", got.status, got.raw)
	}
	refusal := got.body["error"].(map[string]any)
	if refusal["code"] != float64(1203) || refusal["message"] != "that is Daedalus itself" || refusal["data"].(map[string]any)["app"] != "io.github.anchor-inference.daedalus" {
		t.Fatalf("the refusal was changed on the way: %s", got.raw)
	}
	got = computerRequest(t, server, http.MethodPost, "api/computer/resume", server.csrf, `{}`)
	if got.status != http.StatusConflict || !strings.Contains(got.raw, `"data":null`) {
		t.Fatalf("a refusal without data answered %d %s", got.status, got.raw)
	}
}

// A grant reaches the daemon as lease.grant with exactly what the host sent, and its answer comes
// back as it was; the state is info and lease.status put together.
func TestAGrantAndTheStateReachTheDaemon(t *testing.T) {
	network := "tcp"
	if runtime.GOOS == "linux" {
		network = "unix"
	}
	paths := setupTempInstall(t)
	if network == "unix" && len(filepath.Join(deskdRunDir(paths), "deskd.sock")) > 100 {
		network = "tcp"
	}
	if err := WriteSetup(paths, Setup{}, ModeNative); err != nil {
		t.Fatal(err)
	}
	if err := StoreMode(paths, ModeNative); err != nil {
		t.Fatal(err)
	}
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())
	fake := startFakeDeskd(t, paths, network, func(method string, params json.RawMessage) (any, *deskdError) {
		switch method {
		case "lease.grant":
			return map[string]any{"lease": map[string]any{"id": "l1", "session": "s1"}}, nil
		case "info":
			return map[string]any{"version": "1.0", "protocol": 1, "platform": map[string]any{"os": "linux", "session_type": "x11", "units": "pixels"},
				"permissions": map[string]any{"screen": "not_applicable"}, "secure": map[string]any{"locked": false},
				"displays": []any{map[string]any{"id": "d1"}}, "limits": map[string]any{"max_edge": 1568},
				"lease": map[string]any{"active": false}, "human": map[string]any{"paused": false}, "pid": 7}, nil
		case "lease.status":
			return map[string]any{"lease": map[string]any{"active": true, "remaining_ms": 5000}, "human": map[string]any{"paused": true}}, nil
		case "apps.list":
			return map[string]any{"apps": []any{map[string]any{"id": "pid:9", "name": "Editor"}}}, nil
		case "permissions.request":
			return map[string]any{"which": "screen", "state": "granted"}, nil
		}
		return map[string]any{"ok": method}, nil
	})

	got := computerRequest(t, server, http.MethodPost, "api/computer/grant", server.csrf, `{"session":"s1","scope":{"kind":"window","window":"0x2a"},"ttl_ms":60000}`)
	if got.status != http.StatusOK || got.body["lease"].(map[string]any)["id"] != "l1" {
		t.Fatalf("the grant answered %d %s", got.status, got.raw)
	}
	calls := fake.seen()
	if len(calls) != 1 || calls[0].method != "lease.grant" || calls[0].params != `{"session":"s1","scope":{"kind":"window","window":"0x2a"},"ttl_ms":60000}` {
		t.Fatalf("the daemon was asked %v", calls)
	}

	got = computerRequest(t, server, http.MethodGet, "api/computer", server.csrf, "")
	if got.status != http.StatusOK || got.body["available"] != true {
		t.Fatalf("the state answered %d %s", got.status, got.raw)
	}
	if got.body["lease"].(map[string]any)["active"] != true || got.body["human"].(map[string]any)["paused"] != true {
		t.Fatalf("the lease and the pause are not lease.status's: %s", got.raw)
	}
	for _, key := range []string{"platform", "permissions", "secure", "displays", "limits", "version"} {
		if _, ok := got.body[key]; !ok {
			t.Fatalf("the state has no %s: %s", key, got.raw)
		}
	}
	if _, ok := got.body["pid"]; ok {
		t.Fatalf("the state carries what the contract does not: %s", got.raw)
	}

	got = computerRequest(t, server, http.MethodGet, "api/computer/apps", server.csrf, "")
	if got.status != http.StatusOK || len(got.body["apps"].([]any)) != 1 {
		t.Fatalf("the apps answered %d %s", got.status, got.raw)
	}
	for _, route := range []string{"stop", "resume", "revoke"} {
		if got := computerRequest(t, server, http.MethodPost, "api/computer/"+route, server.csrf, ""); got.status != http.StatusOK {
			t.Fatalf("%s answered %d %s", route, got.status, got.raw)
		}
	}
	if got := computerRequest(t, server, http.MethodPost, "api/computer/permissions", server.csrf, `{"which":"screen"}`); got.status != http.StatusOK || got.body["state"] != "granted" {
		t.Fatalf("the permission answered %d %s", got.status, got.raw)
	}
	methods := []string{}
	for _, c := range fake.seen() {
		methods = append(methods, c.method)
	}
	want := "lease.grant info lease.status apps.list stop resume lease.revoke permissions.request"
	if strings.Join(methods, " ") != want {
		t.Fatalf("the daemon was asked %v, want %s", methods, want)
	}
	// All of it on one connection: the client keeps it rather than dialling for every call.
	fake.mu.Lock()
	conns := fake.conns
	fake.mu.Unlock()
	if conns != 1 {
		t.Fatalf("%d connections for one client", conns)
	}
}

// A body the daemon would refuse is refused here first, with the field named, and never sent.
func TestABadBodyIsRefusedBeforeTheDaemonIsAsked(t *testing.T) {
	server, paths := computerServer(t, ModeNative)
	fake := startFakeDeskd(t, paths, "tcp", func(string, json.RawMessage) (any, *deskdError) { return map[string]any{}, nil })
	for _, bad := range []struct{ route, body string }{
		{"grant", `{"session":"","scope":{"kind":"desktop"}}`},
		{"grant", `{"session":"` + strings.Repeat("s", 129) + `","scope":{"kind":"desktop"}}`},
		{"grant", `{"session":"s","scope":{"kind":"everything"}}`},
		{"grant", `{"session":"s","scope":{"kind":"window"}}`},
		{"grant", `{"session":"s","scope":{"kind":"app","app":"a","window":"0x1"}}`},
		{"grant", `{"session":"s","scope":{"kind":"desktop"},"ttl_ms":999}`},
		{"grant", `{"session":"s","scope":{"kind":"desktop"},"ttl_ms":14400001}`},
		{"grant", `{"session":"s","scope":{"kind":"desktop"},"max":1}`},
		{"grant", `not json`},
		{"permissions", `{"which":"camera"}`},
		{"stop", `{"force":true}`},
		{"revoke", `{"session":1}`},
	} {
		got := computerRequest(t, server, http.MethodPost, "api/computer/"+bad.route, server.csrf, bad.body)
		if got.status != http.StatusBadRequest {
			t.Fatalf("%s %s answered %d %s", bad.route, bad.body, got.status, got.raw)
		}
	}
	if got := computerRequest(t, server, http.MethodGet, "api/computer/grant", server.csrf, ""); got.status != http.StatusMethodNotAllowed {
		t.Fatalf("a GET of the grant answered %d", got.status)
	}
	if calls := fake.seen(); len(calls) != 0 {
		t.Fatalf("the daemon was asked %v", calls)
	}
}

// The operator's token goes to the daemon in the handshake and nowhere else: not in an answer, not
// in an error, not in the launcher's log — even when the daemon refuses it.
func TestTheOperatorTokenIsNeverShown(t *testing.T) {
	server, paths := computerServer(t, ModeNative)
	fake := startFakeDeskd(t, paths, "tcp", func(string, json.RawMessage) (any, *deskdError) {
		return nil, &deskdError{Code: 1200, Message: "no lease"}
	})
	answers := []string{}
	for _, r := range computerRoutes {
		answers = append(answers, computerRequest(t, server, r.method, r.route, server.csrf, r.body).raw)
	}
	// And a daemon that no longer takes it: the token it was given has changed under the launcher.
	fake.mu.Lock()
	fake.token = strings.Repeat("f", 64)
	fake.mu.Unlock()
	server.computer().Close()
	got := computerRequest(t, server, http.MethodGet, "api/computer", server.csrf, "")
	if got.status != http.StatusServiceUnavailable || !strings.Contains(got.raw, "refused the launcher's operator token") {
		t.Fatalf("a refused token answered %d %s", got.status, got.raw)
	}
	answers = append(answers, got.raw)
	logs, _ := os.ReadFile(filepath.Join(paths.RuntimeLogs, "launcher.log"))
	for _, text := range append(answers, string(logs)) {
		if strings.Contains(text, fakeOperatorToken) {
			t.Fatalf("the operator's token was shown: %s", text)
		}
	}
}

// The daemon restarts with a new endpoint and a new token; the next call finds it without the
// launcher being told.
func TestTheClientReconnectsToARestartedDaemon(t *testing.T) {
	paths := setupTempInstall(t)
	first := startFakeDeskd(t, paths, "tcp", func(string, json.RawMessage) (any, *deskdError) { return map[string]any{"from": "first"}, nil })
	client := newDeskdOperator(paths)
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	result, err := client.Call(ctx, "info", nil)
	if err != nil || !bytes.Contains(result, []byte("first")) {
		t.Fatalf("first call: %s %v", result, err)
	}
	first.listener.Close()
	client.mu.Lock()
	client.conn.nc.Close()
	client.mu.Unlock()
	startFakeDeskd(t, paths, "tcp", func(string, json.RawMessage) (any, *deskdError) { return map[string]any{"from": "second"}, nil })
	// The call that meets the dead connection may fail; the one after it must reach the new daemon.
	deadline := time.Now().Add(5 * time.Second)
	for {
		result, err = client.Call(ctx, "info", nil)
		if err == nil && bytes.Contains(result, []byte("second")) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("never reached the restarted daemon: %s %v", result, err)
		}
		time.Sleep(20 * time.Millisecond)
	}
}
