package main

// A Cloudflare quick tunnel: no account, and a new address each time the client starts. Run through
// cloudflared, which is a program of its own and has no library worth embedding. The address comes from
// the client's metrics listener; /ready there says how many connections it holds to the edge, which
// after a sleep is the honest answer. The output is read only as a fallback for the address. That
// listener takes no orders, unlike ngrok's and frpc's, and quick tunnels have no credential to read in it,
// but it does serve profiles of the process, so its port is sealed from the agent with the others.
//
// Its visitor's address is in Cf-Connecting-Ip, and the edge is why that can be believed: tried on a live
// tunnel, a request that forged the header was turned away there with a 403 and never arrived.

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"time"
)

var (
	quickAddress = regexp.MustCompile(`https://[a-z0-9-]+\.trycloudflare\.com`)
	quickHost    = regexp.MustCompile(`^[a-z0-9-]+\.trycloudflare\.com$`)
)

type cloudflare struct {
	binary string
	dir    string

	mu      sync.Mutex
	metrics string
}

func (c *cloudflare) Visitor() VisitorOf    { return edgeHeader("Cf-Connecting-Ip") }
func (c *cloudflare) NeedsRouterPort() bool { return true }

func (c *cloudflare) Run(ctx context.Context, t *Tunnel) error {
	port, err := freeLoopbackPort()
	if err != nil {
		return err
	}
	c.mu.Lock()
	c.metrics = "127.0.0.1:" + port
	c.mu.Unlock()
	t.Port(port)
	home := filepath.Join(c.dir, "cloudflared")
	if err := os.MkdirAll(home, 0o700); err != nil {
		return err
	}
	args := []string{"tunnel", "--no-autoupdate", "--loglevel", "info", "--metrics", c.metrics, "--url", t.Router}
	found := make(chan string, 1)
	go c.askAddress(ctx, found)
	go func() {
		select {
		case address := <-found:
			t.Up(address)
		case <-ctx.Done():
		}
	}()
	return runClient(ctx, t, c.binary, args, clientEnv(home), func(line string) {
		if address := quickAddress.FindString(line); address != "" {
			select {
			case found <- address:
			default:
			}
		}
	})
}

func (c *cloudflare) askAddress(ctx context.Context, found chan<- string) {
	for ctx.Err() == nil {
		c.mu.Lock()
		metrics := c.metrics
		c.mu.Unlock()
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+metrics+"/quicktunnel", nil)
		if err == nil {
			if answer, err := localAPI.Do(request); err == nil {
				var body struct {
					Hostname string `json:"hostname"`
				}
				decoded := json.NewDecoder(answer.Body).Decode(&body)
				answer.Body.Close()
				if decoded == nil && quickHost.MatchString(body.Hostname) {
					select {
					case found <- "https://" + body.Hostname:
					default:
					}
					return
				}
			}
		}
		select {
		case <-ctx.Done():
		case <-time.After(500 * time.Millisecond):
		}
	}
}

func (c *cloudflare) Connected(ctx context.Context) bool {
	c.mu.Lock()
	metrics := c.metrics
	c.mu.Unlock()
	if metrics == "" {
		return false
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+metrics+"/ready", nil)
	if err != nil {
		return false
	}
	answer, err := localAPI.Do(request)
	if err != nil {
		return false
	}
	defer answer.Body.Close()
	var body struct {
		ReadyConnections int `json:"readyConnections"`
	}
	return answer.StatusCode == http.StatusOK && json.NewDecoder(answer.Body).Decode(&body) == nil && body.ReadyConnections > 0
}
