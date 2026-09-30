package netwall

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// These tests put a real Chromium behind the wall and try, from inside its pages, every way out the
// wall is there to close. They run when BROWSERD_TEST_CHROMIUM names a Chromium, and are skipped
// otherwise: the gate image and the browser image have one, a developer's checkout may not. They
// touch no network: every "public" host is a loopback fixture the fake resolver gives a public
// address to.
//
//	BROWSERD_TEST_CHROMIUM=/path/to/chrome go test -run Chromium ./internal/netwall/

type siteLog struct {
	mu   sync.Mutex
	hits []string
}

func (l *siteLog) add(s string) {
	l.mu.Lock()
	l.hits = append(l.hits, s)
	l.mu.Unlock()
}

func (l *siteLog) has(prefix string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, h := range l.hits {
		if strings.HasPrefix(h, prefix) {
			return true
		}
	}
	return false
}

func (l *siteLog) count() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.hits)
}

// websocketAccept answers a WebSocket handshake by hand and sends one text frame: enough for a page
// to see its socket open through the proxy and carry a message.
func websocketAccept(rw http.ResponseWriter, r *http.Request) {
	sum := sha1.Sum([]byte(r.Header.Get("Sec-WebSocket-Key") + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	conn, buf, err := rw.(http.Hijacker).Hijack()
	if err != nil {
		return
	}
	defer conn.Close()
	fmt.Fprintf(buf, "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n", base64.StdEncoding.EncodeToString(sum[:]))
	msg := "through the wall"
	buf.Write([]byte{0x81, byte(len(msg))})
	buf.WriteString(msg)
	buf.Flush()
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	bufio.NewReader(conn).ReadByte()
}

const pageHTML = `<!doctype html><title>%s</title><p>%s</p>`

const workerPage = `<!doctype html><title>worker</title><script>
window.workerResult = (async () => {
  const reg = await navigator.serviceWorker.register('/sw.js');
  await navigator.serviceWorker.ready;
  if (!navigator.serviceWorker.controller) {
    await new Promise(r => navigator.serviceWorker.addEventListener('controllerchange', r, {once: true}));
  }
  const r = await fetch('/via-worker');
  return await r.text();
})().catch(e => 'error: ' + e);
</script>`

// The worker answers /via-worker by fetching from other hosts itself: one the wall allows, and one
// it must refuse.
const workerScript = `self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (!e.request.url.endsWith('/via-worker')) return;
  e.respondWith((async () => {
    const out = [];
    for (const u of ['http://public.test/from-worker', 'http://private.test/from-worker']) {
      try { await fetch(u, {mode: 'no-cors'}); out.push('ok'); } catch (err) { out.push('refused'); }
    }
    return new Response(out.join(','));
  })());
});`

func TestChromiumBehindTheWall(t *testing.T) {
	binary := os.Getenv("BROWSERD_TEST_CHROMIUM")
	if binary == "" {
		t.Skip("BROWSERD_TEST_CHROMIUM names no Chromium")
	}
	public, private, sealed, services := &siteLog{}, &siteLog{}, &siteLog{}, &siteLog{}
	site := func(log *siteLog, title string) *httptest.Server {
		srv := httptest.NewServer(http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
			log.add(r.URL.Path)
			switch r.URL.Path {
			case "/ws":
				websocketAccept(rw, r)
			case "/worker.html":
				fmt.Fprint(rw, workerPage)
			case "/sw.js":
				rw.Header().Set("Content-Type", "text/javascript")
				fmt.Fprint(rw, workerScript)
			default:
				rw.Header().Set("Content-Type", "text/html")
				fmt.Fprintf(rw, pageHTML, title, title)
			}
		}))
		t.Cleanup(srv.Close)
		return srv
	}
	publicSrv, privateSrv, sealedSrv, servicesSrv := site(public, "public"), site(private, "private"), site(sealed, "the agent's API"), site(services, "a service")
	port := func(s *httptest.Server) int { return int(netip.MustParseAddrPort(s.Listener.Addr().String()).Port()) }

	// A STUN server on this machine: any packet it receives is WebRTC's UDP leaving without the proxy.
	stun, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer stun.Close()
	var stunPackets atomic.Int64
	go func() {
		buf := make([]byte, 2048)
		for {
			if _, _, err := stun.ReadFrom(buf); err != nil {
				return
			}
			stunPackets.Add(1)
		}
	}()

	var egress []Egress
	var egressMu sync.Mutex
	res := &fakeResolver{answers: map[string][]string{
		"public.test":  {"203.0.113.10"},
		"private.test": {"10.0.0.1"},
		"rebind.test":  {"172.20.7.7"},
	}}
	w := New(Options{Resolver: res, LocalAddrs: func() []netip.Addr { return nil }, Events: func(e Egress) {
		egressMu.Lock()
		egress = append(egress, e)
		egressMu.Unlock()
	}})
	if err := w.Configure(Config{SealedPorts: []int{port(sealedSrv)}, ServicesPorts: [][2]int{{port(servicesSrv), port(servicesSrv)}}, LocalSites: LocalAsk}); err != nil {
		t.Fatal(err)
	}
	routes := map[netip.AddrPort]netip.AddrPort{
		netip.MustParseAddrPort("203.0.113.10:80"): netip.MustParseAddrPort(publicSrv.Listener.Addr().String()),
		netip.MustParseAddrPort("10.0.0.1:80"):     netip.MustParseAddrPort(privateSrv.Listener.Addr().String()),
		netip.MustParseAddrPort("172.20.7.7:80"):   netip.MustParseAddrPort(privateSrv.Listener.Addr().String()),
	}
	w.redirect = func(a netip.AddrPort) netip.AddrPort {
		if to, ok := routes[a]; ok {
			return to
		}
		return a
	}
	proxy, err := w.Listen("b1")
	if err != nil {
		t.Fatal(err)
	}
	defer proxy.Close()

	b := startChromium(t, binary, ChromiumArgs(proxy.Addr())...)
	b.openPage()
	var text, title string

	t.Run("a public page loads through the proxy", func(t *testing.T) {
		b.navigate("http://public.test/")
		b.mustEval("document.title", &title)
		if title != "public" || !public.has("/") {
			t.Fatalf("title %q, public site saw %v", title, public.hits)
		}
	})

	t.Run("the installation's services open and its own doors do not", func(t *testing.T) {
		b.navigate(fmt.Sprintf("http://127.0.0.1:%d/", port(servicesSrv)))
		b.mustEval("document.title", &title)
		if title != "a service" {
			t.Fatalf("the services range: %q", title)
		}
		b.navigate(fmt.Sprintf("http://127.0.0.1:%d/api/sessions", port(sealedSrv)))
		b.mustEval("document.body.innerText", &text)
		if !strings.Contains(text, "network wall") || sealed.count() != 0 {
			t.Fatalf("a sealed port: %q, reached %d times", text, sealed.count())
		}
	})

	t.Run("the LAN, a rebinding name and cloud metadata are refused", func(t *testing.T) {
		for _, u := range []string{"http://private.test/", "http://rebind.test/", "http://10.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://keyproxy:3200/v1/models"} {
			b.navigate(u)
			b.mustEval("document.body.innerText", &text)
			if !strings.Contains(text, "network wall") {
				t.Errorf("%s: %q", u, text)
			}
		}
		if private.count() != 0 {
			t.Fatalf("the LAN fixture was reached: %v", private.hits)
		}
	})

	t.Run("a page's own requests meet the same wall", func(t *testing.T) {
		b.navigate("http://public.test/")
		var results []string
		b.mustEval(fmt.Sprintf(`Promise.all([
			'http://127.0.0.1:%d/api/sessions', 'http://localhost:%d/', 'http://private.test/x', 'http://169.254.169.254/',
			'http://keyproxy:3200/v1/models', 'http://public.test/fetched'
		].map(u => fetch(u, {mode: 'no-cors'}).then(r => r.type === 'opaque' || r.ok ? 'ok' : 'status ' + r.status, () => 'refused')))`, port(sealedSrv), port(sealedSrv)), &results)
		// no-cors hides the status, and a refused plain-http request is the wall's 403 anyway; what
		// matters is which server was reached.
		if sealed.count() != 0 || private.count() != 0 || !public.has("/fetched") {
			t.Fatalf("results %v; sealed %d, private %d, public %v", results, sealed.count(), private.count(), public.hits)
		}
	})

	t.Run("a WebSocket goes through the proxy", func(t *testing.T) {
		b.navigate("http://public.test/")
		b.mustEval(`new Promise(ok => { const s = new WebSocket('ws://public.test/ws'); s.onmessage = e => ok(e.data); s.onerror = () => ok('error'); setTimeout(() => ok('timeout'), 5000); })`, &text)
		if text != "through the wall" || !public.has("/ws") {
			t.Fatalf("websocket: %q", text)
		}
		b.mustEval(`new Promise(ok => { const s = new WebSocket('ws://private.test/ws'); s.onopen = () => ok('open'); s.onerror = () => ok('refused'); setTimeout(() => ok('timeout'), 5000); })`, &text)
		if text != "refused" || private.count() != 0 {
			t.Fatalf("a WebSocket into the LAN: %q", text)
		}
	})

	t.Run("a service worker's fetches go through the proxy", func(t *testing.T) {
		// A worker needs a secure context; the services range on loopback is one.
		b.navigate(fmt.Sprintf("http://localhost:%d/worker.html", port(servicesSrv)))
		b.mustEval("window.workerResult", &text)
		// A refused plain-http request is the wall's 403, which a no-cors fetch sees as an opaque
		// answer like any other; what counts is which server the worker reached.
		if strings.HasPrefix(text, "error") || !public.has("/from-worker") || private.count() != 0 {
			t.Fatalf("worker: %q; public %v; private %v", text, public.hits, private.hits)
		}
	})

	t.Run("a page cannot navigate the tab to file: or data:", func(t *testing.T) {
		b.navigate("http://public.test/")
		for _, target := range []string{"file:///etc/passwd", "data:text/html,<title>data</title>"} {
			b.mustEval(fmt.Sprintf(`new Promise(ok => { location.href = %q; setTimeout(() => ok(location.href), 1500); })`, target), &text)
			if !strings.HasPrefix(text, "http://public.test/") {
				t.Errorf("the page reached %q", text)
			}
		}
	})

	t.Run("WebRTC sends no UDP and resolves no name", func(t *testing.T) {
		b.navigate("http://public.test/")
		var candidates []string
		b.mustEval(fmt.Sprintf(`new Promise(ok => {
			const seen = [];
			const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:127.0.0.1:%d'}, {urls: 'stun:stun.public.test:3478'}]});
			pc.createDataChannel('x');
			pc.onicecandidate = e => { if (e.candidate) seen.push(e.candidate.candidate); else ok(seen); };
			pc.createOffer().then(o => pc.setLocalDescription(o));
			setTimeout(() => ok(seen), 5000);
		})`, stun.LocalAddr().(*net.UDPAddr).Port), &candidates)
		time.Sleep(500 * time.Millisecond)
		for _, c := range candidates {
			if strings.Contains(strings.ToLower(c), " udp ") {
				t.Errorf("a UDP candidate: %s", c)
			}
		}
		if n := stunPackets.Load(); n != 0 {
			t.Fatalf("%d STUN packets left the browser without the proxy", n)
		}
	})

	t.Run("the egress log names every host the pages asked for", func(t *testing.T) {
		egressMu.Lock()
		defer egressMu.Unlock()
		decided := map[string]Decision{}
		for _, e := range egress {
			decided[e.Host] = e.Decision
		}
		for host, want := range map[string]Decision{"public.test": Allow, "private.test": Deny, "rebind.test": Deny, "169.254.169.254": Deny, "keyproxy": Deny} {
			if decided[host] != want {
				t.Errorf("%s: %q, want %q (log %v)", host, decided[host], want, decided)
			}
		}
		// The resolver was asked for page hosts only by the proxy; a STUN server's name never
		// reached it, because Chromium resolved nothing itself.
		res.mu.Lock()
		defer res.mu.Unlock()
		for _, asked := range res.asked {
			if strings.HasPrefix(asked, "stun.") {
				t.Errorf("a STUN name was resolved: %v", res.asked)
			}
		}
		t.Logf("hosts the browser asked for: %v", decided)
	})
}
