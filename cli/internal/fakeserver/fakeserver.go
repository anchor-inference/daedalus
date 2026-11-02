// Package fakeserver is a Daedalus server small enough to reason about, for the client's tests: the
// pairing routes, the session routes and their stream, the global event stream and the device relay
// of docs/architecture/cli.md, with an agent whose turn is a function the test writes. It keeps the
// server's side of the disconnect rules (hold a call while the relay is down, collect a finished
// result with device.result, never send a call twice), so the client's side can be tested against
// them, and it can drop any connection on command.
package fakeserver

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
)

// Server is the fake.
type Server struct {
	*httptest.Server

	// Turn is the agent: what happens when a message arrives. Nil answers "ok: <prompt>".
	Turn func(t *Turn)
	// Keepalive is how often an idle stream sends its comment.
	Keepalive time.Duration
	// ResendOnReconnect makes the fake re-send a call that was in flight when the relay dropped,
	// under the same id, as a server that ignored `known` would: the client must answer it from its
	// file and not run it again.
	ResendOnReconnect bool

	mu        sync.Mutex
	tokens    map[string]string
	pairs     map[string]*pair
	sessions  map[string]*Session
	nextID    int
	relay     *websocket.Conn
	relayGen  int
	relayCond *sync.Cond
	refuse    int
	hellos    []Hello
	guards    []string
	calls     map[string]chan rpcResponse
	sent      map[string]int
	streams   map[chan struct{}]bool
	appSubs   map[chan appEvent]bool
	appSeq    int64
	presence  []map[string]any
	approvals map[string]chan string
}

// Hello is a device.hello the fake received.
type Hello struct {
	Sessions []map[string]string `json:"sessions"`
	Known    []map[string]string `json:"known"`
	Protocol int                 `json:"protocol"`
}

type pair struct {
	userCode string
	approved bool
	denied   bool
	handed   bool
	name     string
}

type rpcResponse struct {
	Result json.RawMessage `json:"result"`
	Error  *RPCError       `json:"error"`
}

// RPCError is a call's failure as the device reported it.
type RPCError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

func (e *RPCError) Error() string { return fmt.Sprintf("%s (%d)", e.Message, e.Code) }

// Reason is the refusal reason a 1004 carries.
func (e *RPCError) Reason() string {
	var d struct {
		Reason string `json:"reason"`
	}
	_ = json.Unmarshal(e.Data, &d)
	return d.Reason
}

// New starts a fake. Close it when done.
func New() *Server {
	s := &Server{
		Keepalive: 200 * time.Millisecond,
		tokens:    map[string]string{},
		pairs:     map[string]*pair{},
		sessions:  map[string]*Session{},
		calls:     map[string]chan rpcResponse{},
		sent:      map[string]int{},
		streams:   map[chan struct{}]bool{},
		appSubs:   map[chan appEvent]bool{},
		approvals: map[string]chan string{},
	}
	s.relayCond = sync.NewCond(&s.mu)
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/cli/pair/start", s.pairStart)
	mux.HandleFunc("POST /api/cli/pair/poll", s.pairPoll)
	mux.HandleFunc("GET /api/cli/me", s.authed(s.me))
	mux.HandleFunc("GET /api/cli/sessions", s.authed(s.resumeList))
	mux.HandleFunc("POST /api/cli/sessions/{id}/attach", s.authed(s.attach))
	mux.HandleFunc("PUT /api/cli/sessions/{id}/guard", s.authed(s.putGuard))
	mux.HandleFunc("GET /api/cli/relay", s.relayHandler)
	mux.HandleFunc("POST /api/sessions", s.authed(s.createSession))
	mux.HandleFunc("GET /api/sessions", s.authed(s.listSessions))
	mux.HandleFunc("GET /api/sessions/{id}", s.authed(s.getSession))
	mux.HandleFunc("GET /api/sessions/{id}/stream", s.authed(s.stream))
	mux.HandleFunc("GET /api/sessions/{id}/steer", s.authed(func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, []any{}) }))
	mux.HandleFunc("GET /api/sessions/{id}/policy/similar/{key}", s.authed(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"similar": nil})
	}))
	mux.HandleFunc("POST /api/sessions/{id}/messages", s.authed(s.postMessage))
	mux.HandleFunc("POST /api/sessions/{id}/stop", s.authed(s.stop))
	mux.HandleFunc("POST /api/sessions/{id}/policy/{decision}", s.authed(s.policy))
	mux.HandleFunc("POST /api/presence", s.authed(s.reportPresence))
	mux.HandleFunc("GET /api/events", s.authed(s.events))
	mux.HandleFunc("GET /api/commands", s.authed(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, []map[string]any{{"name": "/usage", "args": "", "description": "spend", "scope": "session", "confirm": false}})
	}))
	mux.HandleFunc("GET /api/skills", s.authed(func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, []any{}) }))
	s.Server = httptest.NewServer(mux)
	return s
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// AddToken registers a device token, as a finished pairing would have.
func (s *Server) AddToken(token, deviceID string) {
	s.mu.Lock()
	s.tokens[token] = deviceID
	s.mu.Unlock()
}

// Revoke drops every token and closes the relay with 4401.
func (s *Server) Revoke() {
	s.mu.Lock()
	s.tokens = map[string]string{}
	conn := s.relay
	s.mu.Unlock()
	if conn != nil {
		_ = conn.Close(4401, "revoked")
	}
}

func (s *Server) authed(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.deviceOf(r) == "" {
			writeJSON(w, 401, map[string]any{"detail": "no device", "code": "device_token_invalid"})
			return
		}
		h(w, r)
	}
}

func (s *Server) deviceOf(r *http.Request) string {
	token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Device ")
	if !ok {
		return ""
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.tokens[token]
}

// Pairing.

func (s *Server) pairStart(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Name string `json:"name"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	code := randomHex(16)
	user := strings.ToUpper(randomHex(2) + "-" + randomHex(2))
	s.mu.Lock()
	s.pairs[code] = &pair{userCode: user, name: body.Name}
	s.mu.Unlock()
	writeJSON(w, 200, map[string]any{"device_code": code, "user_code": user, "verification_uri": s.URL + "/app/?pair-device",
		"verification_uri_complete": s.URL + "/app/?pair-device=" + user, "expires_in": 600, "interval": 1})
}

// Approve approves the pending pairing with this user code; Deny refuses it.
func (s *Server) Approve(userCode string) bool { return s.decide(userCode, true) }
func (s *Server) Deny(userCode string) bool    { return s.decide(userCode, false) }

func (s *Server) decide(userCode string, allow bool) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, p := range s.pairs {
		if p.userCode == userCode {
			p.approved, p.denied = allow, !allow
			return true
		}
	}
	return false
}

// PendingCodes are the user codes waiting for a decision.
func (s *Server) PendingCodes() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []string
	for _, p := range s.pairs {
		if !p.approved && !p.denied {
			out = append(out, p.userCode)
		}
	}
	return out
}

func (s *Server) pairPoll(w http.ResponseWriter, r *http.Request) {
	var body struct {
		DeviceCode string `json:"device_code"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	s.mu.Lock()
	p := s.pairs[body.DeviceCode]
	if p == nil {
		s.mu.Unlock()
		writeJSON(w, 404, map[string]any{"status": "unknown"})
		return
	}
	switch {
	case p.denied:
		s.mu.Unlock()
		writeJSON(w, 200, map[string]any{"status": "denied"})
	case p.handed:
		s.mu.Unlock()
		writeJSON(w, 200, map[string]any{"status": "expired"})
	case p.approved:
		p.handed = true
		token := "dcl_" + randomHex(20)
		id := "dev-" + randomHex(3)
		s.tokens[token] = id
		s.mu.Unlock()
		writeJSON(w, 200, map[string]any{"status": "approved", "token": token, "device": map[string]any{"id": id, "name": p.name}})
	default:
		s.mu.Unlock()
		writeJSON(w, 200, map[string]any{"status": "pending", "interval": 1})
	}
}

func (s *Server) me(w http.ResponseWriter, r *http.Request) {
	dev := s.deviceOf(r)
	s.mu.Lock()
	connected := s.relay != nil
	var ids []string
	for id := range s.sessions {
		ids = append(ids, id)
	}
	s.mu.Unlock()
	writeJSON(w, 200, map[string]any{"device": map[string]any{"id": dev, "name": "test-device", "connected": connected},
		"server": map[string]any{"version": "test", "contract": 1}, "sessions": ids})
}

func (s *Server) putGuard(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Guard string `json:"guard"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	s.mu.Lock()
	s.guards = append(s.guards, body.Guard)
	s.mu.Unlock()
	writeJSON(w, 200, map[string]any{"ok": true})
}

// Guards are the guard levels the client reported, in order, by either route.
func (s *Server) Guards() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string{}, s.guards...)
}

// Presence are the presence reports received.
func (s *Server) Presence() []map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]map[string]any{}, s.presence...)
}

func (s *Server) reportPresence(w http.ResponseWriter, r *http.Request) {
	var body map[string]any
	_ = json.NewDecoder(r.Body).Decode(&body)
	s.mu.Lock()
	s.presence = append(s.presence, body)
	s.mu.Unlock()
	w.WriteHeader(204)
}

func (s *Server) newID(prefix string) string {
	s.nextID++
	return prefix + strconv.Itoa(s.nextID)
}
