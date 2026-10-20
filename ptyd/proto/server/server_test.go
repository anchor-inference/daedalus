//go:build unix

package server_test

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// testDaemon names the daemon the tests prepare run directories for.
const testDaemon = "ptyd"

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// runDir is a short directory: unix socket paths are limited to about a hundred bytes, and a test's
// temporary directory can be longer than that on its own.
func runDir(t *testing.T) string {
	t.Helper()
	d, err := os.MkdirTemp("", "ptyd")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(d) })
	return filepath.Join(d, "run")
}

func start(t *testing.T, dir string) (*server.Endpoint, *server.Server) {
	t.Helper()
	ep, err := server.Prepare(dir, "unix", testDaemon)
	if err != nil {
		t.Fatal(err)
	}
	srv := server.New(ep.Token, quiet(), func() any { return map[string]any{"instance": "abc", "protocol": 1} })
	srv.Handle("echo", func(ctx context.Context, c *server.Conn, p json.RawMessage) (any, error) {
		return json.RawMessage(p), nil
	})
	srv.Handle("fail", func(ctx context.Context, c *server.Conn, p json.RawMessage) (any, error) {
		return nil, wire.Errorf(wire.CodeNotFound, "nothing here")
	})
	srv.Handle("boom", func(ctx context.Context, c *server.Conn, p json.RawMessage) (any, error) {
		panic("a bug")
	})
	srv.Handle("then", func(ctx context.Context, c *server.Conn, p json.RawMessage) (any, error) {
		return server.Then{Result: "first", After: func() { _ = c.Notify("second", nil) }}, nil
	})
	go srv.Serve(ep.Listener)
	t.Cleanup(func() {
		ep.Listener.Close()
		srv.Close()
		ep.Release()
	})
	return ep, srv
}

func TestHandshakeAndCalls(t *testing.T) {
	dir := runDir(t)
	start(t, dir)
	for name, mode := range map[string]os.FileMode{"": 0o700, server.TokenFile: 0o600, server.SocketName(testDaemon): 0o600} {
		st, err := os.Stat(filepath.Join(dir, name))
		if err != nil {
			t.Fatal(err)
		}
		if st.Mode().Perm() != mode {
			t.Errorf("%q has mode %o, want %o", name, st.Mode().Perm(), mode)
		}
	}
	ep, _ := os.ReadFile(filepath.Join(dir, server.EndpointFile))
	if strings.TrimSpace(string(ep)) != "unix:ptyd.sock" {
		t.Fatalf("endpoint %q", ep)
	}
	c, err := clienttest.Dial(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	var hello map[string]any
	_ = json.Unmarshal(c.Hello, &hello)
	if hello["instance"] != "abc" {
		t.Fatalf("hello %s", c.Hello)
	}
	ctx := context.Background()
	var got map[string]int
	if err := c.Call(ctx, "echo", map[string]int{"a": 1}, &got); err != nil || got["a"] != 1 {
		t.Fatalf("echo: %v %v", got, err)
	}
	var we *wire.Error
	if err := c.Call(ctx, "fail", nil, nil); !errors.As(err, &we) || we.Code != wire.CodeNotFound {
		t.Fatalf("fail: %v", err)
	}
	if err := c.Call(ctx, "nope", nil, nil); !errors.As(err, &we) || we.Code != wire.CodeMethodNotFound {
		t.Fatalf("unknown method: %v", err)
	}
	if err := c.Call(ctx, "boom", nil, nil); !errors.As(err, &we) || we.Code != wire.CodeInternal {
		t.Fatalf("panic: %v", err)
	}
	// The connection survived the panic.
	if err := c.Call(ctx, "echo", 1, nil); err != nil {
		t.Fatal(err)
	}
	var first string
	if err := c.Call(ctx, "then", nil, &first); err != nil || first != "first" {
		t.Fatalf("then: %q %v", first, err)
	}
	select {
	case n := <-c.Notes:
		if n.Method != "second" {
			t.Fatalf("after the reply: %+v", n)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the follow-up never came")
	}
}

func TestWrongTokenClosesAfterADelay(t *testing.T) {
	dir := runDir(t)
	start(t, dir)
	began := time.Now()
	_, err := clienttest.DialWith("unix", filepath.Join(dir, server.SocketName(testDaemon)), strings.Repeat("0", 64))
	if err == nil {
		t.Fatal("a wrong token was accepted")
	}
	if d := time.Since(began); d < 900*time.Millisecond {
		t.Fatalf("closed after %s, want about a second", d)
	}
}

func TestSecondInstanceRefusesAndStaleSocketIsRemoved(t *testing.T) {
	dir := runDir(t)
	start(t, dir)
	if _, err := server.Prepare(dir, "unix", testDaemon); !errors.Is(err, server.ErrHeld) {
		t.Fatalf("second instance: %v", err)
	}

	// A socket file left by a daemon that died: nothing listens on it and nothing holds the lock.
	stale := runDir(t)
	if err := os.MkdirAll(stale, 0o700); err != nil {
		t.Fatal(err)
	}
	ln, err := net.Listen("unix", filepath.Join(stale, server.SocketName(testDaemon)))
	if err != nil {
		t.Fatal(err)
	}
	ln.(*net.UnixListener).SetUnlinkOnClose(false)
	ln.Close()
	if _, err := os.Stat(filepath.Join(stale, server.SocketName(testDaemon))); err != nil {
		t.Fatal("the stale socket was not left behind for the test")
	}
	ep, err := server.Prepare(stale, "unix", testDaemon)
	if err != nil {
		t.Fatalf("over a stale socket: %v", err)
	}
	ep.Listener.Close()
	ep.Release()
	for _, f := range []string{server.EndpointFile, server.TokenFile, server.SocketName(testDaemon)} {
		if _, err := os.Stat(filepath.Join(stale, f)); !os.IsNotExist(err) {
			t.Errorf("%s left after release", f)
		}
	}
}

func TestTokenIsNewEveryStart(t *testing.T) {
	dir := runDir(t)
	a, err := server.Prepare(dir, "unix", testDaemon)
	if err != nil {
		t.Fatal(err)
	}
	a.Listener.Close()
	a.Release()
	b, err := server.Prepare(dir, "unix", testDaemon)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { b.Listener.Close(); b.Release() }()
	if a.Token == b.Token || len(b.Token) != 64 {
		t.Fatalf("tokens %q and %q", a.Token, b.Token)
	}
}

func TestTCPEndpoint(t *testing.T) {
	dir := runDir(t)
	ep, err := server.Prepare(dir, "tcp:127.0.0.1:0", testDaemon)
	if err != nil {
		t.Fatal(err)
	}
	srv := server.New(ep.Token, quiet(), func() any { return map[string]any{} })
	go srv.Serve(ep.Listener)
	defer func() { ep.Listener.Close(); srv.Close(); ep.Release() }()
	c, err := clienttest.Dial(dir)
	if err != nil {
		t.Fatal(err)
	}
	c.Close()
}

func TestGarbageDoesNotKillTheConnection(t *testing.T) {
	dir := runDir(t)
	start(t, dir)
	c, err := clienttest.Dial(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_ = c.Send(0, []byte("{not json"))
	_ = c.Send(0, []byte(`{"jsonrpc":"1.0","id":5,"method":"echo"}`))
	_ = c.Send(42, []byte("to a channel nobody opened"))
	if err := c.Call(context.Background(), "echo", 1, nil); err != nil {
		t.Fatalf("after garbage: %v", err)
	}
}

func TestASecondTokenGrantsItsRole(t *testing.T) {
	dir := runDir(t)
	ep, err := server.Prepare(dir, "unix", testDaemon)
	if err != nil {
		t.Fatal(err)
	}
	operator, err := server.NewToken()
	if err != nil {
		t.Fatal(err)
	}
	srv := server.New(ep.Token, quiet(), nil)
	srv.AddToken(operator, "operator")
	srv.RoleHello = func(role string) any { return map[string]any{"role": role} }
	srv.Handle("whoami", func(ctx context.Context, c *server.Conn, p json.RawMessage) (any, error) {
		return c.Role(), nil
	})
	go srv.Serve(ep.Listener)
	defer func() { ep.Listener.Close(); srv.Close(); ep.Release() }()
	sock := filepath.Join(dir, server.SocketName(testDaemon))
	for token, want := range map[string]string{ep.Token: "", operator: "operator"} {
		c, err := clienttest.DialWith("unix", sock, token)
		if err != nil {
			t.Fatal(err)
		}
		var hello map[string]string
		_ = json.Unmarshal(c.Hello, &hello)
		var got string
		if err := c.Call(context.Background(), "whoami", nil, &got); err != nil || got != want || hello["role"] != want {
			t.Errorf("token for %q: role %q, hello %v, %v", want, got, hello, err)
		}
		c.Close()
	}
}
