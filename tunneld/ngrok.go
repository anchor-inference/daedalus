package main

// ngrok, through the operator's own ngrok program and their own token.
//
// The Go SDK would be smaller to live with, and it was the first plan. It is MIT, but ngrok's terms name
// "the ngrok client software" the ngrok Agent and say that distributing it "to your customers who
// maintain their own accounts with ngrok" takes ngrok's "prior written consent". An SDK inside a release
// is too close to that to ship on a guess, and a self-hosted project with a token field is exactly what
// must not look like a bundled service. So nothing of ngrok's is in this binary or downloaded by it: the
// operator installs ngrok from ngrok, and this runs it.
//
// The token goes in a config file of this daemon's, 0600, and never on a command line. The client's own
// local API says which address it got and whether it holds one, and its logs say when a token is refused.

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// A token refused or a plan that does not allow this is not going to change by trying again.
var ngrokRefusal = regexp.MustCompile(`ERR_NGROK_(105|106|107|108|120|121|334|4018)\b`)

type ngrok struct {
	binary string
	token  string
	dir    string

	mu      sync.Mutex
	web     string
	refused string
}

func (n *ngrok) Visitor() VisitorOf    { return lastPublicForwarded }
func (n *ngrok) NeedsRouterPort() bool { return true }

func (n *ngrok) config(web string) string {
	return fmt.Sprintf("version: \"3\"\nagent:\n  authtoken: %s\n  web_addr: 127.0.0.1:%s\n  update_check: false\n  console_ui: false\n", strconv.Quote(n.token), web)
}

func (n *ngrok) Run(ctx context.Context, t *Tunnel) error {
	if n.token == "" {
		return fatal(CodeNotConfigured, "no ngrok token")
	}
	port, err := freeLoopbackPort()
	if err != nil {
		return err
	}
	n.mu.Lock()
	n.web, n.refused = "127.0.0.1:"+port, ""
	n.mu.Unlock()
	home := filepath.Join(n.dir, "ngrok")
	if err := os.MkdirAll(home, 0o700); err != nil {
		return err
	}
	file := filepath.Join(home, "ngrok.yml")
	if err := os.WriteFile(file, []byte(n.config(port)), 0o600); err != nil {
		return err
	}
	defer os.Remove(file)
	go n.watchAddress(ctx, t)
	err = runClient(ctx, t, n.binary, []string{"http", t.Router, "--config", file, "--log", "stdout", "--log-format", "json"}, clientEnv(home), func(line string) {
		if refused := ngrokRefusal.FindString(line); refused != "" {
			n.mu.Lock()
			n.refused = refused
			n.mu.Unlock()
		}
	})
	n.mu.Lock()
	refused := n.refused
	n.mu.Unlock()
	if refused != "" && ctx.Err() == nil {
		return fatal(CodeTokenRejected, "ngrok refused this token or plan (%s)", refused)
	}
	return err
}

func (n *ngrok) address(ctx context.Context) string {
	n.mu.Lock()
	web := n.web
	n.mu.Unlock()
	if web == "" {
		return ""
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+web+"/api/tunnels", nil)
	if err != nil {
		return ""
	}
	answer, err := localAPI.Do(request)
	if err != nil {
		return ""
	}
	defer answer.Body.Close()
	var body struct {
		Tunnels []struct {
			PublicURL string `json:"public_url"`
		} `json:"tunnels"`
	}
	if json.NewDecoder(answer.Body).Decode(&body) != nil {
		return ""
	}
	for _, tunnel := range body.Tunnels {
		if strings.HasPrefix(tunnel.PublicURL, "https://") {
			return tunnel.PublicURL
		}
	}
	return ""
}

func (n *ngrok) watchAddress(ctx context.Context, t *Tunnel) {
	for ctx.Err() == nil {
		if address := n.address(ctx); address != "" {
			t.Up(address)
			return
		}
		select {
		case <-ctx.Done():
		case <-time.After(500 * time.Millisecond):
		}
	}
}

func (n *ngrok) Connected(ctx context.Context) bool { return n.address(ctx) != "" }
