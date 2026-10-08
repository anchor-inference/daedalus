package netwall

import (
	"context"
	"net"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Resolver is the name lookup the wall trusts. *net.Resolver is one; tests give a fake, since the
// tests may not touch the network.
type Resolver interface {
	LookupNetIP(ctx context.Context, network, host string) ([]netip.Addr, error)
}

// Egress is one destination the browser asked for, as the host's egress log records it.
type Egress struct {
	Browser  string   `json:"browser_id"`
	Host     string   `json:"host"`
	Port     int      `json:"port"`
	Decision Decision `json:"decision"`
	Reason   string   `json:"reason,omitempty"`
	At       int64    `json:"at"`
}

// Options are the wall's collaborators.
type Options struct {
	// Resolver looks names up; nil is the system's.
	Resolver Resolver
	// Events receives an Egress for every distinct host, decision and browser at most once a
	// minute. It must not block: it runs on the request's path.
	Events func(Egress)
	// LocalAddrs lists this machine's own addresses; nil reads the interfaces. A connection to one
	// of them is a connection to this machine, whatever its class.
	LocalAddrs func() []netip.Addr
	// Now is the clock; nil is time.Now.
	Now func() time.Time
}

// Wall holds the rules and judges destinations against them. One per daemon; each browser gets a
// Proxy of its own from Listen, so a grant and an egress event belong to one browser.
type Wall struct {
	opts     Options
	resolver Resolver

	mu    sync.RWMutex
	rules *rules

	localMu  sync.Mutex
	local    map[netip.Addr]bool
	localAt  time.Time
	gwMu     sync.Mutex
	gateway  map[netip.Addr]bool
	gwAt     time.Time
	gwFor    string
	proxyMu  sync.Mutex
	proxies  map[*Proxy]bool
	seen     map[egressKey]time.Time
	seenMu   sync.Mutex
	lookupTO time.Duration

	// redirect is for tests only: the address actually dialled for a judged one, so a name the fake
	// resolver calls public can be served by a loopback fixture. The judgement is always made on the
	// address before it.
	redirect func(netip.AddrPort) netip.AddrPort
}

type egressKey struct {
	browser, host string
	port          int
	decision      Decision
}

// New makes a wall with the strictest rules until Configure is called.
func New(opts Options) *Wall {
	w := &Wall{opts: opts, resolver: opts.Resolver, proxies: map[*Proxy]bool{}, seen: map[egressKey]time.Time{}, lookupTO: 5 * time.Second}
	if w.resolver == nil {
		w.resolver = net.DefaultResolver
	}
	w.rules, _ = Config{}.compile()
	return w
}

// SetTestRedirect is for the tests of other packages that browse through the wall: it dials a
// fixture on loopback in place of the address a fake resolver gave a name, after the wall has judged
// that address. A daemon never calls it.
func (w *Wall) SetTestRedirect(f func(netip.AddrPort) netip.AddrPort) { w.redirect = f }

// Configure replaces the rules. A tunnel already open stays open (a page's WebSocket is not cut by
// a settings change); every new connection is judged by the new rules.
func (w *Wall) Configure(c Config) error {
	r, err := c.compile()
	if err != nil {
		return err
	}
	w.mu.Lock()
	w.rules = r
	w.mu.Unlock()
	w.gwMu.Lock()
	w.gwAt = time.Time{}
	w.gwMu.Unlock()
	// Idle upstream connections were judged by the old rules; the next request opens a new one.
	w.proxyMu.Lock()
	for p := range w.proxies {
		p.tr.CloseIdleConnections()
	}
	w.proxyMu.Unlock()
	return nil
}

func (w *Wall) now() time.Time {
	if w.opts.Now != nil {
		return w.opts.Now()
	}
	return time.Now()
}

func (w *Wall) current() *rules {
	w.mu.RLock()
	defer w.mu.RUnlock()
	return w.rules
}

// isLocal reports whether a is one of this machine's own addresses. They are read at most every ten
// seconds: an interface that comes up is covered within that, and a decision costs no system call.
func (w *Wall) isLocal(a netip.Addr) bool {
	w.localMu.Lock()
	defer w.localMu.Unlock()
	if w.local == nil || w.now().Sub(w.localAt) > 10*time.Second {
		w.local = map[netip.Addr]bool{}
		for _, own := range w.localAddrs() {
			w.local[own.Unmap()] = true
		}
		w.localAt = w.now()
	}
	return w.local[a.Unmap()]
}

func (w *Wall) localAddrs() []netip.Addr {
	if w.opts.LocalAddrs != nil {
		return w.opts.LocalAddrs()
	}
	var out []netip.Addr
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return nil
	}
	for _, a := range addrs {
		if n, ok := a.(*net.IPNet); ok {
			if ip, ok := netip.AddrFromSlice(n.IP); ok {
				out = append(out, ip.Unmap())
			}
		}
	}
	return out
}

// isGateway reports whether a is the Docker host that LoopbackRewrite names. Its addresses are
// looked up at most once a minute.
func (w *Wall) isGateway(ctx context.Context, r *rules, a netip.Addr) bool {
	if r.rewrite == "" {
		return false
	}
	w.gwMu.Lock()
	defer w.gwMu.Unlock()
	if w.gateway == nil || w.gwFor != r.rewrite || w.now().Sub(w.gwAt) > time.Minute {
		w.gateway = map[netip.Addr]bool{}
		w.gwFor = r.rewrite
		w.gwAt = w.now()
		if addrs, err := w.lookup(ctx, r.rewrite); err == nil {
			for _, g := range addrs {
				w.gateway[g.Unmap()] = true
			}
		}
	}
	return w.gateway[a.Unmap()]
}

func (w *Wall) lookup(ctx context.Context, host string) ([]netip.Addr, error) {
	if a, err := netip.ParseAddr(host); err == nil {
		return []netip.Addr{a.Unmap()}, nil
	}
	// "localhost" and everything under ".localhost" are this machine by definition (RFC 6761), as
	// Chromium itself treats them: they are never asked of a resolver that might say otherwise.
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return []netip.Addr{netip.MustParseAddr("127.0.0.1"), netip.MustParseAddr("::1")}, nil
	}
	ctx, cancel := context.WithTimeout(ctx, w.lookupTO)
	defer cancel()
	addrs, err := w.resolver.LookupNetIP(ctx, "ip", host)
	if err != nil {
		return nil, err
	}
	for i := range addrs {
		addrs[i] = addrs[i].Unmap()
	}
	return addrs, nil
}

// normalHost is a host as the rules compare it: lower case, no brackets, no trailing dot.
func normalHost(host string) string {
	host = strings.ToLower(strings.TrimSpace(host))
	host = strings.TrimSuffix(strings.TrimPrefix(host, "["), "]")
	if i := strings.IndexByte(host, '%'); i >= 0 {
		// An IPv6 zone names an interface of this machine: it is only ever used for link-local
		// addresses, which are refused anyway, and it would otherwise let "fe80::1%eth0" dodge the
		// parser.
		host = host[:i]
	}
	return strings.TrimSuffix(host, ".")
}

// isLoopbackName reports whether host, as written, names this machine without a lookup.
func isLoopbackName(host string) bool {
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return true
	}
	a, err := netip.ParseAddr(host)
	return err == nil && classify(a) == classLoopback
}

// judge resolves host and decides about a connection to it on port, for browser p (nil for a check
// that belongs to no browser yet). The strictest answer over every address wins: a name that
// answers with a public address and a private one is refused, because that mixture is what a
// rebinding attack looks like.
func (w *Wall) judge(ctx context.Context, p *Proxy, host string, port int) Verdict {
	host = normalHost(host)
	v := Verdict{Host: host, Port: port}
	if host == "" || port < 1 || port > 65535 {
		v.Decision, v.Reason = Deny, ReasonBadTarget
		return v
	}
	r := w.current()
	target := host
	// In a container a loopback link means the Docker host: the agent's services and the terminals'
	// servers are published there, and so are the local sites, not in the browser's container, which
	// serves nothing. Outside the services ranges only when local sites may be opened at all, so a
	// refusal still names the loopback address the agent asked for.
	if r.rewrite != "" && isLoopbackName(host) && (r.inServices(port) || r.local != LocalServices) {
		target = r.rewrite
	}
	addrs, err := w.lookup(ctx, target)
	if err != nil || len(addrs) == 0 {
		v.Decision, v.Reason = Deny, ReasonUnresolvable
		return v
	}
	v.Decision = Allow
	public, lan := false, false
	for _, a := range addrs {
		d, reason, onLAN := w.judgeAddr(ctx, r, p, host, a, port)
		if severity(d) > severity(v.Decision) {
			v.Decision, v.Reason = d, reason
		}
		public = public || classify(a) == classPublic
		lan = lan || onLAN
	}
	if public && lan && v.Decision != Deny {
		// An ask about a LAN address names the host, and a grant opens the name: one that also
		// answers public is a page's own name pointed at the LAN, never a device to be allowed —
		// before a grant or after it, when a granted name starts answering both ways.
		v.Decision, v.Reason = Deny, ReasonMixed
	}
	if v.Decision == Allow {
		v.Addrs = addrs
	}
	return v
}

// judgeAddr decides about one address, and says whether it is on the LAN beyond this machine and
// the Docker host. host is what was asked for, which is what a grant names.
func (w *Wall) judgeAddr(ctx context.Context, r *rules, p *Proxy, host string, a netip.Addr, port int) (Decision, string, bool) {
	c := classify(a)
	local := c == classLoopback || w.isLocal(a)
	if local && (r.sealed[uint16(port)] || (p != nil && p.port == port)) {
		// Sealed on every address of this machine, not only on loopback: an API bound to all
		// interfaces is reached through the machine's own LAN or public address just as well.
		return Deny, ReasonSealedPort, false
	}
	switch c {
	case classMetadata:
		return Deny, ReasonMetadata, false
	case classMulticast:
		return Deny, ReasonMulticast, false
	case classReserved:
		return Deny, ReasonReserved, false
	}
	if local {
		if r.rewrite == "" && r.inServices(port) {
			return Allow, "", false
		}
		if r.rewrite != "" {
			// This machine is the browser's own container.
			return Deny, ReasonLoopback, false
		}
		d, reason := r.localSite(p, host, port, ReasonLoopback)
		return d, reason, false
	}
	if r.hostAddrs[a.Unmap()] || w.isGateway(ctx, r, a) {
		if r.sealed[uint16(port)] {
			// The installation's own doors are published on the Docker host too (the API on its
			// loopback, which the host's forwarding makes reachable): sealed there as well.
			return Deny, ReasonSealedPort, false
		}
		if r.inServices(port) {
			return Allow, "", false
		}
		d, reason := r.localSite(p, host, port, ReasonGateway)
		return d, reason, false
	}
	switch c {
	case classPrivate, classLinkLocal:
		for _, prefix := range r.lan {
			if prefix.Contains(a) {
				if p != nil && p.granted(host, port) {
					return Allow, "", true
				}
				return Ask, ReasonLAN, true
			}
		}
		if c == classLinkLocal {
			return Deny, ReasonLinkLocal, true
		}
		if r.lanAsk {
			if p != nil && p.granted(host, port) {
				return Allow, "", true
			}
			return Ask, ReasonPrivate, true
		}
		return Deny, ReasonPrivate, true
	}
	return Allow, "", false
}

// localSite decides about a port of this machine (natively) or of the Docker host (in a container)
// that is neither sealed nor in a services range, by the operator's local_sites.
func (r *rules) localSite(p *Proxy, host string, port int, reason string) (Decision, string) {
	switch r.local {
	case LocalAllow:
		return Allow, ""
	case LocalAsk:
		if p != nil && p.granted(host, port) {
			return Allow, ""
		}
		return Ask, reason
	}
	return Deny, reason
}

// The schemes a navigation may have. Everything else — file:, data:, blob:, javascript:, chrome:,
// chrome-extension:, devtools:, view-source:, filesystem: — never leaves as a navigation the agent
// or the operator's address bar asks for. data: and blob: carry no host for an origin list to check,
// which is how they walked around one (a page made them, the agent opened them); refusing them by
// scheme needs no host.
var navigableSchemes = map[string]bool{"http": true, "https": true}

// CheckNavigation judges a top-level navigation: one the agent asks for, the operator types into
// the address bar, or a page makes (a link, a redirect, a form) and the daemon intercepts. p is the
// browser's proxy. "about:blank" is always allowed. On top of the address rules, the operator's
// allowlist, when there is one, makes a host outside it an ask.
func (w *Wall) CheckNavigation(ctx context.Context, p *Proxy, raw string) Verdict {
	if strings.TrimSpace(raw) == "about:blank" {
		return Verdict{Decision: Allow, Host: "about:blank"}
	}
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || !navigableSchemes[strings.ToLower(u.Scheme)] || u.Host == "" {
		scheme := ""
		if u != nil {
			scheme = strings.ToLower(u.Scheme)
		}
		v := Verdict{Decision: Deny, Reason: ReasonScheme, Host: scheme + ":"}
		w.emit(p, v)
		return v
	}
	port := 80
	if strings.EqualFold(u.Scheme, "https") {
		port = 443
	}
	if ps := u.Port(); ps != "" {
		n, err := strconv.Atoi(ps)
		if err != nil {
			v := Verdict{Decision: Deny, Reason: ReasonBadTarget, Host: normalHost(u.Hostname())}
			w.emit(p, v)
			return v
		}
		port = n
	}
	v := w.judge(ctx, p, u.Hostname(), port)
	if v.Decision == Allow {
		r := w.current()
		// The allowlist names hosts the operator trusts on the web. The installation's own services
		// are not the web, and a grant is the operator's answer to exactly this ask.
		services := isLoopbackName(v.Host) && r.inServices(port)
		if !r.egressAllowed(v.Host) && !services && (p == nil || !p.granted(v.Host, port)) {
			v.Decision, v.Reason, v.Addrs = Ask, ReasonEgressAllow, nil
		}
	}
	w.emit(p, v)
	return v
}

// emit publishes v as an egress event, at most once a minute for the same browser, host, port and
// decision. The map is swept as it grows, so a page that asks for a thousand hosts costs a
// thousand entries for a minute and no more.
func (w *Wall) emit(p *Proxy, v Verdict) {
	if w.opts.Events == nil {
		return
	}
	browser := ""
	if p != nil {
		browser = p.browser
	}
	now := w.now()
	key := egressKey{browser, v.Host, v.Port, v.Decision}
	w.seenMu.Lock()
	if at, ok := w.seen[key]; ok && now.Sub(at) < time.Minute {
		w.seenMu.Unlock()
		return
	}
	if len(w.seen) > 4096 {
		for k, at := range w.seen {
			if now.Sub(at) >= time.Minute {
				delete(w.seen, k)
			}
		}
	}
	w.seen[key] = now
	w.seenMu.Unlock()
	w.opts.Events(Egress{Browser: browser, Host: v.Host, Port: v.Port, Decision: v.Decision, Reason: v.Reason, At: now.UnixMilli()})
}
