package relay

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// channel is the extension's end of one relay channel, in memory: what the daemon sends is joined
// back into frames, and Closed says the daemon closed it.
type channel struct {
	mu     sync.Mutex
	join   Joiner
	frames chan []byte
	closed chan struct{}
	once   sync.Once
	link   *Link
}

func newChannel() *channel {
	return &channel{join: Joiner{Max: MaxToExtension}, frames: make(chan []byte, 256), closed: make(chan struct{})}
}

func (c *channel) Send(p []byte) error {
	if len(p) > wire.MaxPayload {
		return wire.ErrFrameTooLarge
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	select {
	case <-c.closed:
		return errors.New("closed")
	default:
	}
	f, err := c.join.Add(p)
	if err != nil {
		return err
	}
	if f != nil {
		c.frames <- f
	}
	return nil
}

func (c *channel) Close() { c.once.Do(func() { close(c.closed) }) }

// send is the extension sending one frame.
func (c *channel) send(t *testing.T, v any) {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range Split(b, wire.MaxPayload) {
		c.link.Frame(p)
	}
}

// next waits for the daemon's next frame of type typ, skipping pings unless asked for.
func (c *channel) next(t *testing.T, typ string, into any) {
	t.Helper()
	deadline := time.After(5 * time.Second)
	for {
		select {
		case f := <-c.frames:
			got, _ := Type(f)
			if got == TPing && typ != TPing {
				continue
			}
			if got != typ {
				t.Fatalf("a %s frame, not %s: %s", got, typ, f)
			}
			if into != nil {
				if err := json.Unmarshal(f, into); err != nil {
					t.Fatal(err)
				}
			}
			return
		case <-deadline:
			t.Fatalf("no %s frame", typ)
		}
	}
}

func (c *channel) waitClosed(t *testing.T) {
	t.Helper()
	select {
	case <-c.closed:
	case <-time.After(5 * time.Second):
		t.Fatal("the daemon did not close the channel")
	}
}

type published struct {
	mu  sync.Mutex
	evs []map[string]any
	typ []string
}

func (p *published) add(typ string, data any) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.typ = append(p.typ, typ)
	m, _ := data.(map[string]any)
	p.evs = append(p.evs, m)
}

func (p *published) wait(t *testing.T, typ string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		p.mu.Lock()
		for i, x := range p.typ {
			if x == typ {
				e := p.evs[i]
				p.mu.Unlock()
				return e
			}
		}
		p.mu.Unlock()
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("no %s event", typ)
	return nil
}

func testHub(o Options) (*Hub, *published) {
	pub := &published{}
	o.Publish = pub.add
	return NewHub(o), pub
}

func (h *Hub) connect(t *testing.T, profileID, name string) *channel {
	t.Helper()
	c := newChannel()
	c.link = h.Open(c, TransportNative, "")
	c.send(t, Hello{T: THello, Proto: Proto, ExtVersion: "1.0.0", ChromeVersion: "141.0", Platform: "linux", ProfileID: profileID, ProfileName: name})
	return c
}

func TestHelloWelcome(t *testing.T) {
	h, pub := testHub(Options{InstallName: "Home"})
	if st := h.Status(); st.Connected {
		t.Fatal("connected before any relay")
	}
	c := h.connect(t, "p1", "Work")
	var w Welcome
	c.next(t, TWelcome, &w)
	if w.Proto != Proto || w.InstallName != "Home" || w.InstallKind != InstallNative {
		t.Fatalf("welcome %+v", w)
	}
	ev := pub.wait(t, "relay.connected")
	if ev["profile_name"] != "Work" || ev["transport"] != TransportNative || ev["chrome_version"] != "141.0" {
		t.Fatalf("relay.connected %v", ev)
	}
	st := h.Status()
	if !st.Connected || st.ProfileName != "Work" || st.ExtVersion != "1.0.0" || st.Since == nil || st.LastSeen == nil {
		t.Fatalf("status %+v", st)
	}
	// The server transport welcomes as a server installation, under the name relay.open gave.
	s := newChannel()
	s.link = h.Open(s, TransportServer, "Office")
	h.Current().end(LostClosed, nil)
	s.send(t, Hello{T: THello, Proto: Proto, ProfileID: "p2"})
	s.next(t, TWelcome, &w)
	if w.InstallKind != InstallServer || w.InstallName != "Office" {
		t.Fatalf("server welcome %+v", w)
	}
	if h.Status().ProfileName != "Chrome" {
		t.Fatalf("a hello without a name is Chrome: %+v", h.Status())
	}
}

// One relay at a time: a second Chrome is refused with the name of the one connected, and the first
// keeps working.
func TestSecondRelayRefused(t *testing.T) {
	h, _ := testHub(Options{})
	first := h.connect(t, "p1", "Home Chrome")
	first.next(t, TWelcome, nil)
	second := h.connect(t, "p2", "Laptop")
	var bye Bye
	second.next(t, TBye, &bye)
	if bye.Reason != ByeAnotherRelay || !strings.Contains(bye.Message, "another Chrome is already connected: Home Chrome") {
		t.Fatalf("bye %+v", bye)
	}
	second.waitClosed(t)
	if st := h.Status(); !st.Connected || st.ProfileName != "Home Chrome" || !strings.Contains(st.LastError, "Laptop") {
		t.Fatalf("status %+v", st)
	}
	first.send(t, Check{T: TCheck, N: 1})
	first.next(t, TCheckOK, nil)
}

// The same Chrome profile connecting again (its worker restarted, its old bridge not yet gone)
// replaces the stale relay.
func TestSameProfileReplaces(t *testing.T) {
	h, pub := testHub(Options{})
	old := h.connect(t, "p1", "Home")
	old.next(t, TWelcome, nil)
	oldSession := h.Current()
	fresh := h.connect(t, "p1", "Home")
	fresh.next(t, TWelcome, nil)
	var bye Bye
	old.next(t, TBye, &bye)
	if bye.Reason != ByeReplaced {
		t.Fatalf("bye %+v", bye)
	}
	old.waitClosed(t)
	if h.Current() == oldSession || h.Current() == nil {
		t.Fatal("the new relay is not the current one")
	}
	if ev := pub.wait(t, "relay.lost"); ev["reason"] != LostReplaced {
		t.Fatalf("relay.lost %v", ev)
	}
}

func TestProtoMismatch(t *testing.T) {
	h, _ := testHub(Options{})
	c := newChannel()
	c.link = h.Open(c, TransportNative, "")
	c.send(t, Hello{T: THello, Proto: Proto + 1, ProfileID: "p"})
	var bye Bye
	c.next(t, TBye, &bye)
	if bye.Reason != ByeProto || bye.Proto != Proto || !strings.Contains(bye.Message, "update Daedalus") {
		t.Fatalf("bye %+v", bye)
	}
	c.waitClosed(t)
	if st := h.Status(); st.Connected || !strings.Contains(st.LastError, "versions differ") {
		t.Fatalf("status %+v", st)
	}
}

func TestHelloFirst(t *testing.T) {
	h, _ := testHub(Options{})
	c := newChannel()
	c.link = h.Open(c, TransportNative, "")
	c.send(t, Ping{T: TPong, N: 1})
	var bye Bye
	c.next(t, TBye, &bye)
	if bye.Reason != ByeBadFrame {
		t.Fatalf("bye %+v", bye)
	}
	c.waitClosed(t)
	d := newChannel()
	d.link = h.Open(d, TransportNative, "")
	d.link.Frame([]byte{PieceLast, '{', 'x'})
	d.next(t, TBye, &bye)
	if bye.Reason != ByeBadFrame {
		t.Fatalf("a frame that is not JSON: %+v", bye)
	}
}

// Three pings unanswered and the relay is lost; answered, it lives.
func TestPingLiveness(t *testing.T) {
	h, pub := testHub(Options{PingInterval: 20 * time.Millisecond})
	c := h.connect(t, "p1", "Home")
	c.next(t, TWelcome, nil)
	var p Ping
	for i := 0; i < 6; i++ {
		c.next(t, TPing, &p)
		c.send(t, Pong{T: TPong, N: p.N})
	}
	if !h.Status().Connected {
		t.Fatal("lost while answering")
	}
	var bye Bye
	deadline := time.After(5 * time.Second)
	for bye.T == "" {
		select {
		case f := <-c.frames:
			if typ, _ := Type(f); typ == TBye {
				_ = json.Unmarshal(f, &bye)
			}
		case <-deadline:
			t.Fatal("never lost")
		}
	}
	if bye.Reason != ByeLost {
		t.Fatalf("bye %+v", bye)
	}
	if ev := pub.wait(t, "relay.lost"); ev["reason"] != LostNoPong {
		t.Fatalf("relay.lost %v", ev)
	}
	if st := h.Status(); st.Connected || st.LastError != "Chrome stopped answering" || st.ProfileName != "Home" {
		t.Fatalf("status %+v", st)
	}
}

func TestCheck(t *testing.T) {
	seen := false
	h, pub := testHub(Options{HostSeen: func() bool { return seen }})
	c := h.connect(t, "p1", "Home")
	c.next(t, TWelcome, nil)
	var ok CheckOK
	c.send(t, Check{T: TCheck, N: 4})
	c.next(t, TCheckOK, &ok)
	if ok.N != 4 || ok.HostSeen {
		t.Fatalf("check_ok %+v with nobody listening", ok)
	}
	if ev := pub.wait(t, "relay.check"); ev["n"] != int64(4) || ev["profile_name"] != "Home" {
		t.Fatalf("relay.check %v", ev)
	}
	seen = true
	c.send(t, Check{T: TCheck, N: 5})
	c.next(t, TCheckOK, &ok)
	if !ok.HostSeen {
		t.Fatalf("check_ok %+v with the host listening", ok)
	}
}

func TestCall(t *testing.T) {
	h, _ := testHub(Options{})
	c := h.connect(t, "p1", "Home")
	c.next(t, TWelcome, nil)
	s := h.Current()
	go func() {
		var call struct {
			ID   int64          `json:"id"`
			Op   string         `json:"op"`
			Args map[string]any `json:"args"`
		}
		c.next(t, TCall, &call)
		if call.Op != OpTabCreate || call.Args["group"] != "s-1" {
			t.Errorf("call %+v", call)
		}
		c.send(t, Result{T: TResult, ID: call.ID, OK: true, Value: json.RawMessage(`{"tab":{"id":9,"group":"s-1","url":"about:blank"}}`)})
		c.next(t, TCall, &call)
		c.send(t, Result{T: TResult, ID: call.ID, OK: false, Error: &Error{Code: ErrRefused, Message: "not yours"}})
	}()
	var r TabCreated
	if err := s.Call(context.Background(), OpTabCreate, TabCreateArgs{Group: "s-1", URL: "about:blank"}, &r); err != nil || r.Tab.ID != 9 {
		t.Fatalf("%v %+v", err, r)
	}
	err := s.Call(context.Background(), OpTabClose, TabArgs{Tab: 3}, nil)
	var re *Error
	if !errors.As(err, &re) || re.Code != ErrRefused {
		t.Fatalf("a refusal: %v", err)
	}
	// A relay that goes away fails the call waiting on it.
	done := make(chan error, 1)
	go func() { done <- s.Call(context.Background(), OpNotify, NotifyArgs{}, nil) }()
	c.next(t, TCall, nil)
	c.link.Closed()
	if err := <-done; !errors.Is(err, ErrNotConnected) {
		t.Fatalf("a call across the end: %v", err)
	}
	if h.Current() != nil || h.Status().LastError != "Chrome closed the connection" {
		t.Fatalf("status %+v", h.Status())
	}
}

func TestShutdownBye(t *testing.T) {
	h, _ := testHub(Options{})
	c := h.connect(t, "p1", "Home")
	c.next(t, TWelcome, nil)
	h.Close()
	var bye Bye
	c.next(t, TBye, &bye)
	if bye.Reason != ByeShutdown {
		t.Fatalf("bye %+v", bye)
	}
	c.waitClosed(t)
	late := h.connect(t, "p2", "Late")
	late.next(t, TBye, &bye)
	if bye.Reason != ByeShutdown {
		t.Fatalf("a relay after shutdown: %+v", bye)
	}
}

// The DevTools connection over the relay: commands to tabs become cdp frames in order, browser-level
// ones and those off the allowlist fail at once, results and events come back as cdp's, and the
// child sessions a tab's auto-attach announces are named by the tab.
func TestConn(t *testing.T) {
	h, _ := testHub(Options{})
	c := h.connect(t, "p1", "Home")
	c.next(t, TWelcome, nil)
	s := h.Current()
	events := make(chan cdp.Event, 16)
	var dispatched []int
	var dmu sync.Mutex
	conn, drop := s.NewConn(func(e cdp.Event) { events <- e }, func(tab int) {
		dmu.Lock()
		dispatched = append(dispatched, tab)
		dmu.Unlock()
	})
	defer drop()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := conn.Call(ctx, "", "Browser.getVersion", nil, nil); err == nil || !strings.Contains(err.Error(), "browser-level") {
		t.Fatalf("a browser-level command: %v", err)
	}
	if err := conn.Call(ctx, "x5", "Network.getAllCookies", nil, nil); err == nil || !strings.Contains(err.Error(), "not carried") {
		t.Fatalf("a refused method: %v", err)
	}
	if err := conn.Call(ctx, "5", "Page.enable", nil, nil); err == nil {
		t.Fatal("a session that is not a relay's")
	}
	if err := conn.Call(ctx, "x5", "Runtime.evaluate", map[string]any{"expression": strings.Repeat("x", MaxToExtension)}, nil); err == nil ||
		!strings.Contains(err.Error(), "larger than the relay carries") {
		t.Fatalf("a command past Chrome's 1 MB: %v", err)
	}

	// Two commands sent without waiting reach the extension in order.
	first := conn.Send(ctx, "x5", "Page.enable", nil)
	second := conn.Send(ctx, "x5", "Input.dispatchMouseEvent", map[string]any{"type": "mouseMoved", "x": 1, "y": 2})
	var a, b CDP
	c.next(t, TCDP, &a)
	c.next(t, TCDP, &b)
	if a.Method != "Page.enable" || b.Method != "Input.dispatchMouseEvent" || a.Session != "x5" || b.ID <= a.ID {
		t.Fatalf("frames %+v %+v", a, b)
	}
	c.send(t, Result{T: TResult, ID: b.ID, OK: true})
	c.send(t, Result{T: TResult, ID: a.ID, OK: false, Error: &Error{Code: ErrCDP, Message: `{"code":-32601,"message":"'Page.enable' wasn't found"}`}})
	if err := <-second; err != nil {
		t.Fatal(err)
	}
	err := <-first
	var ce *cdp.Error
	if !errors.As(err, &ce) || ce.Code != -32601 {
		t.Fatalf("Chrome's own error keeps its code: %v", err)
	}
	dmu.Lock()
	if len(dispatched) != 1 || dispatched[0] != 5 {
		t.Fatalf("input dispatched to %v", dispatched)
	}
	dmu.Unlock()

	// A child announced by Chrome's id is named by its tab, and a command to it carries that name.
	c.send(t, Event{T: TEvent, Session: "x5", Method: "Target.attachedToTarget",
		Params: json.RawMessage(`{"sessionId":"C1","targetInfo":{"targetId":"T1","type":"iframe"},"waitingForDebugger":true}`)})
	e := <-events
	var att struct {
		SessionID string `json:"sessionId"`
	}
	_ = json.Unmarshal(e.Params, &att)
	if e.Session != "x5" || att.SessionID != "x5/C1" {
		t.Fatalf("attached %+v %s", e, e.Params)
	}
	go func() { _ = conn.Call(ctx, "x5/C1", "Runtime.runIfWaitingForDebugger", nil, nil) }()
	var run CDP
	c.next(t, TCDP, &run)
	if run.Session != "x5/C1" {
		t.Fatalf("to the child: %+v", run)
	}
	go func() { _ = conn.Call(ctx, "x5", "Target.detachFromTarget", map[string]any{"sessionId": "x5/C1"}, nil) }()
	var det CDP
	c.next(t, TCDP, &det)
	if !bytes.Contains(det.Params, []byte(`"sessionId":"C1"`)) {
		t.Fatalf("detach carries Chrome's id: %s", det.Params)
	}
	if err := conn.Call(ctx, "x5", "Target.detachFromTarget", map[string]any{"sessionId": "x6/C1"}, nil); err == nil {
		t.Fatal("detaching another tab's child")
	}
	c.send(t, Event{T: TEvent, Session: "x5/C1", Method: "Runtime.executionContextCreated", Params: json.RawMessage(`{"context":{"id":3}}`)})
	if e := <-events; e.Session != "x5/C1" || e.Method != "Runtime.executionContextCreated" {
		t.Fatalf("a child's event %+v", e)
	}

	// The relay's end ends the connection, and a call on it fails.
	c.link.Closed()
	select {
	case <-conn.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("the connection outlived its relay")
	}
	if err := conn.Call(ctx, "x5", "Page.enable", nil, nil); err == nil {
		t.Fatal("a call after the end")
	}
}

// A frame larger than one socket frame — a full-page screenshot's result — arrives in pieces and is
// whole again on the daemon's side.
func TestLargeResult(t *testing.T) {
	h, _ := testHub(Options{})
	c := h.connect(t, "p1", "Home")
	c.next(t, TWelcome, nil)
	conn, drop := h.Current().NewConn(func(cdp.Event) {}, nil)
	defer drop()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	big := strings.Repeat("A", 3<<20)
	go func() {
		var shot CDP
		c.next(t, TCDP, &shot)
		value, _ := json.Marshal(map[string]string{"data": big})
		c.send(t, Result{T: TResult, ID: shot.ID, OK: true, Value: value})
	}()
	var r struct {
		Data string `json:"data"`
	}
	if err := conn.Call(ctx, "x5", "Page.captureScreenshot", nil, &r); err != nil || r.Data != big {
		t.Fatalf("%v, %d bytes", err, len(r.Data))
	}
}
