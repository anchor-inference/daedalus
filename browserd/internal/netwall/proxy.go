package netwall

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/netip"
	"strconv"
	"sync"
	"syscall"
	"time"
)

// The proxy's own limits. A page is allowed a great many connections (a news site opens a hundred),
// but not an unbounded number of tunnels held open by one browser.
const (
	maxTunnels  = 512
	dialTimeout = 10 * time.Second
	grantMax    = 24 * time.Hour
	halfClose   = 30 * time.Second
)

// BlockedHeader is set on every refusal the proxy answers, with the reason, so the daemon's own
// navigation code can tell the wall's refusal from a site's 403.
const BlockedHeader = "X-Browserd-Blocked"

// Proxy is one browser's way out: an HTTP proxy on 127.0.0.1 that Chromium is started against.
// It speaks CONNECT (https, wss, and ws, which Chromium also tunnels) and absolute-form requests
// (plain http). Every connection it makes goes through dial, which judges the destination and
// opens the socket on the address it judged.
type Proxy struct {
	wall    *Wall
	browser string
	ln      net.Listener
	port    int
	srv     *http.Server
	fwd     *httputil.ReverseProxy
	tr      *http.Transport
	slots   chan struct{}

	mu      sync.Mutex
	grants  map[string]time.Time
	tunnels map[net.Conn]bool
	closed  bool
}

// Unlistened is a proxy that listens nowhere and carries nothing: only the grants of a browser
// whose pages leave from another machine (the operator's Chrome), so CheckNavigation reads them
// as it reads a running proxy's. Its port is 0, which no destination has.
func (w *Wall) Unlistened(browser string) *Proxy {
	return &Proxy{wall: w, browser: browser, grants: map[string]time.Time{}, tunnels: map[net.Conn]bool{},
		tr: &http.Transport{}, srv: &http.Server{ReadHeaderTimeout: time.Second}}
}

// Listen starts a proxy for one browser. browser is the daemon's id for it; it is the browser_id
// of the egress events the proxy publishes. Start the browser with ChromiumArgs(p.Addr()), and
// Close the proxy when the browser has exited.
func (w *Wall) Listen(browser string) (*Proxy, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	p := &Proxy{wall: w, browser: browser, ln: ln, port: ln.Addr().(*net.TCPAddr).Port, slots: make(chan struct{}, maxTunnels), grants: map[string]time.Time{}, tunnels: map[net.Conn]bool{}}
	p.tr = &http.Transport{
		// Never the environment's proxy: the wall is the way out, not a hop before another one.
		Proxy: nil,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			host, portText, err := net.SplitHostPort(addr)
			if err != nil {
				return nil, err
			}
			port, _ := strconv.Atoi(portText)
			conn, v := p.dial(ctx, host, port)
			if conn == nil {
				if v.Allowed() {
					return nil, fmt.Errorf("could not reach %s", addr)
				}
				return nil, &blockedError{v}
			}
			return conn, nil
		},
		DisableCompression:    true, // the page's bytes pass through as the server sent them
		MaxIdleConnsPerHost:   8,
		IdleConnTimeout:       30 * time.Second,
		TLSHandshakeTimeout:   dialTimeout,
		ExpectContinueTimeout: time.Second,
	}
	p.fwd = &httputil.ReverseProxy{
		// Rewrite rather than Director: it adds no X-Forwarded-For, so a site never learns the
		// browser's addresses from the proxy.
		Rewrite: func(r *httputil.ProxyRequest) {
			r.Out.Header.Del("Proxy-Connection")
			r.Out.Header.Del("Proxy-Authorization")
		},
		Transport:     p.tr,
		FlushInterval: -1, // server-sent events and long polls reach the page as they come
		ErrorHandler: func(rw http.ResponseWriter, r *http.Request, err error) {
			var blocked *blockedError
			if errors.As(err, &blocked) {
				refuse(rw, blocked.v)
				return
			}
			http.Error(rw, "the browser's proxy could not reach "+r.URL.Host, http.StatusBadGateway)
		},
	}
	p.srv = &http.Server{Handler: p, ReadHeaderTimeout: 30 * time.Second, IdleTimeout: 2 * time.Minute, ErrorLog: nil}
	w.proxyMu.Lock()
	w.proxies[p] = true
	w.proxyMu.Unlock()
	go p.srv.Serve(ln)
	return p, nil
}

// Addr is where the proxy listens, as Chromium's --proxy-server wants it.
func (p *Proxy) Addr() string { return p.ln.Addr().String() }

// Port is the proxy's port. It is sealed: a page cannot use the proxy to reach the proxy.
func (p *Proxy) Port() int { return p.port }

// Close stops the proxy and ends every connection it holds.
func (p *Proxy) Close() error {
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return nil
	}
	p.closed = true
	for c := range p.tunnels {
		c.Close()
	}
	p.mu.Unlock()
	p.wall.proxyMu.Lock()
	delete(p.wall.proxies, p)
	p.wall.proxyMu.Unlock()
	p.tr.CloseIdleConnections()
	return p.srv.Close()
}

// Grant opens an asked destination (a loopback port natively, a listed LAN address, a host outside
// the allowlist) for this browser until until, at most a day. It is the operator's answer to an ask,
// carried by the host; the daemon never grants on its own.
func (p *Proxy) Grant(host string, port int, until time.Time) {
	if max := p.wall.now().Add(grantMax); until.After(max) {
		until = max
	}
	p.mu.Lock()
	p.grants[grantKey(host, port)] = until
	p.mu.Unlock()
}

// Revoke closes a granted destination again.
func (p *Proxy) Revoke(host string, port int) {
	p.mu.Lock()
	delete(p.grants, grantKey(host, port))
	p.mu.Unlock()
	p.tr.CloseIdleConnections()
}

func grantKey(host string, port int) string {
	return net.JoinHostPort(normalHost(host), strconv.Itoa(port))
}

func (p *Proxy) granted(host string, port int) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	until, ok := p.grants[grantKey(host, port)]
	if ok && p.wall.now().After(until) {
		delete(p.grants, grantKey(host, port))
		return false
	}
	return ok
}

type blockedError struct{ v Verdict }

func (e *blockedError) Error() string { return e.v.Message() }

// refuse answers a refused request. Chromium shows the body for a plain http page; for a CONNECT it
// shows its own tunnel error, and the header is what the daemon reads.
func refuse(rw http.ResponseWriter, v Verdict) {
	rw.Header().Set(BlockedHeader, v.Reason)
	rw.Header().Set("Content-Type", "text/plain; charset=utf-8")
	rw.Header().Set("Cache-Control", "no-store")
	rw.WriteHeader(http.StatusForbidden)
	io.WriteString(rw, "Blocked by the browser's network wall: "+v.Message()+".\n")
}

// dial is the one place the proxy opens a connection. It judges the destination, then dials only
// the addresses it judged, and the socket itself checks that the address it connects to is one of
// them: nothing between the judgement and the connect can change where the connection goes.
func (p *Proxy) dial(ctx context.Context, host string, port int) (net.Conn, Verdict) {
	v := p.wall.judge(ctx, p, host, port)
	p.wall.emit(p, v)
	if !v.Allowed() {
		return nil, v
	}
	for _, a := range v.Addrs {
		target := netip.AddrPortFrom(a, uint16(port))
		if p.wall.redirect != nil {
			target = p.wall.redirect(target)
		}
		want := target
		d := net.Dialer{Timeout: dialTimeout, Control: func(network, address string, _ syscall.RawConn) error {
			got, err := netip.ParseAddrPort(address)
			if err != nil || got.Addr().Unmap() != want.Addr().Unmap() || got.Port() != want.Port() {
				return fmt.Errorf("refusing to connect to %s: judged %s", address, want)
			}
			return nil
		}}
		if conn, err := d.DialContext(ctx, "tcp", target.String()); err == nil {
			return conn, v
		}
	}
	// Reachable by the rules and not by the network: the page gets the proxy's 502, not a refusal.
	return nil, Verdict{Decision: Allow, Host: v.Host, Port: port}
}

// ServeHTTP is the proxy's front door.
func (p *Proxy) ServeHTTP(rw http.ResponseWriter, r *http.Request) {
	if ap, err := netip.ParseAddrPort(r.RemoteAddr); err != nil || !ap.Addr().Unmap().IsLoopback() {
		http.Error(rw, "the browser's proxy serves this machine only", http.StatusForbidden)
		return
	}
	if r.Method == http.MethodConnect {
		p.tunnel(rw, r)
		return
	}
	// A proxy request names its destination in full. A request that does not is someone using the
	// proxy's port as a web server, which it is not.
	if !r.URL.IsAbs() || r.URL.Scheme != "http" || r.URL.Host == "" {
		http.Error(rw, "the browser's proxy takes proxy requests only", http.StatusBadRequest)
		return
	}
	p.fwd.ServeHTTP(rw, r)
}

// tunnel serves a CONNECT: the destination is judged and dialled, then the two sockets are joined.
// The proxy never sees inside: TLS goes end to end between Chromium and the site.
func (p *Proxy) tunnel(rw http.ResponseWriter, r *http.Request) {
	host, portText, err := net.SplitHostPort(r.Host)
	port, perr := strconv.Atoi(portText)
	if err != nil || perr != nil {
		refuse(rw, Verdict{Decision: Deny, Reason: ReasonBadTarget, Host: normalHost(r.Host)})
		return
	}
	select {
	case p.slots <- struct{}{}:
	default:
		http.Error(rw, "the browser has too many connections open", http.StatusServiceUnavailable)
		return
	}
	defer func() { <-p.slots }()
	upstream, v := p.dial(r.Context(), host, port)
	if upstream == nil {
		if v.Allowed() {
			http.Error(rw, "the browser's proxy could not reach "+r.Host, http.StatusBadGateway)
			return
		}
		refuse(rw, v)
		return
	}
	hj, ok := rw.(http.Hijacker)
	if !ok {
		upstream.Close()
		http.Error(rw, "no tunnel on this connection", http.StatusInternalServerError)
		return
	}
	client, buf, err := hj.Hijack()
	if err != nil {
		upstream.Close()
		return
	}
	if !p.hold(client, upstream) {
		client.Close()
		upstream.Close()
		return
	}
	defer p.release(client, upstream)
	if _, err := client.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n")); err != nil {
		return
	}
	splice(client, buf.Reader, upstream)
}

func (p *Proxy) hold(conns ...net.Conn) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		return false
	}
	for _, c := range conns {
		p.tunnels[c] = true
	}
	return true
}

func (p *Proxy) release(conns ...net.Conn) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, c := range conns {
		c.Close()
		delete(p.tunnels, c)
	}
}

// splice copies both ways until both sides have finished, then ends both. Bytes the client sent
// after its CONNECT, already read into the server's buffer, go first. Once one direction ends the
// other gets halfClose to finish: a peer that half-closes and then never speaks again would
// otherwise hold the tunnel, and its slot, for ever.
func splice(client net.Conn, buffered *bufio.Reader, upstream net.Conn) {
	done := make(chan struct{}, 2)
	go func() {
		if n := buffered.Buffered(); n > 0 {
			head, _ := buffered.Peek(n)
			upstream.Write(head)
		}
		io.Copy(upstream, client)
		closeWrite(upstream)
		done <- struct{}{}
	}()
	go func() {
		io.Copy(client, upstream)
		closeWrite(client)
		done <- struct{}{}
	}()
	<-done
	select {
	case <-done:
	case <-time.After(halfClose):
		client.Close()
		upstream.Close()
		<-done
	}
}

func closeWrite(c net.Conn) {
	if tc, ok := c.(interface{ CloseWrite() error }); ok {
		tc.CloseWrite()
		return
	}
	c.Close()
}
