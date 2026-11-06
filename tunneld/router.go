package main

// The router: the one thing a tunnel ever reaches.
//
// A tunnel maps one address to one place, and an installation has two things to show through it with
// different risks: /a/, where strangers' agents reach the guest door's front, and /s/, where shared
// services run pages an agent wrote. This is the place they share. It knows two ports and no others,
// and the app's port is not among them: nothing here can be talked into reaching the API, the app or a
// terminal, because no code in this program has heard of them. Everything else gets a 404 from here.
//
// The two share an origin, which is the thing to be careful of. A page under /s/ runs a script on the
// same origin as the door, and could call it from a visitor's browser. What protects the door is that
// it has nothing a browser holds: no cookies, tokens only in an agent's hands, invites only in links.
// Answers under /a/ are never HTML, carry a sandbox and cannot be framed into anything useful. What is
// added here: a request to /a/ made by a page is refused, because the door's clients are programs and
// browsers only ever navigate to it. Fetch metadata says which, and a tunnel is always https, where
// browsers send it. So a page on /s/ cannot spend an invite, or push guesses at the door from visitors'
// addresses. Cookies are dropped on the way to /a/: a page can set one with path=/, and it is no
// business of the door's. Service workers are held to their own folder, because the host drops
// Service-Worker-Allowed from a service's answer and nothing here serves a script at the root.
//
// Who the visitor is comes from the tunnel's own word for it, which depends on the provider. Whatever
// the visitor wrote in a header of that name is gone before anything reads it. What goes down is one
// X-Forwarded-For with one address, and ThroughHeader, which the host reads as "this came from outside
// however local its peer looks". A visitor can add that header and only ever makes themselves less
// trusted; nobody can remove it, because this code writes it on every request.

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/netip"
	"net/url"
	"strings"
	"sync"
	"time"
)

const (
	// ThroughHeader marks a request as having come through a tunnel. The host's rule is that its
	// presence means not local, whatever the socket's peer says (daedalus/host/reveal.py).
	ThroughHeader = "X-Daedalus-Through"

	// maxDoorBody is the front's own limit, so an answer comes from here rather than a cut connection.
	maxDoorBody = 64 << 10
	// doorBodySeconds is how long a door's short JSON body may take to arrive.
	doorBodySeconds = 30
	// inFlightLimit and perVisitorLimit are what one router will hold open at once, past which it says
	// so. The front has its own bounds for /a/; a service is usually a dev server and has none.
	inFlightLimit   = 512
	perVisitorLimit = 64

	notHere = "Nothing here. This address serves agent links and shared services.\n"
)

// strippedHeaders never reach a door. The X-Forwarded family and Forwarded go by themselves, because
// ReverseProxy's Rewrite removes them, and they are listed for the reader and for a change of mechanism.
var strippedHeaders = []string{
	"Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-Port", "X-Forwarded-Prefix", "X-Real-Ip", "True-Client-Ip", "X-Client-Ip",
	"Cf-Connecting-Ip", "Cf-Ipcountry", "Cf-Ray", "Cf-Visitor", "Cdn-Loop", "X-Daedalus-Token", "X-Guest-Client", "X-Share-Key-Internal", "Tailscale-Ingress-Target",
}

type visitorKey struct{}

// VisitorOf reads a visitor's address from a request as this provider states it; "" when it cannot.
type VisitorOf func(*http.Request) string

// Router is an http.Handler serving the two prefixes. Probe is a path only this process knows, by
// which it checks that its own public address answers.
type Router struct {
	Door     *url.URL
	Services *url.URL
	Probe    string
	Visitor  VisitorOf

	door     *httputil.ReverseProxy
	services *httputil.ReverseProxy
	mu       sync.Mutex
	open     map[string]int
	total    int
}

// NewRouter is a router for the front on one loopback port and the services door on another.
func NewRouter(doorPort, servicesPort, probe string, visitor VisitorOf) *Router {
	rt := &Router{
		Door:     &url.URL{Scheme: "http", Host: net.JoinHostPort("127.0.0.1", doorPort)},
		Services: &url.URL{Scheme: "http", Host: net.JoinHostPort("127.0.0.1", servicesPort)},
		Probe:    probe,
		Visitor:  visitor,
		open:     map[string]int{},
	}
	rt.door = rt.proxy(rt.Door)
	rt.services = rt.proxy(rt.Services)
	return rt
}

func (rt *Router) proxy(target *url.URL) *httputil.ReverseProxy {
	transport := &http.Transport{
		Proxy:                 nil, // an operator's HTTP_PROXY must not carry visitors anywhere
		DisableCompression:    true,
		MaxIdleConns:          16,
		IdleConnTimeout:       30 * time.Second,
		ResponseHeaderTimeout: 0, // a service may take its time, and a poll holds for half a minute
	}
	return &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			visitor, _ := pr.In.Context().Value(visitorKey{}).(string)
			pr.SetURL(target)
			pr.Out.Host = pr.In.Host // the name the visitor used, which a service builds its links from
			for _, name := range strippedHeaders {
				pr.Out.Header.Del(name)
			}
			pr.Out.Header.Del(ThroughHeader)
			pr.Out.Header.Set(ThroughHeader, "tunnel")
			pr.Out.Header.Set("X-Forwarded-Proto", "https")
			if visitor != "" {
				pr.Out.Header.Set("X-Forwarded-For", visitor)
			}
		},
		ModifyResponse: func(answer *http.Response) error {
			answer.Header.Del("Server")
			return nil
		},
		Transport:     transport,
		FlushInterval: -1, // an event stream goes out as it is written
		ErrorLog:      log.New(io.Discard, "", 0),
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			// Said in two lines and never with the error: it names local ports.
			if errors.Is(err, context.Canceled) {
				return
			}
			w.Header().Set("Retry-After", "30")
			http.Error(w, "This address's door is closed for the moment.", http.StatusBadGateway)
		},
	}
}

// cleanPath refuses what could be read as another path further on. Go's server does not normalise a
// path for a handler, and the host decodes once more. Both the decoded and the raw form are looked at
// because %2e%2e and .. are the same thing a step later, and a backslash is a slash to some servers.
func cleanPath(r *http.Request) bool {
	for _, path := range []string{r.URL.Path, r.URL.EscapedPath()} {
		lower := strings.ToLower(path)
		if strings.Contains(path, "\\") || strings.Contains(path, "//") || strings.Contains(path, "\x00") || strings.Contains(lower, "%5c") || strings.Contains(lower, "%2f") || strings.Contains(lower, "%00") {
			return false
		}
		for _, segment := range strings.Split(lower, "/") {
			switch segment {
			case ".", "..", "%2e", "%2e%2e", ".%2e", "%2e.":
				return false
			}
		}
	}
	return true
}

// madeByAPage says a browser page issued this, rather than a program or a person following a link.
func madeByAPage(r *http.Request) bool {
	if r.Header.Get("Origin") != "" {
		return true
	}
	site := strings.ToLower(r.Header.Get("Sec-Fetch-Site"))
	if site == "" || site == "none" {
		return false
	}
	// A person clicking a link in a chat is cross-site and navigating: they are reading the page.
	return !(r.Method == http.MethodGet && r.Header.Get("Sec-Fetch-Mode") == "navigate" && r.Header.Get("Sec-Fetch-Dest") == "document")
}

func refuse(w http.ResponseWriter, status int, text string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	io.WriteString(w, text)
}

func (rt *Router) hold(visitor string) bool {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	if rt.total >= inFlightLimit || rt.open[visitor] >= perVisitorLimit {
		return false
	}
	rt.total++
	rt.open[visitor]++
	return true
}

func (rt *Router) release(visitor string) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	rt.total--
	if rt.open[visitor]--; rt.open[visitor] <= 0 {
		delete(rt.open, visitor)
	}
}

func (rt *Router) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if rt.Probe != "" && r.URL.Path == rt.Probe {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.URL.Path == "/robots.txt" {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		io.WriteString(w, "User-agent: *\nDisallow: /\n")
		return
	}
	path := r.URL.Path
	toDoor, toServices := strings.HasPrefix(path, "/a/"), strings.HasPrefix(path, "/s/")
	if (!toDoor && !toServices) || !cleanPath(r) {
		refuse(w, http.StatusNotFound, notHere)
		return
	}
	visitor := ""
	if rt.Visitor != nil {
		visitor = rt.Visitor(r)
	}
	if !rt.hold(visitor) {
		w.Header().Set("Retry-After", "10")
		refuse(w, http.StatusServiceUnavailable, "Too many connections.\n")
		return
	}
	defer rt.release(visitor)
	r = r.WithContext(context.WithValue(r.Context(), visitorKey{}, visitor))
	if toServices {
		rt.services.ServeHTTP(w, r)
		return
	}
	if madeByAPage(r) {
		refuse(w, http.StatusForbidden, "The door takes no requests from web pages.\n")
		return
	}
	if r.Header.Get("Upgrade") != "" {
		refuse(w, http.StatusBadRequest, "The door takes plain requests.\n")
		return
	}
	r.Header.Del("Cookie")
	if !bufferBody(w, r) {
		return
	}
	rt.door.ServeHTTP(w, r)
}

// bufferBody reads a door's body whole, as nginx does on a server. The front takes a Content-Length and
// refuses Transfer-Encoding, so that one request cannot be read as two, and an HTTP/2 client that
// declares no length arrives at this end as chunked. Bounded in size and in time.
func bufferBody(w http.ResponseWriter, r *http.Request) bool {
	if r.Body == nil || r.Body == http.NoBody {
		return true
	}
	if r.ContentLength > maxDoorBody {
		refuse(w, http.StatusRequestEntityTooLarge, "Too long.\n")
		return false
	}
	_ = http.NewResponseController(w).SetReadDeadline(time.Now().Add(doorBodySeconds * time.Second))
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxDoorBody))
	if err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			refuse(w, http.StatusRequestEntityTooLarge, "Too long.\n")
		} else {
			refuse(w, http.StatusRequestTimeout, "The request took too long to arrive.\n")
		}
		return false
	}
	_ = http.NewResponseController(w).SetReadDeadline(time.Time{})
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	r.TransferEncoding = nil
	r.Header.Del("Transfer-Encoding")
	return true
}

// Server wraps the router for a listener. No access log exists anywhere here: an invite is in a path.
func (rt *Router) Server(connContext func(context.Context, net.Conn) context.Context) *http.Server {
	return &http.Server{
		Handler:           rt,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    16 << 10,
		ErrorLog:          log.New(io.Discard, "", 0),
		ConnContext:       connContext,
	}
}

// edgeHeader reads a visitor from a header only the tunnel's edge writes.
func edgeHeader(name string) VisitorOf {
	return func(r *http.Request) string {
		address, err := netip.ParseAddr(strings.TrimSpace(r.Header.Get(name)))
		if err != nil {
			return ""
		}
		return address.Unmap().String()
	}
}

// lastPublicForwarded reads X-Forwarded-For from the right, where the proxies we know wrote it, and
// stops at the first address that is not one of theirs. Anything left of that the visitor could have
// written. A proxy on the operator's own VPS adds itself as 127.0.0.1 or a private address.
func lastPublicForwarded(r *http.Request) string {
	entries := strings.Split(strings.Join(r.Header.Values("X-Forwarded-For"), ","), ",")
	for i := len(entries) - 1; i >= 0; i-- {
		address, err := netip.ParseAddr(strings.TrimSpace(entries[i]))
		if err != nil {
			continue
		}
		address = address.Unmap()
		if address.IsLoopback() || address.IsPrivate() || address.IsLinkLocalUnicast() {
			continue
		}
		return address.String()
	}
	return ""
}
