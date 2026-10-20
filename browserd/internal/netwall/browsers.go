package netwall

import (
	"context"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// CodeBlocked is the error an agent's navigation gets when the wall refuses it (the contract's 1102).
const CodeBlocked = 1102

// Browsers is the wall as the daemon's browser manager uses it: a proxy for each browser while it
// runs, and the check every navigation passes before it is made.
type Browsers struct {
	Wall *Wall

	mu      sync.Mutex
	proxies map[string]*Proxy
}

// NewBrowsers wraps w.
func NewBrowsers(w *Wall) *Browsers {
	return &Browsers{Wall: w, proxies: map[string]*Proxy{}}
}

// Open starts the proxy of a browser about to start, and returns the address Chromium is pointed at.
func (b *Browsers) Open(browserID string) (string, error) {
	p, err := b.Wall.Listen(browserID)
	if err != nil {
		return "", err
	}
	b.mu.Lock()
	if old := b.proxies[browserID]; old != nil {
		old.Close()
	}
	b.proxies[browserID] = p
	b.mu.Unlock()
	return p.Addr(), nil
}

// Track gives a browser with no proxy of its own a place for the operator's grants, so a
// navigation the operator allowed is judged as allowed (see Wall.Unlistened).
func (b *Browsers) Track(browserID string) {
	p := b.Wall.Unlistened(browserID)
	b.mu.Lock()
	if old := b.proxies[browserID]; old != nil {
		old.Close()
	}
	b.proxies[browserID] = p
	b.mu.Unlock()
}

// Close ends a browser's proxy, and with it every connection the browser still had.
func (b *Browsers) Close(browserID string) {
	b.mu.Lock()
	p := b.proxies[browserID]
	delete(b.proxies, browserID)
	b.mu.Unlock()
	if p != nil {
		p.Close()
	}
}

// Proxy is a running browser's proxy, or nil.
func (b *Browsers) Proxy(browserID string) *Proxy {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.proxies[browserID]
}

// Navigation judges a top-level navigation of browserID's page to url: nil, or the 1102 error the
// caller answers with, carrying the verdict as its data. The scheme is judged here too, so a caller
// that forgot its own scheme check is still covered.
func (b *Browsers) Navigation(ctx context.Context, browserID, url string) error {
	v := b.Wall.CheckNavigation(ctx, b.Proxy(browserID), url)
	if v.Allowed() {
		return nil
	}
	e := wire.Errorf(CodeBlocked, "%s", v.Message())
	e.Data = v.Data()
	return e
}

// Allowlist reports whether the operator's rules carry an allowlist.
func (b *Browsers) Allowlist() bool { return b.Wall.current().hasEgress }

// Grant opens an asked destination for one browser; see Proxy.Grant.
func (b *Browsers) Grant(browserID, host string, port int, ttl time.Duration) bool {
	p := b.Proxy(browserID)
	if p == nil {
		return false
	}
	p.Grant(host, port, b.Wall.now().Add(ttl))
	return true
}

// Revoke closes a granted destination again.
func (b *Browsers) Revoke(browserID, host string, port int) bool {
	p := b.Proxy(browserID)
	if p == nil {
		return false
	}
	p.Revoke(host, port)
	return true
}
