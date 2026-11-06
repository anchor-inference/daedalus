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
// The client's local API stays shut: web_addr false, and --inspect=false. Open, it answered anyone on this
// machine with no secret. POST /api/tunnels {"addr":"<the app's port>"} opened a public tunnel straight to
// the API, past the router, and GET /api/requests/http returned captured invites and bearers. Everything
// wanted from it comes from the client's JSON log instead: the address on "started tunnel", the session
// going and coming on the lines below, a refusal on its error. The words were read from ngrok 3.39 running
// with this configuration, and "started tunnel" is the one line only a real account can show.
//
// The token goes in a config file of this daemon's, 0600, and never on a command line.

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
)

// A token refused or a plan that does not allow this is not going to change by trying again.
var ngrokRefusal = regexp.MustCompile(`ERR_NGROK_(105|106|107|108|120|121|334|4018)\b|authentication failed`)

type ngrok struct {
	binary string
	token  string
	dir    string

	mu      sync.Mutex
	refused string
	session bool
}

func (n *ngrok) Visitor() VisitorOf    { return lastPublicForwarded }
func (n *ngrok) NeedsRouterPort() bool { return true }

func (n *ngrok) config() string {
	return fmt.Sprintf("version: \"3\"\nagent:\n  authtoken: %s\n  web_addr: false\n  update_check: false\n  console_ui: false\n", strconv.Quote(n.token))
}

// read takes one line of the client's output, which is its only way of saying anything.
func (n *ngrok) read(line string, t *Tunnel) {
	if refused := ngrokRefusal.FindString(line); refused != "" {
		n.mu.Lock()
		n.refused = refused
		n.mu.Unlock()
	}
	var entry struct {
		Message string `json:"msg"`
		URL     string `json:"url"`
	}
	if json.Unmarshal([]byte(line), &entry) != nil {
		return
	}
	switch entry.Message {
	case "client session established":
		n.setSession(true)
	case "session closing", "failed to reconnect session":
		n.setSession(false)
	case "started tunnel":
		n.setSession(true)
		if strings.HasPrefix(entry.URL, "https://") {
			t.Up(entry.URL)
		}
	}
}

func (n *ngrok) setSession(up bool) {
	n.mu.Lock()
	n.session = up
	n.mu.Unlock()
}

func (n *ngrok) Run(ctx context.Context, t *Tunnel) error {
	if n.token == "" {
		return fatal(CodeNotConfigured, "no ngrok token")
	}
	n.mu.Lock()
	n.refused, n.session = "", false
	n.mu.Unlock()
	home := filepath.Join(n.dir, "ngrok")
	if err := os.MkdirAll(home, 0o700); err != nil {
		return err
	}
	file := filepath.Join(home, "ngrok.yml")
	if err := os.WriteFile(file, []byte(n.config()), 0o600); err != nil {
		return err
	}
	defer os.Remove(file)
	args := []string{"http", t.Router, "--inspect=false", "--config", file, "--log", "stdout", "--log-format", "json"}
	err := runClient(ctx, t, n.binary, args, clientEnv(home), func(line string) { n.read(line, t) })
	n.mu.Lock()
	refused := n.refused
	n.session = false
	n.mu.Unlock()
	if refused != "" && ctx.Err() == nil {
		return fatal(CodeTokenRejected, "ngrok refused this token or plan (%s)", refused)
	}
	return err
}

func (n *ngrok) Connected(context.Context) bool {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.session
}
