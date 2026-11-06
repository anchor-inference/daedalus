package main

// The operator's own VPS, through frp: the one option where no third party reads the traffic or caps
// it. frpc runs as a program of its own and is not linked in here, and that is the reason this binary
// exists apart from the launcher at all: antivirus products flag frpc (Defender as a hack tool, by the
// project's own account), and whatever they do to it they then do to a file nobody else depends on.
//
// HTTPS ends at the operator's server (Caddy in the documented setup), so the address is always https
// and the visitor's address arrives in X-Forwarded-For, written by the proxies there.

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"sync"
)

type frp struct {
	binary string
	server string
	domain string
	token  string
	dir    string

	mu    sync.Mutex
	admin string
}

func (f *frp) Visitor() VisitorOf    { return lastPublicForwarded }
func (f *frp) NeedsRouterPort() bool { return true }

// config writes a client that keeps trying: after a sleep, or while the server restarts, frpc reconnects
// by itself rather than exiting, and a login that fails at first is one of those moments.
func (f *frp) config(host, port, admin, routerPort string) string {
	return fmt.Sprintf(`serverAddr = %s
serverPort = %s
loginFailExit = false
auth.method = "token"
auth.token = %s
webServer.addr = "127.0.0.1"
webServer.port = %s
log.to = "console"
log.level = "info"

[[proxies]]
name = "daedalus"
type = "http"
localIP = "127.0.0.1"
localPort = %s
customDomains = [%s]
`, strconv.Quote(host), port, strconv.Quote(f.token), admin, routerPort, strconv.Quote(f.domain))
}

func (f *frp) Run(ctx context.Context, t *Tunnel) error {
	host, port, err := net.SplitHostPort(f.server)
	if err != nil {
		host, port = f.server, "7000"
	}
	if host == "" || f.domain == "" || f.token == "" {
		return fatal(CodeNotConfigured, "the server, its domain and its token are all needed")
	}
	admin, err := freeLoopbackPort()
	if err != nil {
		return err
	}
	_, routerPort, err := net.SplitHostPort(trimScheme(t.Router))
	if err != nil {
		return err
	}
	home := filepath.Join(f.dir, "frp")
	if err := os.MkdirAll(home, 0o700); err != nil {
		return err
	}
	file := filepath.Join(home, "frpc.toml")
	if err := os.WriteFile(file, []byte(f.config(host, port, admin, routerPort)), 0o600); err != nil {
		return err
	}
	defer os.Remove(file)
	f.mu.Lock()
	f.admin = "127.0.0.1:" + admin
	f.mu.Unlock()
	t.Up("https://" + f.domain)
	return runClient(ctx, t, f.binary, []string{"-c", file}, clientEnv(home), nil)
}

func trimScheme(url string) string {
	if len(url) > 7 && url[:7] == "http://" {
		return url[7:]
	}
	return url
}

func (f *frp) Connected(ctx context.Context) bool {
	f.mu.Lock()
	admin := f.admin
	f.mu.Unlock()
	if admin == "" {
		return false
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+admin+"/api/status", nil)
	if err != nil {
		return false
	}
	answer, err := localAPI.Do(request)
	if err != nil {
		return false
	}
	defer answer.Body.Close()
	var body map[string][]struct {
		Name   string `json:"name"`
		Status string `json:"status"`
	}
	if json.NewDecoder(answer.Body).Decode(&body) != nil {
		return false
	}
	for _, proxies := range body {
		for _, proxy := range proxies {
			if proxy.Status == "running" {
				return true
			}
		}
	}
	return false
}
