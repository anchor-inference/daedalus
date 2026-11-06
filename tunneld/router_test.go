package main

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

// upstream is a door or a services door that notes what reached it.
type upstream struct {
	*httptest.Server
	mu   sync.Mutex
	seen []*http.Request
	body []string
}

func newUpstream(t *testing.T) *upstream {
	t.Helper()
	up := &upstream{}
	up.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		up.mu.Lock()
		up.seen = append(up.seen, r.Clone(context.Background()))
		up.body = append(up.body, string(body))
		up.mu.Unlock()
		w.Header().Set("Server", "uvicorn")
		fmt.Fprint(w, "answered")
	}))
	t.Cleanup(up.Close)
	return up
}

func (up *upstream) last() *http.Request {
	up.mu.Lock()
	defer up.mu.Unlock()
	if len(up.seen) == 0 {
		return nil
	}
	return up.seen[len(up.seen)-1]
}

func (up *upstream) count() int {
	up.mu.Lock()
	defer up.mu.Unlock()
	return len(up.seen)
}

func portOf(t *testing.T, server *httptest.Server) string {
	t.Helper()
	parsed, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	return parsed.Port()
}

type rig struct {
	door, services *upstream
	front          *httptest.Server
}

func newRig(t *testing.T, visitor VisitorOf) *rig {
	t.Helper()
	r := &rig{door: newUpstream(t), services: newUpstream(t)}
	rt := NewRouter(portOf(t, r.door.Server), portOf(t, r.services.Server), "/.well-known/daedalus-probe", visitor)
	r.front = httptest.NewServer(rt)
	t.Cleanup(r.front.Close)
	return r
}

func (r *rig) ask(t *testing.T, method, path string, header http.Header, body io.Reader) *http.Response {
	t.Helper()
	request, err := http.NewRequest(method, r.front.URL, body)
	if err != nil {
		t.Fatal(err)
	}
	request.URL.Opaque = path // sent exactly as written, the way a hostile client would
	if header != nil {
		request.Header = header
	}
	client := http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	answer, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { answer.Body.Close() })
	return answer
}

func TestOnlyTheTwoPrefixesReachAnything(t *testing.T) {
	r := newRig(t, nil)
	for _, path := range []string{
		"/", "/app", "/app/", "/api", "/api/status", "/api/auth/me", "/api/cli/pair/start", "/ws/terminal", "/pages/x/", "/c/slug", "/healthz", "/a", "/s",
		"/admin", "/a.txt", "/s.txt", "/ab/c", "/sx/y", "/favicon.ico", "/.well-known/daedalus-other", "/API/status",
	} {
		answer := r.ask(t, http.MethodGet, path, nil, nil)
		if answer.StatusCode != http.StatusNotFound {
			t.Errorf("%s answered %d", path, answer.StatusCode)
		}
	}
	if r.door.count()+r.services.count() != 0 {
		t.Fatalf("a refused path reached a door: %d", r.door.count()+r.services.count())
	}
}

func TestAPathCannotClimbOutOfItsPrefix(t *testing.T) {
	r := newRig(t, nil)
	for _, path := range []string{
		"/s/../api/status", "/a/../api/status", "/s/%2e%2e/api/status", "/s/x/..%2f..%2fapi", "/s/x/%2E%2E/%2E%2E/api", "/a/.%2e/api", "/s/%2e./api",
		"/s/x/../../api", "/s//api", "/s/x\\..\\api", "/s/x%5c..%5capi", "/s/x%2fapi", "/s/x%00/api", "/a/./x",
	} {
		answer := r.ask(t, http.MethodGet, path, nil, nil)
		if answer.StatusCode != http.StatusNotFound {
			t.Errorf("%s answered %d", path, answer.StatusCode)
		}
	}
	if r.door.count()+r.services.count() != 0 {
		t.Fatal("a path out of its prefix reached a door")
	}
}

func TestEachPrefixGoesToItsOwnDoor(t *testing.T) {
	r := newRig(t, nil)
	if answer := r.ask(t, http.MethodGet, "/a/invite", nil, nil); answer.StatusCode != http.StatusOK || answer.Header.Get("Server") != "" {
		t.Fatalf("door: %d, server %q", answer.StatusCode, answer.Header.Get("Server"))
	}
	if r.door.last().URL.Path != "/a/invite" || r.services.count() != 0 {
		t.Fatal("/a/ went to the wrong place")
	}
	if answer := r.ask(t, http.MethodGet, "/s/slug/page?x=1", nil, nil); answer.StatusCode != http.StatusOK {
		t.Fatalf("services: %d", answer.StatusCode)
	}
	if got := r.services.last(); got.URL.Path != "/s/slug/page" || got.URL.RawQuery != "x=1" {
		t.Fatalf("/s/ arrived as %s", got.URL)
	}
}

func TestSpoofedHeadersDoNothing(t *testing.T) {
	r := newRig(t, func(*http.Request) string { return "203.0.113.9" })
	forged := http.Header{
		"X-Forwarded-For": {"127.0.0.1"}, "X-Real-Ip": {"127.0.0.1"}, "Forwarded": {"for=127.0.0.1"}, "X-Forwarded-Proto": {"http"}, "X-Forwarded-Host": {"localhost"},
		"True-Client-Ip": {"127.0.0.1"}, "Cf-Connecting-Ip": {"127.0.0.1"}, "X-Guest-Client": {"127.0.0.1"}, "X-Daedalus-Token": {"forged"}, "X-Daedalus-Through": {"local"},
	}
	for _, path := range []string{"/a/x", "/s/x/"} {
		r.ask(t, http.MethodGet, path, forged.Clone(), nil)
	}
	for _, got := range []*http.Request{r.door.last(), r.services.last()} {
		if v := got.Header.Values("X-Forwarded-For"); len(v) != 1 || v[0] != "203.0.113.9" {
			t.Errorf("X-Forwarded-For: %v", v)
		}
		if v := got.Header.Values(ThroughHeader); len(v) != 1 || v[0] != "tunnel" {
			t.Errorf("%s: %v", ThroughHeader, v)
		}
		if got.Header.Get("X-Forwarded-Proto") != "https" {
			t.Errorf("proto: %q", got.Header.Get("X-Forwarded-Proto"))
		}
		for _, name := range []string{"X-Real-Ip", "Forwarded", "True-Client-Ip", "Cf-Connecting-Ip", "X-Guest-Client", "X-Daedalus-Token", "X-Forwarded-Host"} {
			if got.Header.Get(name) != "" {
				t.Errorf("%s survived: %q", name, got.Header.Get(name))
			}
		}
	}
}

func TestAnUnknownVisitorIsNamedByNobody(t *testing.T) {
	r := newRig(t, func(*http.Request) string { return "" })
	r.ask(t, http.MethodGet, "/a/x", http.Header{"X-Forwarded-For": {"127.0.0.1"}}, nil)
	if got := r.door.last().Header.Get("X-Forwarded-For"); got != "" && !strings.HasPrefix(got, "127.0.0.1") {
		t.Fatalf("got %q", got)
	}
}

func TestAPageOnTheSameOriginCannotCallTheDoor(t *testing.T) {
	r := newRig(t, nil)
	for name, header := range map[string]http.Header{
		"a fetch from a page":   {"Sec-Fetch-Site": {"same-origin"}, "Sec-Fetch-Mode": {"cors"}, "Sec-Fetch-Dest": {"empty"}},
		"a form from a page":    {"Sec-Fetch-Site": {"same-origin"}, "Sec-Fetch-Mode": {"navigate"}, "Sec-Fetch-Dest": {"document"}, "Origin": {"https://x.example"}},
		"a frame":               {"Sec-Fetch-Site": {"same-origin"}, "Sec-Fetch-Mode": {"navigate"}, "Sec-Fetch-Dest": {"iframe"}},
		"cross-site":            {"Sec-Fetch-Site": {"cross-site"}, "Sec-Fetch-Mode": {"cors"}},
		"an origin and no site": {"Origin": {"https://x.example"}},
	} {
		if answer := r.ask(t, http.MethodPost, "/a/invite/redeem", header, nil); answer.StatusCode != http.StatusForbidden {
			t.Errorf("%s answered %d", name, answer.StatusCode)
		}
	}
	if r.door.count() != 0 {
		t.Fatal("a page's request reached the door")
	}
	for name, header := range map[string]http.Header{
		"an agent's curl":          {},
		"a typed address":          {"Sec-Fetch-Site": {"none"}, "Sec-Fetch-Mode": {"navigate"}, "Sec-Fetch-Dest": {"document"}},
		"a link clicked in a chat": {"Sec-Fetch-Site": {"cross-site"}, "Sec-Fetch-Mode": {"navigate"}, "Sec-Fetch-Dest": {"document"}},
	} {
		if answer := r.ask(t, http.MethodGet, "/a/invite", header, nil); answer.StatusCode != http.StatusOK {
			t.Errorf("%s answered %d", name, answer.StatusCode)
		}
	}
}

func TestTheDoorIsSentNoCookies(t *testing.T) {
	r := newRig(t, nil)
	r.ask(t, http.MethodGet, "/a/x", http.Header{"Cookie": {"set-by=a-page"}}, nil)
	if got := r.door.last().Header.Get("Cookie"); got != "" {
		t.Fatalf("cookie reached the door: %q", got)
	}
	r.ask(t, http.MethodGet, "/s/x/", http.Header{"Cookie": {"dshare-x=key"}}, nil)
	if got := r.services.last().Header.Get("Cookie"); got == "" {
		t.Fatal("a service's own key cookie has to reach it")
	}
}

func TestABodyWithNoLengthReachesTheFrontWithOne(t *testing.T) {
	r := newRig(t, nil)
	// io.MultiReader hides the length, so the client sends this chunked, as HTTP/2 arrives.
	body := io.MultiReader(strings.NewReader(`{"text":`), strings.NewReader(`"hello"}`))
	answer := r.ask(t, http.MethodPost, "/a/messages", http.Header{"Authorization": {"Bearer t"}}, body)
	if answer.StatusCode != http.StatusOK {
		t.Fatalf("answered %d", answer.StatusCode)
	}
	got := r.door.last()
	if got.ContentLength != 16 || len(got.TransferEncoding) != 0 || got.Header.Get("Authorization") != "Bearer t" {
		t.Fatalf("length %d, encoding %v", got.ContentLength, got.TransferEncoding)
	}
}

func TestABodyPastTheFrontsLimitIsTurnedAwayHere(t *testing.T) {
	r := newRig(t, nil)
	big := bytes.Repeat([]byte("x"), maxDoorBody+1)
	if answer := r.ask(t, http.MethodPost, "/a/x", nil, bytes.NewReader(big)); answer.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("declared: %d", answer.StatusCode)
	}
	if answer := r.ask(t, http.MethodPost, "/a/x", nil, io.MultiReader(bytes.NewReader(big))); answer.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("undeclared: %d", answer.StatusCode)
	}
	if r.door.count() != 0 {
		t.Fatal("it reached the door")
	}
	if answer := r.ask(t, http.MethodPost, "/s/x/upload", nil, bytes.NewReader(big)); answer.StatusCode != http.StatusOK {
		t.Fatalf("a service takes uploads: %d", answer.StatusCode)
	}
}

func TestTheProbeAnswersOnlyToItsOwnPath(t *testing.T) {
	r := newRig(t, nil)
	if answer := r.ask(t, http.MethodGet, "/.well-known/daedalus-probe", nil, nil); answer.StatusCode != http.StatusNoContent {
		t.Fatalf("probe: %d", answer.StatusCode)
	}
	if answer := r.ask(t, http.MethodGet, "/.well-known/daedalus-probe2", nil, nil); answer.StatusCode != http.StatusNotFound {
		t.Fatalf("a near miss: %d", answer.StatusCode)
	}
}

func TestADoorThatIsDownSaysSoAndNamesNoPort(t *testing.T) {
	r := newRig(t, nil)
	port := portOf(t, r.door.Server)
	r.door.Close()
	answer := r.ask(t, http.MethodGet, "/a/x", nil, nil)
	body, _ := io.ReadAll(answer.Body)
	if answer.StatusCode != http.StatusBadGateway || strings.Contains(string(body), port) {
		t.Fatalf("%d: %s", answer.StatusCode, body)
	}
}

// A real WebSocket handshake and a byte each way: a hot reload stands or falls on this.
func TestAWebSocketPassesUnderS(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		reader := bufio.NewReader(conn)
		if _, err := http.ReadRequest(reader); err != nil {
			return
		}
		io.WriteString(conn, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: x\r\n\r\n")
		buffer := make([]byte, 4)
		if _, err := io.ReadFull(reader, buffer); err == nil {
			conn.Write(bytes.ToUpper(buffer))
		}
	}()
	door := newUpstream(t)
	rt := NewRouter(portOf(t, door.Server), strings.TrimPrefix(listener.Addr().String(), "127.0.0.1:"), "", nil)
	front := httptest.NewServer(rt)
	defer front.Close()
	conn, err := net.Dial("tcp", strings.TrimPrefix(front.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(5 * time.Second))
	io.WriteString(conn, "GET /s/app/hmr HTTP/1.1\r\nHost: x.example\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Key: x\r\nSec-WebSocket-Version: 13\r\n\r\n")
	reader := bufio.NewReader(conn)
	answer, err := http.ReadResponse(reader, nil)
	if err != nil || answer.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("%v %v", answer, err)
	}
	io.WriteString(conn, "ping")
	back := make([]byte, 4)
	if _, err := io.ReadFull(reader, back); err != nil || string(back) != "PING" {
		t.Fatalf("%q %v", back, err)
	}
	// The door is not a thing a socket goes to.
	second := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/a/x", nil)
	request.Header.Set("Upgrade", "websocket")
	rt.ServeHTTP(second, request)
	if second.Code != http.StatusBadRequest {
		t.Fatalf("an upgrade at the door: %d", second.Code)
	}
}

func TestOneVisitorCannotHoldEveryConnection(t *testing.T) {
	rt := NewRouter("1", "2", "", func(*http.Request) string { return "203.0.113.9" })
	for i := 0; i < perVisitorLimit; i++ {
		if !rt.hold("/a/", "203.0.113.9") {
			t.Fatalf("refused at %d", i)
		}
	}
	if rt.hold("/a/", "203.0.113.9") {
		t.Fatal("past the limit")
	}
	if !rt.hold("/a/", "203.0.113.10") {
		t.Fatal("another visitor must not feel it")
	}
	if !rt.hold("/s/", "203.0.113.9") {
		t.Fatal("what is held at the door is not held at the services")
	}
	rt.release("/a/", "203.0.113.9")
	if !rt.hold("/a/", "203.0.113.9") {
		t.Fatal("a released connection counts no more")
	}
}

func TestOneIPv6HouseholdIsOneVisitor(t *testing.T) {
	rt := NewRouter("1", "2", "", nil)
	for i := 1; i <= perVisitorLimit; i++ {
		if !rt.hold("/a/", fmt.Sprintf("2001:db8:1:2::%x", i)) {
			t.Fatalf("refused at %d", i)
		}
	}
	if rt.hold("/a/", "2001:db8:1:2::ffff") {
		t.Fatal("the whole /64 is one household, and it took every address in it to hold every slot")
	}
	if !rt.hold("/a/", "2001:db8:1:3::1") {
		t.Fatal("the next /64 is someone else")
	}
	rt.release("/a/", "2001:db8:1:2::1")
	if !rt.hold("/a/", "2001:db8:1:2::ffff") {
		t.Fatal("a slot given back is the household's again")
	}
}

func TestHalfSentBodiesAtTheDoorLeaveTheServicesOpen(t *testing.T) {
	rt := NewRouter("1", "2", "", nil)
	for i := 0; i < inFlightLimit; i++ {
		if !rt.hold("/a/", fmt.Sprintf("192.0.2.%d", i)) {
			t.Fatalf("refused at %d", i)
		}
	}
	if rt.hold("/a/", "198.51.100.1") {
		t.Fatal("the door's budget is spent")
	}
	if !rt.hold("/s/", "198.51.100.1") {
		t.Fatal("the services have a budget of their own")
	}
}

func TestAnUnderscoredHeaderIsTheSameHeader(t *testing.T) {
	r := newRig(t, func(*http.Request) string { return "203.0.113.9" })
	forged := http.Header{"X_Forwarded_For": {"198.51.100.99"}, "X_forwarded_for": {"127.0.0.1"}, "X-Forwarded_For": {"127.0.0.1"}, "Cf_Connecting_Ip": {"127.0.0.1"}, "X_Real_Ip": {"127.0.0.1"}, "Forwarded_": {"kept"}, "X_Guest_Client": {"127.0.0.1"}, "Cf_Worker": {"x"}}
	r.ask(t, http.MethodGet, "/a/x", forged, nil)
	got := r.door.last()
	if v := got.Header.Values("X-Forwarded-For"); len(v) != 1 || v[0] != "203.0.113.9" {
		t.Fatalf("X-Forwarded-For: %v", v)
	}
	for name := range got.Header {
		if strings.Contains(name, "_") && name != "Forwarded_" {
			t.Errorf("%s reached the door", name)
		}
	}
}

func TestAProviderThatNamesNoVisitorIsNoticed(t *testing.T) {
	rt := NewRouter("1", "2", "", func(*http.Request) string { return "" })
	rt.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/app/", nil))
	if rt.nameless.Load() {
		t.Fatal("a 404 proves nothing about who came")
	}
	rt.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/a/x", nil))
	if !rt.nameless.Load() {
		t.Fatal("the status has to be able to say everyone is one address")
	}
}

func TestReadingAVisitor(t *testing.T) {
	cases := []struct {
		forwarded string
		want      string
	}{
		{"203.0.113.9", "203.0.113.9"},
		{"127.0.0.1, 203.0.113.9", "203.0.113.9"},
		{"203.0.113.9, 127.0.0.1", "203.0.113.9"},
		{"198.51.100.1, 203.0.113.9, 10.0.0.1", "203.0.113.9"},
		{"garbage, 203.0.113.9", "203.0.113.9"},
		{"127.0.0.1", ""},
		{"", ""},
		{"::ffff:203.0.113.9", "203.0.113.9"},
	}
	for _, c := range cases {
		request := httptest.NewRequest(http.MethodGet, "/", nil)
		request.Header.Set("X-Forwarded-For", c.forwarded)
		if got := lastPublicForwarded(request); got != c.want {
			t.Errorf("%q: got %q, want %q", c.forwarded, got, c.want)
		}
	}
	request := httptest.NewRequest(http.MethodGet, "/", nil)
	request.Header.Set("Cf-Connecting-Ip", "not an address")
	if got := edgeHeader("Cf-Connecting-Ip")(request); got != "" {
		t.Fatalf("got %q", got)
	}
}
