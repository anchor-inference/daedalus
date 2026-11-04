package relay

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"sync"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
)

// The DevTools connection over a relay. The daemon's page, view and workflow packages speak to a tab
// through a *cdp.Conn by session id; over a relay the session ids are the relay's own ("x<tabId>",
// "x<tabId>/<sessionId>"), and the connection's pipe is this adapter: each message cdp writes
// becomes a cdp frame, and each result and event of the relay becomes the message cdp reads. So
// everything above it runs unchanged, and the one thing it cannot do is what the relay must never
// carry: a command with no session, to the browser itself, fails here at once.

// NewConn returns a DevTools connection over this relay; handler gets its events, in order.
// dispatched, when set, is told of every Input command the daemon sends to a tab, so the operator's
// own input can be told from the daemon's. A second NewConn replaces the first, whose connection
// ends; so does drop.
func (s *Session) NewConn(handler func(cdp.Event), dispatched func(tab int)) (conn *cdp.Conn, drop func()) {
	conn, a := newAdapter(handler, dispatched, false)
	a.bind(s)
	return conn, a.close
}

// Line is a DevTools connection that outlives the relay it runs over: when the relay ends, the
// calls waiting on it fail with why and the connection stays, refusing commands until Bind gives it
// the relay the same Chrome made again. A relay that dropped for a second once ended the browser,
// renumbered the operator's tabs and lost the conversation's grants; the attach backend keeps its
// browser on a Line through a reconnect.
type Line struct{ a *adapter }

// NewLine returns a connection bound to no relay yet; Bind gives it one.
func NewLine(handler func(cdp.Event), dispatched func(tab int)) (*cdp.Conn, *Line) {
	conn, a := newAdapter(handler, dispatched, true)
	return conn, &Line{a: a}
}

// Bind makes s the relay the connection's commands go over, and its events come from.
func (l *Line) Bind(s *Session) { l.a.bind(s) }

// Close ends the connection for good.
func (l *Line) Close() { l.a.close() }

// ErrDropped is the text of a call whose relay ended while it waited, on a Line.
const ErrDropped = "the connection to Chrome dropped during the call; Chrome is reconnecting, so try the call again"

// ErrReconnecting is the text of a command sent on a Line while it has no relay.
const ErrReconnecting = "Chrome is reconnecting to Daedalus; try the call again in a moment"

func newAdapter(handler func(cdp.Event), dispatched func(tab int), durable bool) (*cdp.Conn, *adapter) {
	pr, pw := io.Pipe()
	a := &adapter{pw: pw, wake: make(chan struct{}, 1), done: make(chan struct{}), dispatched: dispatched, durable: durable}
	go a.pump()
	return cdp.New(pr, a, handler), a
}

// bind makes s the adapter's relay. A relay holds one connection: one it held before is ended, or,
// on a Line, let go.
func (a *adapter) bind(s *Session) {
	s.mu.Lock()
	old := s.conn
	ended := s.ended
	s.conn = a
	s.mu.Unlock()
	if old != nil && old != a {
		old.lost(s)
	}
	a.mu.Lock()
	a.s = s
	a.mu.Unlock()
	if ended {
		a.lost(s)
	}
}

// lost is the relay s ending under the adapter: a connection of that relay alone ends with it, a
// Line waits for the next.
func (a *adapter) lost(s *Session) {
	if !a.durable {
		a.close()
		return
	}
	a.mu.Lock()
	if a.s == s {
		a.s = nil
	}
	a.mu.Unlock()
}

func (a *adapter) session() *Session {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.s
}

type adapter struct {
	s          *Session // under mu; nil on a Line between relays
	durable    bool
	pw         *io.PipeWriter
	dispatched func(tab int)

	mu     sync.Mutex
	queue  [][]byte
	closed bool
	wake   chan struct{}
	done   chan struct{}
	once   sync.Once
}

// message is one DevTools message as cdp writes and reads it.
type message struct {
	ID        int64           `json:"id,omitempty"`
	Method    string          `json:"method,omitempty"`
	Params    json.RawMessage `json:"params,omitempty"`
	Result    json.RawMessage `json:"result,omitempty"`
	Error     *cdp.Error      `json:"error,omitempty"`
	SessionID string          `json:"sessionId,omitempty"`
}

// Write takes one command from cdp, which writes each in one call ended by a NUL. The frame is sent
// before Write returns, so commands reach the extension in the order cdp sent them: a paused frame's
// setup, then the command that lets it run, is that order or it never runs.
func (a *adapter) Write(b []byte) (int, error) {
	var m message
	if err := json.Unmarshal(bytes.TrimSuffix(b, []byte{0}), &m); err != nil {
		return 0, err
	}
	refuse := func(why string) (int, error) {
		a.reply(message{ID: m.ID, Error: &cdp.Error{Code: -32000, Message: why}})
		return len(b), nil
	}
	if m.SessionID == "" {
		return refuse(m.Method + " is a browser-level command, and the operator's Chrome takes none")
	}
	tab, _, ok := ParseSession(m.SessionID)
	if !ok {
		return refuse(fmt.Sprintf("%q is not a relay session", m.SessionID))
	}
	if err := Allowed(m.Method); err != nil {
		return refuse(err.Error())
	}
	params := m.Params
	if m.Method == "Target.detachFromTarget" {
		var err error
		if params, err = localChild(params, tab); err != nil {
			return refuse(err.Error())
		}
	}
	if strings.HasPrefix(m.Method, "Input.") && a.dispatched != nil {
		a.dispatched(tab)
	}
	id := m.ID
	s := a.session()
	if s == nil {
		return refuse(ErrReconnecting)
	}
	err := s.register(func(rid int64) any {
		return CDP{T: TCDP, ID: rid, Session: m.SessionID, Method: m.Method, Params: params}
	}, func(r *Result) { a.result(id, r) })
	switch {
	case err == ErrTooLarge:
		return refuse(fmt.Sprintf("%s is larger than the relay carries to Chrome (1 MB)", m.Method))
	case err == ErrNotConnected && a.durable:
		return refuse(ErrReconnecting)
	case err != nil:
		return 0, err
	}
	return len(b), nil
}

// localChild turns the sessionId of Target.detachFromTarget back from the daemon's "x<tab>/<sid>"
// into the sid Chrome knows on that tab.
func localChild(params json.RawMessage, tab int) (json.RawMessage, error) {
	var p map[string]json.RawMessage
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, err
	}
	var sid string
	if raw, ok := p["sessionId"]; ok {
		if err := json.Unmarshal(raw, &sid); err != nil {
			return nil, err
		}
	}
	t, child, ok := ParseSession(sid)
	if !ok || child == "" || t != tab {
		return nil, fmt.Errorf("%q is not a child session of tab %d", sid, tab)
	}
	p["sessionId"], _ = json.Marshal(child)
	return json.Marshal(p)
}

// result turns a relay result into cdp's reply. Chrome's own DevTools errors come through the
// extension as their text, which is JSON ({"code":…,"message":…}) when Chrome made it; its code is
// kept, so a caller that tells errors apart by code still can.
func (a *adapter) result(id int64, r *Result) {
	if r == nil {
		if a.durable {
			// The relay ended; the connection waits for the next one, and the call is told why.
			a.reply(message{ID: id, Error: &cdp.Error{Code: -32000, Message: ErrDropped}})
		}
		return // the relay ended: closing the pipe fails the call
	}
	if r.OK {
		v := r.Value
		if len(v) == 0 || string(v) == "null" {
			v = json.RawMessage("{}")
		}
		a.reply(message{ID: id, Result: v})
		return
	}
	e := &cdp.Error{Code: -32000, Message: "the relay refused the command"}
	if r.Error != nil {
		e.Message = r.Error.Error()
		var inner cdp.Error
		if r.Error.Code == ErrCDP && json.Unmarshal([]byte(r.Error.Message), &inner) == nil && inner.Message != "" {
			e = &inner
		}
	}
	a.reply(message{ID: id, Error: e})
}

// event turns a relay event into cdp's. The session id of a child the tab's auto-attach announces is
// Chrome's id on that tab; the daemon's name for it carries the tab, so a command to it reaches it.
func (a *adapter) event(e Event) {
	tab, _, ok := ParseSession(e.Session)
	if !ok {
		return
	}
	params := e.Params
	if e.Method == "Target.attachedToTarget" || e.Method == "Target.detachedFromTarget" {
		var p map[string]json.RawMessage
		var sid string
		if json.Unmarshal(params, &p) == nil && json.Unmarshal(p["sessionId"], &sid) == nil && sid != "" {
			p["sessionId"], _ = json.Marshal(SessionOf(tab) + "/" + sid)
			params, _ = json.Marshal(p)
		}
	}
	a.reply(message{Method: e.Method, Params: params, SessionID: e.Session})
}

// reply queues one message for cdp to read; the pump writes them in order.
func (a *adapter) reply(m message) {
	b, err := json.Marshal(m)
	if err != nil {
		return
	}
	a.mu.Lock()
	if a.closed {
		a.mu.Unlock()
		return
	}
	a.queue = append(a.queue, append(b, 0))
	a.mu.Unlock()
	select {
	case a.wake <- struct{}{}:
	default:
	}
}

func (a *adapter) pump() {
	for {
		a.mu.Lock()
		q := a.queue
		a.queue = nil
		closed := a.closed
		a.mu.Unlock()
		for _, b := range q {
			if _, err := a.pw.Write(b); err != nil {
				return
			}
		}
		if len(q) > 0 {
			continue
		}
		if closed {
			_ = a.pw.Close()
			return
		}
		select {
		case <-a.wake:
		case <-a.done:
		}
	}
}

// close ends the connection: what was queued is still read, then cdp sees its pipe end and fails
// every call still waiting.
func (a *adapter) close() {
	a.once.Do(func() {
		a.mu.Lock()
		a.closed = true
		a.mu.Unlock()
		close(a.done)
	})
}
