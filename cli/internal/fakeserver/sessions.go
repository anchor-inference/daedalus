package fakeserver

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"sync"
	"time"
)

// Session is one conversation of the fake.
type Session struct {
	ID    string
	Title string
	Cwd   string
	Guard string

	mu        sync.Mutex
	messages  []map[string]any
	log       []envelope
	seq       int64
	subs      map[chan envelope]bool
	running   bool
	runID     string
	cancelRun context.CancelFunc
	approvals []string
}

type envelope struct {
	Kind      string         `json:"kind"`
	Seq       int64          `json:"event_seq"`
	RunID     string         `json:"run_id"`
	Revision  int64          `json:"history_revision"`
	Payload   map[string]any `json:"payload"`
	Ephemeral bool           `json:"ephemeral,omitempty"`
}

// Messages is the transcript.
func (ss *Session) Messages() []map[string]any {
	ss.mu.Lock()
	defer ss.mu.Unlock()
	return append([]map[string]any{}, ss.messages...)
}

func (ss *Session) publish(kind, runID string, payload map[string]any, ephemeral bool) {
	ss.mu.Lock()
	env := envelope{Kind: kind, RunID: runID, Payload: payload, Ephemeral: ephemeral}
	if !ephemeral {
		ss.seq++
		env.Seq = ss.seq
		ss.log = append(ss.log, env)
	}
	subs := make([]chan envelope, 0, len(ss.subs))
	for ch := range ss.subs {
		subs = append(subs, ch)
	}
	ss.mu.Unlock()
	for _, ch := range subs {
		select {
		case ch <- env:
		default:
		}
	}
}

func (ss *Session) addMessage(m map[string]any) {
	ss.mu.Lock()
	m["seq"] = len(ss.messages) + 1
	m["created_at"] = time.Now().UTC().Format(time.RFC3339Nano)
	ss.messages = append(ss.messages, m)
	ss.mu.Unlock()
}

// Session returns a session by id.
func (s *Server) Session(id string) *Session {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.sessions[id]
}

// Sessions are every session, in no order.
func (s *Server) Sessions() []*Session {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*Session, 0, len(s.sessions))
	for _, ss := range s.sessions {
		out = append(out, ss)
	}
	return out
}

func (s *Server) session(w http.ResponseWriter, r *http.Request) *Session {
	ss := s.Session(r.PathValue("id"))
	if ss == nil {
		writeJSON(w, 404, map[string]any{"detail": "no such session"})
	}
	return ss
}

func (s *Server) createSession(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Kind  string `json:"kind"`
		Cwd   string `json:"cwd"`
		Title string `json:"title"`
		Guard string `json:"guard"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	if body.Kind != "remote" {
		writeJSON(w, 403, map[string]any{"detail": "a device creates remote sessions only"})
		return
	}
	if body.Cwd == "" {
		writeJSON(w, 400, map[string]any{"detail": "cwd is required"})
		return
	}
	s.mu.Lock()
	id := s.newID("s")
	ss := &Session{ID: id, Title: body.Title, Cwd: body.Cwd, Guard: body.Guard, subs: map[chan envelope]bool{}}
	if ss.Title == "" {
		ss.Title = "untitled"
	}
	s.sessions[id] = ss
	s.mu.Unlock()
	writeJSON(w, 200, map[string]any{"id": id, "title": ss.Title, "model": "fake-model", "kind": "remote", "remote": s.remoteOf(ss)})
}

func (s *Server) remoteOf(ss *Session) map[string]any {
	s.mu.Lock()
	connected := s.relay != nil
	s.mu.Unlock()
	return map[string]any{"device_id": "dev-1", "device_name": "test-device", "cwd": ss.Cwd, "connected": connected, "guard": ss.Guard, "waiting": nil}
}

func (s *Server) getSession(w http.ResponseWriter, r *http.Request) {
	ss := s.session(w, r)
	if ss == nil {
		return
	}
	ss.mu.Lock()
	status := "idle"
	if ss.running {
		status = "running"
	}
	body := map[string]any{"id": ss.ID, "title": ss.Title, "status": status, "kind": "remote", "model": "fake-model",
		"messages": append([]map[string]any{}, ss.messages...), "open_approvals": append([]string{}, ss.approvals...),
		"pending": nil, "usage": map[string]any{"i": 10, "o": 20}, "context": map[string]any{}}
	if ss.runID != "" {
		body["run_id"] = ss.runID
	}
	ss.mu.Unlock()
	body["remote"] = s.remoteOf(ss)
	writeJSON(w, 200, body)
}

func (s *Server) listSessions(w http.ResponseWriter, r *http.Request) {
	var rows []map[string]any
	for _, ss := range s.Sessions() {
		rows = append(rows, map[string]any{"id": ss.ID, "title": ss.Title, "status": "idle", "kind": "remote", "last_message_at": time.Now().UTC().Format(time.RFC3339)})
	}
	writeJSON(w, 200, map[string]any{"sessions": rows})
}

func (s *Server) resumeList(w http.ResponseWriter, r *http.Request) {
	cwd := r.URL.Query().Get("cwd")
	var rows []map[string]any
	for _, ss := range s.Sessions() {
		rows = append(rows, map[string]any{"id": ss.ID, "title": ss.Title, "status": "idle", "kind": "remote",
			"last_message_at": time.Now().UTC().Format(time.RFC3339), "remote": s.remoteOf(ss), "attached": true, "here": ss.Cwd == cwd})
	}
	writeJSON(w, 200, map[string]any{"sessions": rows})
}

func (s *Server) attach(w http.ResponseWriter, r *http.Request) {
	ss := s.session(w, r)
	if ss == nil {
		return
	}
	writeJSON(w, 200, map[string]any{"id": ss.ID, "title": ss.Title, "kind": "remote", "attached": true})
}

// DropStreams ends every open session and event stream, as a proxy restart would.
func (s *Server) DropStreams() {
	s.mu.Lock()
	for ch := range s.streams {
		close(ch)
		delete(s.streams, ch)
	}
	s.mu.Unlock()
}

func (s *Server) trackStream() chan struct{} {
	ch := make(chan struct{})
	s.mu.Lock()
	s.streams[ch] = true
	s.mu.Unlock()
	return ch
}

func (s *Server) untrackStream(ch chan struct{}) {
	s.mu.Lock()
	if s.streams[ch] {
		delete(s.streams, ch)
	}
	s.mu.Unlock()
}

func (s *Server) stream(w http.ResponseWriter, r *http.Request) {
	ss := s.session(w, r)
	if ss == nil {
		return
	}
	flusher := w.(http.Flusher)
	w.Header().Set("Content-Type", "text/event-stream")
	w.WriteHeader(200)
	sub := make(chan envelope, 256)
	ss.mu.Lock()
	ss.subs[sub] = true
	watermark := ss.seq
	var replay []envelope
	raw := r.URL.Query().Get("after")
	if raw != "" {
		after, _ := strconv.ParseInt(raw, 10, 64)
		for _, e := range ss.log {
			if e.Seq > after {
				replay = append(replay, e)
			}
		}
	}
	ss.mu.Unlock()
	defer func() {
		ss.mu.Lock()
		delete(ss.subs, sub)
		ss.mu.Unlock()
	}()
	dropped := s.trackStream()
	defer s.untrackStream(dropped)
	hello, _ := json.Marshal(map[string]any{"session_id": ss.ID, "event_seq": watermark, "history_revision": 0, "runtime_epoch": "e"})
	fmt.Fprintf(w, "event: hello\ndata: %s\n\n", hello)
	last := int64(0)
	for _, e := range replay {
		data, _ := json.Marshal(e)
		fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", e.Seq, e.Kind, data)
		last = e.Seq
	}
	flusher.Flush()
	ticker := time.NewTicker(s.Keepalive)
	defer ticker.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-dropped:
			return
		case <-ticker.C:
			fmt.Fprint(w, ": keepalive\n\n")
			flusher.Flush()
		case e := <-sub:
			if !e.Ephemeral && e.Seq <= last {
				continue
			}
			data, _ := json.Marshal(e)
			if e.Ephemeral {
				fmt.Fprintf(w, "event: %s\ndata: %s\n\n", e.Kind, data)
			} else {
				last = e.Seq
				fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", e.Seq, e.Kind, data)
			}
			flusher.Flush()
		}
	}
}

type appEvent struct {
	Seq       int64          `json:"seq"`
	Type      string         `json:"type"`
	SessionID string         `json:"session_id"`
	Payload   map[string]any `json:"payload"`
}

// PublishApp sends an event on the global stream.
func (s *Server) PublishApp(kind, sessionID string, payload map[string]any) {
	s.mu.Lock()
	s.appSeq++
	ev := appEvent{Seq: s.appSeq, Type: kind, SessionID: sessionID, Payload: payload}
	subs := make([]chan appEvent, 0, len(s.appSubs))
	for ch := range s.appSubs {
		subs = append(subs, ch)
	}
	s.mu.Unlock()
	for _, ch := range subs {
		select {
		case ch <- ev:
		default:
		}
	}
}

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	flusher := w.(http.Flusher)
	w.Header().Set("Content-Type", "text/event-stream")
	w.WriteHeader(200)
	sub := make(chan appEvent, 64)
	s.mu.Lock()
	s.appSubs[sub] = true
	head := s.appSeq
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.appSubs, sub)
		s.mu.Unlock()
	}()
	dropped := s.trackStream()
	defer s.untrackStream(dropped)
	fmt.Fprintf(w, "event: hello\ndata: {\"head\": %d}\n\n", head)
	flusher.Flush()
	ticker := time.NewTicker(s.Keepalive)
	defer ticker.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-dropped:
			return
		case <-ticker.C:
			fmt.Fprint(w, ": keepalive\n\n")
			flusher.Flush()
		case ev := <-sub:
			data, _ := json.Marshal(ev)
			fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", ev.Seq, ev.Type, data)
			flusher.Flush()
		}
	}
}

func (s *Server) postMessage(w http.ResponseWriter, r *http.Request) {
	ss := s.session(w, r)
	if ss == nil {
		return
	}
	var body struct {
		Text string `json:"text"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	ss.addMessage(map[string]any{"role": "user", "text": body.Text, "origin": "cli"})
	ss.mu.Lock()
	if ss.running {
		ss.mu.Unlock()
		writeJSON(w, 200, map[string]any{"run_id": ss.runID})
		return
	}
	s.mu.Lock()
	runID := s.newID("r")
	s.mu.Unlock()
	ctx, cancel := context.WithCancel(context.Background())
	ss.running, ss.runID, ss.cancelRun = true, runID, cancel
	ss.mu.Unlock()
	go s.runTurn(ctx, ss, runID, body.Text)
	writeJSON(w, 200, map[string]any{"run_id": runID})
}

func (s *Server) runTurn(ctx context.Context, ss *Session, runID, prompt string) {
	t := &Turn{s: s, Session: ss, RunID: runID, Prompt: prompt, Ctx: ctx, status: "completed"}
	turn := s.Turn
	if turn == nil {
		turn = func(t *Turn) { t.Say("ok: " + t.Prompt) }
	}
	turn(t)
	if ctx.Err() != nil {
		t.status = "stopped"
	}
	ss.mu.Lock()
	ss.running = false
	ss.mu.Unlock()
	ss.publish("run_settled", runID, map[string]any{"status": t.status, "error": t.err}, false)
}

func (s *Server) stop(w http.ResponseWriter, r *http.Request) {
	ss := s.session(w, r)
	if ss == nil {
		return
	}
	ss.mu.Lock()
	cancel := ss.cancelRun
	ss.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	writeJSON(w, 200, map[string]any{"stopped": true})
}

func (s *Server) policy(w http.ResponseWriter, r *http.Request) {
	ss := s.session(w, r)
	if ss == nil {
		return
	}
	var body struct {
		Key string `json:"key"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	s.mu.Lock()
	ch := s.approvals[body.Key]
	s.mu.Unlock()
	if ch == nil {
		writeJSON(w, 400, map[string]any{"detail": "no such request"})
		return
	}
	select {
	case ch <- r.PathValue("decision"):
	default:
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

// Turn is one run of the fake agent.
type Turn struct {
	Session *Session
	RunID   string
	Prompt  string
	Ctx     context.Context

	s      *Server
	calls  int
	status string
	err    string
}

// Fail ends the turn as failed.
func (t *Turn) Fail(message string) {
	t.status, t.err = "failed", message
	t.Session.publish("error", t.RunID, map[string]any{"message": message}, false)
}

// Say streams one assistant message and writes it into the transcript.
func (t *Turn) Say(text string) {
	ss := t.Session
	ss.publish("message_start", t.RunID, map[string]any{}, false)
	for _, piece := range splitWords(text) {
		ss.publish("content_block_delta", t.RunID, map[string]any{"delta": map[string]any{"type": "text_delta", "text": piece}}, true)
	}
	ss.addMessage(map[string]any{"role": "assistant", "text": text, "run_id": t.RunID})
	ss.publish("message_stop", t.RunID, map[string]any{"stop_reason": "end_turn"}, false)
}

func splitWords(s string) []string {
	var out []string
	start := 0
	for i := range s {
		if s[i] == ' ' {
			out = append(out, s[start:i+1])
			start = i + 1
		}
	}
	return append(out, s[start:])
}

// Exec runs a shell command on the device as the Exec tool would, and reports it in the stream.
func (t *Turn) Exec(command string) (map[string]any, *RPCError, string) {
	return t.Tool("Exec", "exec.run", "exec", map[string]any{"argv": []string{"bash", "-c", command}, "cwd": t.Session.Cwd}, map[string]any{"command": command})
}

// Tool makes one device call for a tool and streams its start, input and result. The outcome is
// "ok", "error" or "unknown" (the device did not know the call after a reconnect).
func (t *Turn) Tool(tool, method, intent string, params map[string]any, input map[string]any) (map[string]any, *RPCError, string) {
	t.calls++
	callID := t.RunID + "-c" + strconv.Itoa(t.calls)
	ss := t.Session
	ss.publish("message_start", t.RunID, map[string]any{}, false)
	ss.publish("tool_use_start", t.RunID, map[string]any{"tool_call_id": callID, "tool_name": tool}, false)
	ss.publish("tool_use_stop", t.RunID, map[string]any{"tool_call_id": callID, "final_input": input}, false)
	ss.publish("message_stop", t.RunID, map[string]any{"stop_reason": "tool_use"}, false)
	params["_meta"] = map[string]any{"session_id": ss.ID, "tool_call_id": callID, "tool": tool, "intent": intent, "title": fmt.Sprint(input["command"])}
	id := ss.ID + ":" + callID + ":1"
	result, rpcErr, outcome := t.s.Call(t.Ctx, id, method, params)
	content := ""
	isErr := false
	switch {
	case outcome == "unknown":
		content, isErr = "the terminal disconnected while running this and reported no outcome", true
	case rpcErr != nil:
		content, isErr = rpcErr.Message, true
	default:
		raw, _ := json.Marshal(result)
		content = string(raw)
	}
	ss.addMessage(map[string]any{"role": "assistant", "text": "", "run_id": t.RunID,
		"tool_calls": []map[string]any{{"id": callID, "name": tool, "arguments": input}}})
	ss.addMessage(map[string]any{"role": "tool", "run_id": t.RunID, "tool_results": []map[string]any{{"id": callID, "content": content, "is_error": isErr}}})
	ss.publish("tool_result", t.RunID, map[string]any{"tool_call_id": callID, "content": content, "is_error": isErr}, false)
	return result, rpcErr, outcome
}

// Approval holds the turn on a policy approval until the operator decides or the turn is stopped,
// and answers the decision ("grant", "grant-similar", "refuse") or "".
func (t *Turn) Approval(tool, text string) string {
	key := "k-" + t.RunID
	ch := make(chan string, 1)
	t.s.mu.Lock()
	t.s.approvals[key] = ch
	t.s.mu.Unlock()
	t.Session.mu.Lock()
	t.Session.approvals = append(t.Session.approvals, key)
	t.Session.mu.Unlock()
	t.s.PublishApp("permission.pending", t.Session.ID, map[string]any{"request_id": key, "request_ref": "policy:" + t.Session.ID + ":" + key,
		"kind": "policy", "title": tool, "tool": tool, "text": text, "risk": "routine"})
	select {
	case d := <-ch:
		t.s.PublishApp("permission.resolved", t.Session.ID, map[string]any{"request_id": key, "decision": d, "via": "cli"})
		return d
	case <-t.Ctx.Done():
		return ""
	}
}
