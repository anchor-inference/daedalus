package main

// The operator's own VPS, through frp: the one option where no third party reads the traffic or caps
// it. frpc runs as a program of its own and is not linked in here, and that is the reason this binary
// exists apart from the launcher at all: antivirus products flag frpc (Defender as a hack tool, by the
// project's own account), and whatever they do to it they then do to a file nobody else depends on.
//
// HTTPS ends at the operator's server (Caddy in the documented setup), so the address is always https
// and the visitor's address arrives in X-Forwarded-For, written by the proxies there.
//
// frpc's admin server is not configured. With it on, anyone on this machine could read this file's token
// with GET /api/config, and PUT a new one with GET /api/reload to aim localPort at the app. Nothing here
// needs it: the client's log says all of it, and these are its words, read from frpc 0.72 against a real
// frps that was stopped and started again.
//
// The link to the server is TLS, and frpc does not verify the server's certificate unless it is given a CA
// (transport.tls.trustedCaFile). The token is not sent in the clear, but someone on the path could stand
// in for the server and read the traffic. The settings say so; a CA of one's own is the operator's to set up.

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
)

type frp struct {
	binary string
	server string
	domain string
	token  string
	dir    string

	mu        sync.Mutex
	connected bool
}

const (
	frpProxyUp      = "start proxy success"
	frpTrying       = "try to connect to server"
	frpLoginRefused = "token in login doesn't match"
)

func (f *frp) Visitor() VisitorOf    { return lastPublicForwarded }
func (f *frp) NeedsRouterPort() bool { return true }

// config writes a client that keeps trying: after a sleep, or while the server restarts, frpc reconnects
// by itself rather than exiting, and a login that fails at first is one of those moments.
func (f *frp) config(host, port, routerPort string) string {
	return fmt.Sprintf(`serverAddr = %s
serverPort = %s
loginFailExit = false
auth.method = "token"
auth.token = %s
log.to = "console"
log.level = "info"
log.disablePrintColor = true

[[proxies]]
name = "daedalus"
type = "http"
localIP = "127.0.0.1"
localPort = %s
customDomains = [%s]
`, strconv.Quote(host), port, strconv.Quote(f.token), routerPort, strconv.Quote(f.domain))
}

// read follows the client's own account of its connection. A token the server turns down is said here
// and is not going to change by trying again, while frpc would try for ever.
func (f *frp) read(line string, t *Tunnel, refuse func()) {
	switch {
	case strings.Contains(line, frpProxyUp):
		f.setConnected(true)
		t.Up("https://" + f.domain)
	case strings.Contains(line, frpTrying):
		f.setConnected(false)
	case strings.Contains(line, frpLoginRefused):
		refuse()
	}
}

func (f *frp) setConnected(up bool) {
	f.mu.Lock()
	f.connected = up
	f.mu.Unlock()
}

func (f *frp) Run(ctx context.Context, t *Tunnel) error {
	host, port, err := net.SplitHostPort(f.server)
	if err != nil {
		host, port = f.server, "7000"
	}
	if host == "" || f.domain == "" || f.token == "" {
		return fatal(CodeNotConfigured, "the server, its domain and its token are all needed")
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
	if err := os.WriteFile(file, []byte(f.config(host, port, routerPort)), 0o600); err != nil {
		return err
	}
	defer os.Remove(file)
	f.setConnected(false)
	running, stop := context.WithCancel(ctx)
	defer stop()
	refused := false
	err = runClient(running, t, f.binary, []string{"-c", file}, clientEnv(home), func(line string) {
		f.read(line, t, func() {
			refused = true
			stop()
		})
	})
	f.setConnected(false)
	if refused {
		return fatal(CodeServerRefused, "the server turned down this token")
	}
	return err
}

func trimScheme(url string) string {
	if len(url) > 7 && url[:7] == "http://" {
		return url[7:]
	}
	return url
}

func (f *frp) Connected(context.Context) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.connected
}
