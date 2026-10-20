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
	pr, pw := io.Pipe()
	a := &adapter{s: s, pw: pw, wake: make(chan struct{}, 1), done: make(chan struct{}), dispatched: dispatched}
	s.mu.Lock()
	old := s.conn
	ended := s.ended
	s.conn = a
	s.mu.Unlock()
	if old != nil {
		old.close()
	}
	go a.pump()
	conn = cdp.New(pr, a, handler)
	if ended {
		a.close()
	}
	return conn, a.close
}

type adapter struct {
	s          *Session
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
	err := a.s.register(func(rid int64) any {
		return CDP{T: TCDP, ID: rid, Session: m.SessionID, Method: m.Method, Params: params}
	}, func(r *Result) { a.result(id, r) })
	switch {
	case err == ErrTooLarge:
		return refuse(fmt.Sprintf("%s is larger than the relay carries to Chrome (1 MB)", m.Method))
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
