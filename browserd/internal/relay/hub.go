package relay

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/version"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The relay's liveness: a ping every PingInterval, and the relay is lost after MissedPings of them
// go unanswered. A worker the browser suspended, a bridge whose Chrome quit without closing its
// pipe, a WebSocket whose network went away: each would otherwise hold the one relay slot for ever.
const (
	PingInterval = 15 * time.Second
	MissedPings  = 3
)

// The reasons a relay ends, as relay.lost and relay.status name them.
const (
	LostClosed   = "closed"    // the bridge or the host's proxy closed the channel: Chrome quit, the extension reloaded
	LostNoPong   = "no_pong"   // three pings went unanswered
	LostReplaced = "replaced"  // the same Chrome profile connected again
	LostShutdown = "shutdown"  // the daemon is stopping
	LostBadFrame = "bad_frame" // the relay sent something the daemon cannot read
)

// shutdownBye is how long a stopping daemon waits for the relay to take its bye and close.
const shutdownBye = time.Second

// ErrNotConnected is a call made while no relay is connected.
var ErrNotConnected = errors.New("Chrome is not connected")

// Options are what the hub needs from the daemon.
type Options struct {
	// InstallName is the installation's name, shown in the extension's popup; a relay.open may
	// name another.
	InstallName string
	// Publish publishes a daemon event: relay.connected, relay.lost, relay.check.
	Publish func(typ string, data any)
	// HostSeen says whether the host is listening to the daemon's events, which is what check_ok's
	// host_seen confirms to the popup.
	HostSeen func() bool
	Log      *slog.Logger
	// PingInterval and MissedPings replace the defaults when not zero; only a test sets them.
	PingInterval time.Duration
	MissedPings  int
}

// Sender is the daemon's side of one relay channel: Send carries one socket-frame payload, Close
// ends the channel.
type Sender interface {
	Send(payload []byte) error
	Close()
}

// Hub holds the one relay a `user` daemon has at a time.
type Hub struct {
	o Options

	mu        sync.Mutex
	cur       *Session
	last      Status // the relay that was connected last, for a status while none is
	lastError string
	closed    bool
}

// NewHub returns a hub with no relay.
func NewHub(o Options) *Hub {
	if o.PingInterval <= 0 {
		o.PingInterval = PingInterval
	}
	if o.MissedPings <= 0 {
		o.MissedPings = MissedPings
	}
	if o.InstallName == "" {
		o.InstallName = "Daedalus"
	}
	if o.Log == nil {
		o.Log = slog.New(slog.DiscardHandler)
	}
	return &Hub{o: o}
}

func (h *Hub) publish(typ string, data any) {
	if h.o.Publish != nil {
		h.o.Publish(typ, data)
	}
}

// Status is the relay as daemon.info and relay.status report it.
type Status struct {
	Connected     bool       `json:"connected"`
	ProfileID     string     `json:"profile_id,omitempty"`
	ProfileName   string     `json:"profile_name,omitempty"`
	ExtVersion    string     `json:"ext_version,omitempty"`
	ChromeVersion string     `json:"chrome_version,omitempty"`
	Platform      string     `json:"platform,omitempty"`
	Transport     string     `json:"transport,omitempty"`
	Since         *time.Time `json:"since"`
	LastSeen      *time.Time `json:"last_seen"`
	LastError     string     `json:"last_error,omitempty"`
}

// Status is the relay now: the connected one, or the last one with why it is not.
func (h *Hub) Status() Status {
	h.mu.Lock()
	cur, last, lastError := h.cur, h.last, h.lastError
	h.mu.Unlock()
	if cur != nil {
		st := cur.status()
		st.LastError = lastError
		return st
	}
	last.Connected = false
	last.LastError = lastError
	return last
}

// Current is the connected relay, or nil.
func (h *Hub) Current() *Session {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.cur
}

// Close ends the relay with a bye saying the daemon stops, and refuses any other.
func (h *Hub) Close() {
	h.mu.Lock()
	h.closed = true
	cur := h.cur
	h.mu.Unlock()
	if cur != nil {
		cur.end(LostShutdown, &Bye{T: TBye, Reason: ByeShutdown, Message: "Daedalus is stopping"})
	}
}

// Open takes over a channel a relay.open opened: the frames that arrive on it go to the returned
// link, which the caller registers as the channel's handler. transport is how the relay arrives;
// installName, when set, replaces the hub's for this relay.
func (h *Hub) Open(out Sender, transport, installName string) *Link {
	if installName == "" {
		installName = h.o.InstallName
	}
	return &Link{h: h, out: out, transport: transport, installName: installName, join: Joiner{Max: MaxFromExtension}, gone: make(chan struct{})}
}

// Link is one relay channel, from relay.open to its end. Until the extension's hello is accepted it
// is a candidate; then it carries a Session.
type Link struct {
	h           *Hub
	out         Sender
	transport   string
	installName string
	join        Joiner

	mu     sync.Mutex
	s      *Session
	ended  bool
	sendMu sync.Mutex
	// gone is closed when the other side closes the channel.
	gone     chan struct{}
	goneOnce sync.Once
}

// Frame takes one payload of the relay channel. It runs on the socket's read loop and never waits
// for anything slow.
func (l *Link) Frame(payload []byte) {
	frame, err := l.join.Add(payload)
	if err != nil {
		l.fail(fmt.Sprintf("a broken piece: %v", err))
		return
	}
	if frame == nil {
		return
	}
	t, err := Type(frame)
	if err != nil {
		l.fail(err.Error())
		return
	}
	l.mu.Lock()
	s, ended := l.s, l.ended
	l.mu.Unlock()
	if ended {
		return
	}
	if s == nil {
		if t != THello {
			l.fail("the first frame must be hello, not " + t)
			return
		}
		l.hello(frame)
		return
	}
	s.frame(t, frame)
}

// Closed is the other side closing the channel.
func (l *Link) Closed() {
	l.goneOnce.Do(func() { close(l.gone) })
	l.mu.Lock()
	s := l.s
	l.ended = true
	l.mu.Unlock()
	if s != nil {
		s.end(LostClosed, nil)
	}
}

// send writes one frame as the pieces the channel carries.
func (l *Link) send(v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	if len(b) > MaxToExtension {
		return ErrTooLarge
	}
	l.sendMu.Lock()
	defer l.sendMu.Unlock()
	for _, p := range Split(b, wire.MaxPayload) {
		if err := l.out.Send(p); err != nil {
			return err
		}
	}
	return nil
}

// refuse says bye to a candidate and closes its channel.
func (l *Link) refuse(bye Bye) {
	l.mu.Lock()
	l.ended = true
	l.mu.Unlock()
	_ = l.send(bye)
	l.out.Close()
}

func (l *Link) fail(why string) {
	l.mu.Lock()
	s := l.s
	l.mu.Unlock()
	l.h.o.Log.Warn("relay frame refused", "error", why)
	if s != nil {
		s.end(LostBadFrame, &Bye{T: TBye, Reason: ByeBadFrame, Message: why})
		return
	}
	l.h.setError("a relay sent a broken frame: " + why)
	l.refuse(Bye{T: TBye, Reason: ByeBadFrame, Message: why})
}

func (h *Hub) setError(e string) {
	h.mu.Lock()
	h.lastError = e
	h.mu.Unlock()
}

// clip bounds a string the extension sends that the daemon shows or logs.
func clip(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

func (l *Link) hello(frame []byte) {
	var hi Hello
	if err := json.Unmarshal(frame, &hi); err != nil {
		l.fail("hello: " + err.Error())
		return
	}
	hi.ProfileName = clip(hi.ProfileName, 200)
	if hi.ProfileName == "" {
		hi.ProfileName = "Chrome"
	}
	hi.ProfileID = clip(hi.ProfileID, 200)
	hi.ExtVersion, hi.ChromeVersion, hi.Platform = clip(hi.ExtVersion, 64), clip(hi.ChromeVersion, 64), clip(hi.Platform, 64)
	hi.Token = ""
	h := l.h
	if hi.Proto != Proto {
		side := "Daedalus"
		if hi.Proto < Proto {
			side = "the extension"
		}
		msg := fmt.Sprintf("versions differ: the extension speaks relay protocol %d, Daedalus %d; update %s", hi.Proto, Proto, side)
		h.setError(msg)
		h.o.Log.Warn("relay refused", "reason", "proto", "extension_proto", hi.Proto)
		l.refuse(Bye{T: TBye, Reason: ByeProto, Message: msg, Proto: Proto})
		return
	}
	h.mu.Lock()
	if h.closed {
		h.mu.Unlock()
		l.refuse(Bye{T: TBye, Reason: ByeShutdown, Message: "Daedalus is stopping"})
		return
	}
	old := h.cur
	if old != nil && (hi.ProfileID == "" || old.hello.ProfileID != hi.ProfileID) {
		name := old.hello.ProfileName
		h.lastError = fmt.Sprintf("another Chrome tried to connect (%s) while %s was connected", hi.ProfileName, name)
		h.mu.Unlock()
		h.o.Log.Info("relay refused", "reason", "another relay", "connected", name, "refused", hi.ProfileName)
		l.refuse(Bye{T: TBye, Reason: ByeAnotherRelay, Message: "another Chrome is already connected: " + name})
		return
	}
	now := time.Now().UTC()
	s := &Session{h: h, link: l, hello: hi, transport: l.transport, since: now, lastSeen: now,
		pending: map[int64]func(*Result){}, done: make(chan struct{}), observed: make(chan func(), 256)}
	h.cur = s
	h.lastError = ""
	h.mu.Unlock()
	if old != nil {
		// The same Chrome profile again: its worker restarted, or its bridge outlived a Chrome that
		// quit. The new relay is the live one.
		old.end(LostReplaced, &Bye{T: TBye, Reason: ByeReplaced, Message: "this Chrome connected again"})
	}
	l.mu.Lock()
	l.s = s
	l.mu.Unlock()
	kind := InstallNative
	if l.transport == TransportServer {
		kind = InstallServer
	}
	if err := l.send(Welcome{T: TWelcome, Proto: Proto, DaemonVersion: version.Version, InstallName: l.installName, InstallKind: kind}); err != nil {
		s.end(LostClosed, nil)
		return
	}
	go s.observe()
	go s.live()
	h.o.Log.Info("relay connected", "profile", hi.ProfileName, "extension", hi.ExtVersion, "chrome", hi.ChromeVersion, "transport", l.transport)
	h.publish("relay.connected", map[string]any{"profile_id": hi.ProfileID, "profile_name": hi.ProfileName, "ext_version": hi.ExtVersion,
		"chrome_version": hi.ChromeVersion, "platform": hi.Platform, "transport": l.transport, "proto": hi.Proto})
}

// The kinds of installation a welcome names.
const (
	InstallNative = "native"
	InstallServer = "server"
)

// Observer is told what the operator changed about the agent's tabs. Calls come one at a time, in
// the order the relay sent them, on a goroutine of their own.
type Observer interface {
	RelayTabs(Tabs)
	RelayInput(tab int)
}

// Session is a connected relay.
type Session struct {
	h         *Hub
	link      *Link
	hello     Hello
	transport string
	since     time.Time

	mu         sync.Mutex
	lastSeen   time.Time
	unanswered int
	ping       int64
	next       int64
	pending    map[int64]func(*Result)
	observer   Observer
	conn       *adapter
	ended      bool
	reason     string

	observed chan func()
	done     chan struct{}
}

// Done is closed when the relay ends.
func (s *Session) Done() <-chan struct{} { return s.done }

// Reason is why the relay ended, "" while it lives.
func (s *Session) Reason() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.reason
}

// Hello is what the extension said of itself.
func (s *Session) Hello() Hello { return s.hello }

func (s *Session) status() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	since, seen := s.since, s.lastSeen
	return Status{Connected: !s.ended, ProfileID: s.hello.ProfileID, ProfileName: s.hello.ProfileName, ExtVersion: s.hello.ExtVersion,
		ChromeVersion: s.hello.ChromeVersion, Platform: s.hello.Platform, Transport: s.transport, Since: &since, LastSeen: &seen}
}

// SetObserver names who hears of the operator's changes.
func (s *Session) SetObserver(o Observer) {
	s.mu.Lock()
	s.observer = o
	s.mu.Unlock()
}

func (s *Session) observe() {
	for {
		select {
		case f := <-s.observed:
			f()
		case <-s.done:
			return
		}
	}
}

// later queues an observer's call behind the ones before it.
func (s *Session) later(f func()) {
	select {
	case s.observed <- f:
	case <-s.done:
	}
}

// live pings the relay and ends it when it stops answering.
func (s *Session) live() {
	tick := time.NewTicker(s.h.o.PingInterval)
	defer tick.Stop()
	for {
		select {
		case <-tick.C:
		case <-s.done:
			return
		}
		s.mu.Lock()
		missed := s.unanswered
		s.unanswered++
		s.ping++
		n := s.ping
		s.mu.Unlock()
		if missed >= s.h.o.MissedPings {
			s.end(LostNoPong, &Bye{T: TBye, Reason: ByeLost, Message: fmt.Sprintf("no answer to %d pings", missed)})
			return
		}
		_ = s.link.send(Ping{T: TPing, N: n})
	}
}

// end finishes the relay once: it is forgotten, everything waiting on it fails, bye (when given) is
// its last frame, and relay.lost is published.
func (s *Session) end(reason string, bye *Bye) {
	s.mu.Lock()
	if s.ended {
		s.mu.Unlock()
		return
	}
	s.ended = true
	s.reason = reason
	pending := s.pending
	s.pending = map[int64]func(*Result){}
	conn := s.conn
	s.mu.Unlock()
	h := s.h
	h.mu.Lock()
	if h.cur == s {
		h.cur = nil
		h.last = s.status()
		h.last.Connected = false
	}
	if reason != LostReplaced && reason != LostShutdown {
		h.lastError = lostWords(reason)
	}
	h.mu.Unlock()
	close(s.done)
	for _, f := range pending {
		f(nil)
	}
	if conn != nil {
		conn.close()
	}
	s.link.mu.Lock()
	s.link.ended = true
	s.link.mu.Unlock()
	if bye != nil {
		_ = s.link.send(*bye)
	}
	if reason == LostShutdown {
		// The daemon is about to close its connections, and a frame still queued then is never
		// written: the bridge and the host's proxy close their side once they have the bye, and that
		// is waited for, briefly.
		select {
		case <-s.link.gone:
		case <-time.After(shutdownBye):
		}
	}
	s.link.out.Close()
	h.o.Log.Info("relay lost", "profile", s.hello.ProfileName, "reason", reason)
	h.publish("relay.lost", map[string]any{"reason": reason, "profile_id": s.hello.ProfileID, "profile_name": s.hello.ProfileName})
}

// lostWords is a lost relay's reason as the app shows it under "Not connected".
func lostWords(reason string) string {
	switch reason {
	case LostClosed:
		return "Chrome closed the connection"
	case LostNoPong:
		return "Chrome stopped answering"
	case LostBadFrame:
		return "Chrome sent something Daedalus cannot read"
	}
	return reason
}

// frame handles one frame of an accepted relay.
func (s *Session) frame(t string, frame []byte) {
	s.mu.Lock()
	s.lastSeen = time.Now().UTC()
	s.mu.Unlock()
	switch t {
	case TResult:
		var r Result
		if err := json.Unmarshal(frame, &r); err != nil {
			s.link.fail("result: " + err.Error())
			return
		}
		s.mu.Lock()
		f := s.pending[r.ID]
		delete(s.pending, r.ID)
		s.mu.Unlock()
		if f != nil {
			f(&r)
		}
	case TEvent:
		var e Event
		if err := json.Unmarshal(frame, &e); err != nil {
			s.link.fail("event: " + err.Error())
			return
		}
		s.mu.Lock()
		conn := s.conn
		s.mu.Unlock()
		if conn != nil {
			conn.event(e)
		}
	case TPong:
		s.mu.Lock()
		s.unanswered = 0
		s.mu.Unlock()
	case TCheck:
		var c Check
		if err := json.Unmarshal(frame, &c); err != nil {
			s.link.fail("check: " + err.Error())
			return
		}
		seen := s.h.o.HostSeen != nil && s.h.o.HostSeen()
		_ = s.link.send(CheckOK{T: TCheckOK, N: c.N, HostSeen: seen})
		s.h.publish("relay.check", map[string]any{"n": c.N, "profile_id": s.hello.ProfileID, "profile_name": s.hello.ProfileName})
	case TTabs:
		var tb Tabs
		if err := json.Unmarshal(frame, &tb); err != nil {
			s.link.fail("tabs: " + err.Error())
			return
		}
		s.later(func() {
			if o := s.getObserver(); o != nil {
				o.RelayTabs(tb)
			}
		})
	case TInput:
		var in Input
		if err := json.Unmarshal(frame, &in); err != nil {
			s.link.fail("input: " + err.Error())
			return
		}
		s.later(func() {
			if o := s.getObserver(); o != nil {
				o.RelayInput(in.Tab)
			}
		})
	case THello:
		s.link.fail("a second hello")
	default:
		// A frame of a newer extension this daemon does not know: ignored, so an addition to the
		// protocol does not need a new version of it.
		s.h.o.Log.Debug("relay frame ignored", "t", t)
	}
}

func (s *Session) getObserver() Observer {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.observer
}

// register sends a frame with a fresh id and calls back with its result, or with nil when the relay
// ends first. build makes the frame from the id.
func (s *Session) register(build func(id int64) any, back func(*Result)) error {
	s.mu.Lock()
	if s.ended {
		s.mu.Unlock()
		return ErrNotConnected
	}
	s.next++
	id := s.next
	s.pending[id] = back
	s.mu.Unlock()
	if err := s.link.send(build(id)); err != nil {
		s.mu.Lock()
		delete(s.pending, id)
		s.mu.Unlock()
		return err
	}
	return nil
}

// Call runs one browser-level operation and decodes its value into out (which may be nil). A
// failure the extension reports is an *Error.
func (s *Session) Call(ctx context.Context, op string, args, out any) error {
	ch := make(chan *Result, 1)
	if args == nil {
		args = map[string]any{}
	}
	if err := s.register(func(id int64) any { return Call{T: TCall, ID: id, Op: op, Args: args} }, func(r *Result) { ch <- r }); err != nil {
		return fmt.Errorf("%s: %w", op, err)
	}
	select {
	case r := <-ch:
		if r == nil {
			return fmt.Errorf("%s: %w", op, ErrNotConnected)
		}
		if !r.OK {
			if r.Error == nil {
				return &Error{Code: ErrFailed, Message: op + " failed"}
			}
			return r.Error
		}
		if out != nil && len(r.Value) > 0 {
			if err := json.Unmarshal(r.Value, out); err != nil {
				return fmt.Errorf("%s: %w", op, err)
			}
		}
		return nil
	case <-ctx.Done():
		return fmt.Errorf("%s: %w", op, ctx.Err())
	case <-s.done:
		return fmt.Errorf("%s: %w", op, ErrNotConnected)
	}
}
