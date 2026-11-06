package relay

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"sort"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/version"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The relay's liveness: a ping every PingInterval, and the relay is lost after MissedPings of them
// go unanswered. A worker the browser suspended, a bridge whose Chrome quit without closing its
// pipe, a WebSocket whose network went away: each would otherwise hold its Chrome's place for ever,
// and a Chrome that came back would replace a relay nobody could tell was dead.
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

// ErrNotConnected is a call made while the Chrome it names has no relay.
var ErrNotConnected = errors.New("Chrome is not connected")

// keepChromes bounds how many Chromes that went away the hub remembers for a status. A Chrome that is
// connected is never dropped; a host pairs at most twenty, and a computer has a handful of profiles.
const keepChromes = 64

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

// Hub holds the relays a `user` daemon has: one for each Chrome connected, however many that is. They
// share nothing but the daemon: each has its own place, so a Chrome going away, or reconnecting,
// says nothing about any other. One daemon rather than one per Chrome, because this one launches no
// browser and costs a few goroutines per relay, and because a server's compose stack has one
// browser-user service and nothing that could start another.
type Hub struct {
	o Options

	mu sync.Mutex
	// places are the Chromes heard from, connected or not, by ChromeID.
	places map[string]*place
	// lastError is the newest refusal of a relay that never became a Chrome: a broken hello, a
	// protocol the daemon does not speak.
	lastError string
	closed    bool
	// onSession is told of each relay accepted, before its welcome goes out and so before the tabs
	// the extension replays after the welcome: the attach backend binds its browser to it there.
	onSession func(*Session)
	// connected is closed and made anew each time any relay is accepted.
	connected chan struct{}
}

// place is one Chrome as the hub knows it.
type place struct {
	cur       *Session
	last      Status // the relay that was connected last, for a status while none is
	lastError string
	// lostAt is when its relay last ended by itself (not replaced, not the daemon stopping): while it
	// is recent the same Chrome is expected back, and a call waits for it (Hub.Expected).
	lostAt time.Time
	// connected is closed and made anew each time this Chrome's relay is accepted.
	connected chan struct{}
}

var plainChromeID = regexp.MustCompile(`^[0-9a-z]{1,16}$`)

// ChromeID is what the daemon and the host call the Chrome a hello speaks for. A server's host puts
// its paired device's id in the hello, which is proved by the token and short enough to go in a
// group's id; natively the extension's profile id is the operator's own and of any shape, so its
// digest is used. Nothing here is a secret, only a name that stays the same.
func ChromeID(profileID string) string {
	if plainChromeID.MatchString(profileID) {
		return profileID
	}
	sum := sha256.Sum256([]byte(profileID))
	return hex.EncodeToString(sum[:6])
}

// anonymousChrome names a relay whose hello gave no profile: nothing connects to it again, so it
// never takes another Chrome's place.
func anonymousChrome() string {
	raw := make([]byte, 6)
	_, _ = rand.Read(raw)
	return hex.EncodeToString(raw)
}

// OnSession names who is told of each relay accepted, before its welcome goes out.
func (h *Hub) OnSession(f func(*Session)) {
	h.mu.Lock()
	h.onSession = f
	h.mu.Unlock()
}

// Expected reports whether chrome has no relay but its last one ended by itself less than within ago:
// the same Chrome reconnecting, which its extension does within seconds. An empty chrome asks about
// any Chrome.
func (h *Hub) Expected(chrome string, within time.Duration) (bool, time.Duration) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		return false, 0
	}
	var most time.Duration
	found := false
	for id, pl := range h.places {
		if (chrome != "" && id != chrome) || pl.cur != nil || pl.lostAt.IsZero() {
			continue
		}
		if left := within - time.Since(pl.lostAt); left > 0 {
			found = true
			most = max(most, left)
		}
	}
	return found, most
}

// Candidates are the Chromes an unnamed call could mean: connected now, or expected back within.
func (h *Hub) Candidates(within time.Duration) []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	var out []string
	for id, pl := range h.places {
		if pl.cur != nil || (!pl.lostAt.IsZero() && time.Since(pl.lostAt) < within) {
			out = append(out, id)
		}
	}
	sort.Strings(out)
	return out
}

// Connected is closed when the next relay of chrome is accepted; of any Chrome when chrome is empty.
func (h *Hub) Connected(chrome string) <-chan struct{} {
	h.mu.Lock()
	defer h.mu.Unlock()
	if chrome == "" {
		if h.connected == nil {
			h.connected = make(chan struct{})
		}
		return h.connected
	}
	pl := h.places[chrome]
	if pl == nil {
		return nil // never connected, so nothing to wait for: Expected says no
	}
	if pl.connected == nil {
		pl.connected = make(chan struct{})
	}
	return pl.connected
}

// placeOf is chrome's place, made when it has none. The caller holds h.mu.
func (h *Hub) placeOf(chrome string) *place {
	pl := h.places[chrome]
	if pl == nil {
		pl = &place{}
		h.places[chrome] = pl
		h.forgetOldest()
	}
	return pl
}

// forgetOldest keeps the table of Chromes that went away bounded. The caller holds h.mu.
func (h *Hub) forgetOldest() {
	for len(h.places) > keepChromes {
		oldest := ""
		var at time.Time
		for id, pl := range h.places {
			if pl.cur != nil || pl.last.Since == nil {
				continue
			}
			if oldest == "" || pl.last.Since.Before(at) {
				oldest, at = id, *pl.last.Since
			}
		}
		if oldest == "" {
			return
		}
		delete(h.places, oldest)
	}
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
	return &Hub{o: o, places: map[string]*place{}}
}

func (h *Hub) publish(typ string, data any) {
	if h.o.Publish != nil {
		h.o.Publish(typ, data)
	}
}

// Status is one Chrome as daemon.info and relay.status report it. Reconnecting, Groups and Tabs are
// the attach backend's to fill in (Manager.RelayReport): the hub knows the relay, not the browser.
type Status struct {
	Chrome        string     `json:"chrome,omitempty"`
	Connected     bool       `json:"connected"`
	Reconnecting  bool       `json:"reconnecting,omitempty"`
	ProfileID     string     `json:"profile_id,omitempty"`
	ProfileName   string     `json:"profile_name,omitempty"`
	ExtVersion    string     `json:"ext_version,omitempty"`
	ChromeVersion string     `json:"chrome_version,omitempty"`
	Platform      string     `json:"platform,omitempty"`
	Transport     string     `json:"transport,omitempty"`
	Since         *time.Time `json:"since"`
	LastSeen      *time.Time `json:"last_seen"`
	LastError     string     `json:"last_error,omitempty"`
	Groups        int        `json:"groups"`
	Tabs          int        `json:"tabs"`
}

// Report is the relays of a daemon: every Chrome it has heard from, the connected ones first, oldest
// connection first, so a list read twice stays in one order.
type Report struct {
	// Connected says any Chrome is.
	Connected bool `json:"connected"`
	// LastError is the newest refusal that belongs to no Chrome, else the newest Chrome's loss.
	LastError string   `json:"last_error,omitempty"`
	Chromes   []Status `json:"chromes"`
}

// Status is the relays now.
func (h *Hub) Status() Report {
	h.mu.Lock()
	r := Report{LastError: h.lastError, Chromes: []Status{}}
	type entry struct {
		id        string
		cur       *Session
		last      Status
		lastError string
	}
	var entries []entry
	for id, pl := range h.places {
		if pl.cur == nil && pl.last.Since == nil {
			continue // only ever waited for
		}
		entries = append(entries, entry{id, pl.cur, pl.last, pl.lastError})
	}
	h.mu.Unlock()
	for _, e := range entries {
		st := e.last
		if e.cur != nil {
			st = e.cur.status()
		}
		st.Chrome, st.LastError = e.id, e.lastError
		st.Connected = e.cur != nil
		r.Connected = r.Connected || st.Connected
		r.Chromes = append(r.Chromes, st)
	}
	sort.Slice(r.Chromes, func(i, j int) bool {
		a, b := r.Chromes[i], r.Chromes[j]
		if a.Connected != b.Connected {
			return a.Connected
		}
		if a.Since != nil && b.Since != nil && !a.Since.Equal(*b.Since) {
			return a.Since.Before(*b.Since)
		}
		return a.Chrome < b.Chrome
	})
	if r.LastError == "" {
		for _, st := range r.Chromes {
			if st.LastError != "" {
				r.LastError = st.LastError
				break
			}
		}
	}
	return r
}

// Session is chrome's connected relay, or nil. An empty chrome means the only one connected, and nil
// while there are several: a call that names no Chrome must never land on one by the order of a map.
func (h *Hub) Session(chrome string) *Session {
	h.mu.Lock()
	defer h.mu.Unlock()
	if chrome != "" {
		if pl := h.places[chrome]; pl != nil {
			return pl.cur
		}
		return nil
	}
	var only *Session
	for _, pl := range h.places {
		if pl.cur == nil {
			continue
		}
		if only != nil {
			return nil
		}
		only = pl.cur
	}
	return only
}

// Close ends every relay with a bye saying the daemon stops, and refuses any other.
func (h *Hub) Close() {
	h.mu.Lock()
	h.closed = true
	var live []*Session
	for _, pl := range h.places {
		if pl.cur != nil {
			live = append(live, pl.cur)
		}
	}
	h.mu.Unlock()
	var wg sync.WaitGroup
	for _, s := range live {
		wg.Add(1)
		go func() {
			defer wg.Done()
			s.end(LostShutdown, &Bye{T: TBye, Reason: ByeShutdown, Message: "Daedalus is stopping"})
		}()
	}
	wg.Wait()
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
	chrome := anonymousChrome()
	if hi.ProfileID != "" {
		chrome = ChromeID(hi.ProfileID)
	}
	pl := h.placeOf(chrome)
	old := pl.cur
	now := time.Now().UTC()
	s := &Session{h: h, link: l, hello: hi, chrome: chrome, transport: l.transport, since: now, lastSeen: now,
		pending: map[int64]func(*Result){}, done: make(chan struct{}), observed: make(chan func(), 256)}
	pl.cur = s
	pl.lastError = ""
	pl.lostAt = time.Time{}
	h.lastError = ""
	told := h.onSession
	connected, anyone := pl.connected, h.connected
	pl.connected, h.connected = nil, nil
	h.mu.Unlock()
	if old != nil {
		// The same Chrome profile again: its worker restarted, or its bridge outlived a Chrome that
		// quit. The new relay is the live one.
		old.end(LostReplaced, &Bye{T: TBye, Reason: ByeReplaced, Message: "this Chrome connected again"})
	}
	l.mu.Lock()
	l.s = s
	l.mu.Unlock()
	if told != nil {
		told(s)
	}
	// After the browser is bound to it, so a call that waited for the relay finds it bound.
	if connected != nil {
		close(connected)
	}
	if anyone != nil {
		close(anyone)
	}
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
	h.o.Log.Info("relay connected", "chrome", chrome, "profile", hi.ProfileName, "extension", hi.ExtVersion, "chrome_version", hi.ChromeVersion, "transport", l.transport)
	h.publish("relay.connected", map[string]any{"chrome": chrome, "profile_id": hi.ProfileID, "profile_name": hi.ProfileName, "ext_version": hi.ExtVersion,
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
	chrome    string
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

// Chrome is the Chrome this relay is of (ChromeID).
func (s *Session) Chrome() string { return s.chrome }

func (s *Session) status() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	since, seen := s.since, s.lastSeen
	return Status{Chrome: s.chrome, Connected: !s.ended, ProfileID: s.hello.ProfileID, ProfileName: s.hello.ProfileName, ExtVersion: s.hello.ExtVersion,
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
	pl := h.places[s.chrome]
	if pl != nil && pl.cur == s {
		pl.cur = nil
		pl.last = s.status()
		pl.last.Connected = false
	}
	if pl != nil && reason != LostReplaced && reason != LostShutdown {
		pl.lastError = lostWords(reason)
		if pl.cur == nil {
			pl.lostAt = time.Now()
		}
	}
	h.mu.Unlock()
	close(s.done)
	for _, f := range pending {
		f(nil)
	}
	if conn != nil {
		conn.lost(s)
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
	h.o.Log.Info("relay lost", "chrome", s.chrome, "profile", s.hello.ProfileName, "reason", reason)
	h.publish("relay.lost", map[string]any{"chrome": s.chrome, "reason": reason, "profile_id": s.hello.ProfileID, "profile_name": s.hello.ProfileName})
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
		s.h.publish("relay.check", map[string]any{"n": c.N, "chrome": s.chrome, "profile_id": s.hello.ProfileID, "profile_name": s.hello.ProfileName})
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
