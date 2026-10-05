//go:build linux

package providerchannel

import (
	"bufio"
	"context"
	"io"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type answers struct {
	mu    sync.Mutex
	calls int
	sets  [][]net.IPAddr
}

func (a *answers) LookupIPAddr(_ context.Context, _ string) ([]net.IPAddr, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	set := a.sets[min(a.calls, len(a.sets)-1)]
	a.calls++
	return set, nil
}

func TestPinProviderRejectsLocalAndMixedAnswers(t *testing.T) {
	for _, address := range []string{"127.0.0.1", "10.0.0.1", "100.64.0.1", "169.254.1.1", "192.0.2.1", "192.88.99.1",
		"198.18.0.1", "203.0.113.1", "::1", "::ffff:127.0.0.1", "fc00::1", "fe80::1", "64:ff9b::a00:1", "2001:db8::1", "2002::1"} {
		resolver := &answers{sets: [][]net.IPAddr{{{IP: net.ParseIP(address)}}}}
		if _, err := PinProvider(context.Background(), "api.test:443", resolver); err == nil {
			t.Fatalf("nonpublic DNS answer accepted: %s", address)
		}
	}
	resolver := &answers{sets: [][]net.IPAddr{{{IP: net.ParseIP("8.8.8.8")}, {IP: net.ParseIP("127.0.0.1")}}}}
	if _, err := PinProvider(context.Background(), "api.test:443", resolver); err == nil {
		t.Fatal("mixed public and local DNS answers accepted")
	}
	for _, authority := range []string{"127.0.0.1:443", "api.test:80", "API.TEST:443", "api.test.:443",
		"api.test:443/path", "localhost:443", "bad..test:443"} {
		if _, err := PinProvider(context.Background(), authority, resolver); err == nil {
			t.Fatalf("invalid provider authority accepted: %s", authority)
		}
	}
}

func TestPinnedBrokerIgnoresLaterDNSAndRevokesStreams(t *testing.T) {
	resolver := &answers{sets: [][]net.IPAddr{{{IP: net.ParseIP("8.8.8.8")}}, {{IP: net.ParseIP("127.0.0.1")}}}}
	target, err := PinProvider(context.Background(), "api.test:443", resolver)
	if err != nil {
		t.Fatal(err)
	}
	parent := t.TempDir()
	if err := os.Chmod(parent, 0700); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	dials := 0
	broker, err := startBroker(parent, target, func(_ context.Context, network, address string) (net.Conn, error) {
		if network != "tcp" || address != "8.8.8.8:443" {
			t.Errorf("broker dialed a changed endpoint: %s %s", network, address)
		}
		mu.Lock()
		dials++
		mu.Unlock()
		client, endpoint := net.Pipe()
		go func() {
			defer endpoint.Close()
			request := make([]byte, 4)
			if _, err := io.ReadFull(endpoint, request); err == nil && string(request) == "ping" {
				_, _ = endpoint.Write([]byte("pong"))
			}
			_, _ = io.Copy(io.Discard, endpoint)
		}()
		return client, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	defer broker.Close()
	directory, err := broker.Directory()
	if err != nil {
		t.Fatal(err)
	}
	defer directory.Close()
	if info, err := os.Stat(broker.directory); err != nil || info.Mode().Perm() != 0700 {
		t.Fatalf("broker directory is not private: %v %v", info, err)
	}
	if info, err := os.Stat(filepath.Join(broker.directory, "provider.sock")); err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("broker socket is not private: %v %v", info, err)
	}
	open := func() *net.UnixConn {
		t.Helper()
		conn, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: filepath.Join(broker.directory, "provider.sock"), Net: "unix"})
		if err != nil {
			t.Fatal(err)
		}
		conn.SetDeadline(time.Now().Add(5 * time.Second))
		return conn
	}
	peer := open()
	if _, err := io.WriteString(peer, "CONNECT peer.test:443 HTTP/1.1\r\n\r\n"); err != nil {
		t.Fatal(err)
	}
	denied, err := io.ReadAll(peer)
	peer.Close()
	if err != nil || !strings.HasPrefix(string(denied), "HTTP/1.1 403") {
		t.Fatalf("peer target was not denied: %q %v", denied, err)
	}
	mu.Lock()
	if dials != 0 {
		t.Fatal("denied target reached the dialer")
	}
	mu.Unlock()
	selected := open()
	if _, err := io.WriteString(selected, "CONNECT api.test:443 HTTP/1.1\r\n\r\n"); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(selected)
	if line, err := reader.ReadString('\n'); err != nil || line != "HTTP/1.1 200 Connection Established\r\n" {
		t.Fatalf("selected target did not connect: %q %v", line, err)
	}
	if line, err := reader.ReadString('\n'); err != nil || line != "\r\n" {
		t.Fatalf("invalid CONNECT header: %q %v", line, err)
	}
	if _, err := io.WriteString(selected, "ping"); err != nil {
		t.Fatal(err)
	}
	response := make([]byte, 4)
	if _, err := io.ReadFull(reader, response); err != nil || string(response) != "pong" {
		t.Fatalf("pinned provider response: %q %v", response, err)
	}
	mu.Lock()
	if dials != 1 {
		t.Fatalf("expected one selected dial, got %d", dials)
	}
	mu.Unlock()
	resolver.mu.Lock()
	if resolver.calls != 1 {
		t.Fatalf("broker re-resolved provider DNS %d times", resolver.calls)
	}
	resolver.mu.Unlock()
	if err := broker.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := selected.Read(make([]byte, 1)); err == nil {
		t.Fatal("active provider stream survived broker close")
	}
	selected.Close()
	if _, err := broker.Directory(); err == nil {
		t.Fatal("closed broker still handed out a socket directory")
	}
	if _, err := os.Stat(broker.directory); !os.IsNotExist(err) {
		t.Fatalf("broker directory survived close: %v", err)
	}
	if _, err := PinProvider(context.Background(), "api.test:443", resolver); err == nil {
		t.Fatal("new launch accepted DNS rebinding to loopback")
	}
}

func TestPinnedBrokerRejectsUnownedOrSharedParent(t *testing.T) {
	target := PinnedProvider{authority: "api.test:443", address: netip.MustParseAddr("8.8.8.8")}
	parent := t.TempDir()
	if err := os.Chmod(parent, 0755); err != nil {
		t.Fatal(err)
	}
	if broker, err := startBroker(parent, target, func(context.Context, string, string) (net.Conn, error) {
		return nil, nil
	}); err == nil {
		broker.Close()
		t.Fatal("shared parent accepted")
	}
	if err := os.Chmod(parent, 0700); err != nil {
		t.Fatal(err)
	}
	alias := filepath.Join(t.TempDir(), "alias")
	if err := os.Symlink(parent, alias); err != nil {
		t.Fatal(err)
	}
	if broker, err := startBroker(alias, target, func(context.Context, string, string) (net.Conn, error) {
		return nil, nil
	}); err == nil {
		broker.Close()
		t.Fatal("symlinked parent accepted")
	}
}
