// Package app joins the parts of daedalus-cli into the backend the terminal interface draws: the
// server session (REST and its two streams), the device connection that runs the session's tools
// here, the local guard, and pairing. It implements tui.Backend; headless mode drives the same
// backend without a screen.
package app

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/auth"
	"github.com/ascorblack/daedalus/cli/internal/client"
	"github.com/ascorblack/daedalus/cli/internal/config"
	"github.com/ascorblack/daedalus/cli/internal/device"
	"github.com/ascorblack/daedalus/cli/internal/exec"
	"github.com/ascorblack/daedalus/cli/internal/guard"
	"github.com/ascorblack/daedalus/cli/internal/tui"
)

// Kind is the session kind of a conversation started here.
const Kind = "remote"

// Options configures a backend.
type Options struct {
	Client   *client.Client
	Executor *exec.Executor
	Guard    *guard.Guard
	// Link is the device connection; nil when the credential cannot open one (the static API token),
	// and then a session's tools cannot run here.
	Link *device.Link
	// Cwd is the folder new sessions are bound to.
	Cwd     string
	Version string
	// Profile, Config and Store are for Login: where a new token is kept.
	Profile string
	Config  *config.File
	Store   auth.Store
	// StoreBackend names where the token came from, for Status.
	StoreBackend string
	// DeviceName names this machine at pairing.
	DeviceName string
	// Interactive says a person is at the terminal: the backend answers the guard's questions
	// through PermissionRequested events. Headless mode leaves it false.
	Interactive bool
	// Tail is how many turns a snapshot reads.
	Tail int
}

// Backend implements tui.Backend.
type Backend struct {
	opt      Options
	events   chan tui.Event
	ctx      context.Context
	cancel   context.CancelFunc
	clientID string

	mu         sync.Mutex
	session    string
	info       tui.SessionInfo
	stopStream context.CancelFunc
	tools      map[string]string
	run        string
	msgIndex   int
	dropped    bool
	questions  map[string]client.Question
	local      map[string]chan guard.Decision
	localN     int
	streamUp   bool
	parked     bool
	device     device.Status
	closed     bool
}

var _ tui.Backend = (*Backend)(nil)

// New returns a backend. Close ends it and closes Events.
func New(opt Options) *Backend {
	if opt.Tail <= 0 {
		opt.Tail = 600
	}
	ctx, cancel := context.WithCancel(context.Background())
	b := &Backend{
		opt:       opt,
		events:    make(chan tui.Event, 1024),
		ctx:       ctx,
		cancel:    cancel,
		clientID:  "cli-" + randomHex(6),
		tools:     map[string]string{},
		questions: map[string]client.Question{},
		local:     map[string]chan guard.Decision{},
	}
	if opt.Interactive && opt.Guard != nil {
		opt.Guard.SetPrompter(b)
	}
	return b
}

func randomHex(n int) string {
	buf := make([]byte, n)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}

// blocksText joins the text of a tool result's content blocks; blocks of another type (an image)
// have no text to show here.
func blocksText(v any) string {
	blocks, _ := v.([]any)
	var parts []string
	for _, item := range blocks {
		block, _ := item.(map[string]any)
		if text, ok := block["text"].(string); ok && text != "" {
			parts = append(parts, text)
		}
	}
	return strings.Join(parts, "\n")
}

// ClientID is this process's id, the same in presence, the event stream and the device connection.
func (b *Backend) ClientID() string { return b.clientID }

// Events is the stream the interface reads.
func (b *Backend) Events() <-chan tui.Event { return b.events }

// emit hands an event on. The channel is wide; a screen that stopped reading for a thousand events
// has a bigger problem than the event it misses, and the backend must never block its streams on it.
func (b *Backend) emit(e tui.Event) {
	// Under the lock, so Close cannot close the channel between the check and the send; the send
	// never blocks, so holding the lock across it costs nothing.
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return
	}
	select {
	case b.events <- e:
	default:
	}
}

// Close stops the streams and closes Events.
func (b *Backend) Close() {
	b.mu.Lock()
	if b.closed {
		b.mu.Unlock()
		return
	}
	b.closed = true
	close(b.events)
	stop := b.stopStream
	b.mu.Unlock()
	if stop != nil {
		stop()
	}
	b.cancel()
}

// SessionID is the session on screen.
func (b *Backend) SessionID() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.session
}

// DeviceStatus is called by the device link with its state changes.
func (b *Backend) DeviceStatus(s device.Status) {
	b.mu.Lock()
	b.device = s
	b.mu.Unlock()
	switch s.State {
	case device.Connected:
		b.connChanged()
		b.reportGuard()
	case device.Reconnecting:
		b.emit(tui.ConnectionChanged{State: tui.Reconnecting, Attempt: s.Attempt, RetryIn: s.RetryIn, Err: errString(s.Err)})
	case device.Unauthorized:
		b.emit(tui.ConnectionChanged{State: tui.Unauthorized, Err: errString(s.Err)})
	case device.Replaced:
		b.emit(tui.Notice{Level: tui.NoticeError, Text: "Another daedalus-cli of this device took over its connection: the agent's tools now run there, not here."})
		b.emit(tui.ConnectionChanged{State: tui.Offline, Err: errString(s.Err)})
	}
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// connChanged reports Online once the stream and, when there is one, the device link are both up.
func (b *Backend) connChanged() {
	b.mu.Lock()
	up := b.streamUp && (b.opt.Link == nil || b.device.State == device.Connected)
	b.mu.Unlock()
	if up {
		b.emit(tui.ConnectionChanged{State: tui.Online})
	}
}

// Sessions lists sessions for the resume picker.
func (b *Backend) Sessions(ctx context.Context, limit int) ([]tui.SessionSummary, error) {
	if client.IsDeviceToken(b.opt.Client.Token()) {
		rows, err := b.opt.Client.ResumeSessions(ctx, b.opt.Cwd, "", limit)
		if err != nil {
			return nil, err
		}
		out := make([]tui.SessionSummary, 0, len(rows))
		for _, r := range rows {
			s := tui.SessionSummary{ID: r.ID, Title: r.Title, Kind: r.Kind, Updated: parseTime(r.LastMessageAt), Running: r.Status == "running"}
			if r.Remote != nil {
				s.Cwd = r.Remote.Cwd
			}
			out = append(out, s)
		}
		return out, nil
	}
	rows, err := b.opt.Client.Sessions(ctx, limit)
	if err != nil {
		return nil, err
	}
	out := make([]tui.SessionSummary, 0, len(rows))
	for _, r := range rows {
		out = append(out, tui.SessionSummary{ID: r.ID, Title: r.Title, Kind: r.Kind, Updated: parseTime(r.LastMessageAt), Running: r.Status == "running"})
	}
	return out, nil
}

func parseTime(s string) time.Time {
	t, _ := time.Parse(time.RFC3339Nano, s)
	return t
}

// Start opens a session: resumed, continued, or new in the working folder. The stream is opened
// before the transcript is read, so nothing falls between the two, and a first prompt is sent only
// once both are in place, so its turn is followed from its first token.
func (b *Backend) Start(ctx context.Context, req tui.StartRequest) (tui.Session, error) {
	c := b.opt.Client
	id := req.Resume
	if id == "" && req.Continue {
		rows, err := c.ResumeSessions(ctx, b.opt.Cwd, "", 50)
		if err != nil {
			return tui.Session{}, err
		}
		for _, r := range rows {
			if r.Here {
				id = r.ID
				break
			}
		}
		if id == "" {
			return tui.Session{}, errors.New("no conversation was started in this folder yet")
		}
	}
	if id != "" {
		if client.IsDeviceToken(c.Token()) {
			if _, err := c.Attach(ctx, id); err != nil {
				return tui.Session{}, err
			}
		}
	} else {
		if b.opt.Link == nil {
			return tui.Session{}, errors.New("a conversation started here runs its tools on this machine, which needs a paired device: run daedalus-cli login")
		}
		if err := b.opt.Executor.CheckRoot(b.opt.Cwd); err != nil {
			return tui.Session{}, err
		}
		ref, err := c.CreateSession(ctx, client.NewSession{Kind: Kind, Cwd: b.opt.Cwd, Autotitle: req.Prompt != "", Title: titleFor(req.Prompt, b.opt.Cwd), Guard: b.guardName()})
		if err != nil {
			return tui.Session{}, err
		}
		id = ref.ID
	}
	snap, err := b.open(ctx, id)
	if err != nil {
		return tui.Session{}, err
	}
	if req.Prompt != "" {
		if err := b.Send(ctx, tui.Prompt{Text: req.Prompt, ClientID: randomHex(8)}); err != nil {
			return snap, err
		}
		snap.Running = true
	}
	return snap, nil
}

// titleFor is the title a new session carries until the server names it from the first message;
// the server wants one when there is no message to name it from.
func titleFor(prompt, cwd string) string {
	if prompt != "" {
		return ""
	}
	return filepath.Base(cwd)
}

func (b *Backend) guardName() string {
	if b.opt.Guard == nil {
		return "auto"
	}
	return b.opt.Guard.Level().String()
}

// open switches the screen to a session: its stream, its global events, its snapshot.
func (b *Backend) open(ctx context.Context, id string) (tui.Session, error) {
	b.mu.Lock()
	if b.stopStream != nil {
		b.stopStream()
	}
	sctx, stop := context.WithCancel(b.ctx)
	b.stopStream = stop
	b.session = id
	b.tools = map[string]string{}
	b.run = ""
	b.streamUp = false
	b.mu.Unlock()

	stream := b.opt.Client.FollowSession(sctx, id, client.StreamOptions{})
	// Wait for the hello (or the stream's failure) before reading the transcript.
	var first client.StreamItem
	select {
	case item, ok := <-stream.Items():
		if !ok {
			return tui.Session{}, errors.New("the session stream closed before it opened")
		}
		first = item
	case <-ctx.Done():
		stop()
		return tui.Session{}, ctx.Err()
	}
	if first.Signal == client.SignalFatal {
		stop()
		return tui.Session{}, first.Err
	}
	// The global stream too is live only from its hello: an approval raised between the read below
	// and a later subscription would never reach the screen, so it is open before the read.
	events := b.opt.Client.FollowEvents(sctx, client.EventsOptions{Session: id, Client: b.clientID,
		Types: []string{"permission.", "ask.", "session.", "run.", "remote."}})
	select {
	case item, ok := <-events:
		if ok && item.Fatal != nil {
			stop()
			return tui.Session{}, item.Fatal
		}
	case <-time.After(10 * time.Second):
		// A server slow to open it still gets the session on screen; the snapshot covers what is open.
	case <-ctx.Done():
		stop()
		return tui.Session{}, ctx.Err()
	}
	detail, err := b.opt.Client.Session(ctx, id, b.opt.Tail)
	if err != nil {
		stop()
		return tui.Session{}, err
	}
	if detail.Remote != nil && b.opt.Link != nil {
		if w := b.opt.Link.Welcome(); w != nil && w.DeviceID == detail.Remote.DeviceID {
			if err := b.opt.Executor.Bind(id, detail.Remote.Cwd); err != nil {
				b.emit(tui.Notice{Level: tui.NoticeWarning, Text: "This terminal cannot run the session's tools: " + err.Error()})
			}
		} else if w == nil {
			_ = b.opt.Executor.Bind(id, detail.Remote.Cwd)
		}
	}
	snap := b.snapshot(ctx, detail)
	b.mu.Lock()
	b.info = snap.Info
	b.streamUp = first.Signal == client.SignalHello
	b.mu.Unlock()
	go b.follow(sctx, id, stream)
	go b.followEvents(id, events)
	go b.presence(sctx, id)
	if b.opt.Link != nil {
		b.opt.Link.NotifySessions(ctx)
	}
	b.connChanged()
	return snap, nil
}

// Sessions this process shows, with their guard, for the device connection's hello.
func (b *Backend) ShownSessions() []device.SessionGuard {
	id := b.SessionID()
	if id == "" {
		return nil
	}
	return []device.SessionGuard{{SessionID: id, Guard: b.guardName()}}
}

// snapshot turns a session read into what the screen draws.
func (b *Backend) snapshot(ctx context.Context, d *client.SessionDetail) tui.Session {
	info := tui.SessionInfo{ID: d.ID, Title: d.Title, Kind: d.Kind, Model: d.Model, Server: b.opt.Client.Base()}
	if d.Remote != nil {
		info.Cwd, info.Device = d.Remote.Cwd, d.Remote.DeviceName
	}
	snap := tui.Session{Info: info, Running: d.Running(), History: History(d.Messages), Usage: usageOf(d.Usage, d.Context)}
	if d.Pending != nil && len(d.Pending.Questions) > 0 {
		q := b.question(d.Pending.ID, d.Pending.Questions)
		snap.Pending = append(snap.Pending, tui.QuestionAsked{Question: q})
	}
	for _, key := range d.OpenApprovals {
		req := tui.PermissionRequest{ID: key, Source: tui.SourceServer, Kind: "policy"}
		req.Tool, req.Text = refusedCall(d.Messages, key)
		if similar, err := b.opt.Client.Similar(ctx, d.ID, key); err == nil {
			req.Similar = similar
		}
		snap.Pending = append(snap.Pending, tui.PermissionRequested{Request: req})
	}
	if queue, err := b.opt.Client.Queue(ctx, d.ID); err == nil {
		for _, q := range queue {
			snap.Queue = append(snap.Queue, tui.QueuedMessage{ID: q.ID, Text: q.Text})
		}
	}
	return snap
}

// refusedCall finds the call a refusal with this approval key answered, for the permission box.
func refusedCall(messages []client.Message, key string) (string, string) {
	names := map[string]client.Message{}
	for _, m := range messages {
		for _, call := range m.ToolCalls {
			names[call.ID] = m
		}
	}
	for i := len(messages) - 1; i >= 0; i-- {
		for _, r := range messages[i].ToolResults {
			if !strings.Contains(r.Content, key) {
				continue
			}
			if m, ok := names[r.ID]; ok {
				for _, call := range m.ToolCalls {
					if call.ID == r.ID {
						return call.Name, describeInput(call.Arguments)
					}
				}
			}
			return "", r.Content
		}
	}
	return "", key
}

func describeInput(args map[string]any) string {
	for _, k := range []string{"command", "cmd", "path", "file_path", "url", "query"} {
		if v, ok := args[k].(string); ok && v != "" {
			return v
		}
	}
	raw, _ := json.Marshal(args)
	return string(raw)
}

// History turns a transcript page into the events a live turn produces.
func History(messages []client.Message) []tui.Event {
	var out []tui.Event
	for i, m := range messages {
		if m.Internal {
			continue
		}
		at := parseTime(m.CreatedAt)
		id := strconv.Itoa(i)
		if m.Seq != nil {
			id = strconv.FormatInt(*m.Seq, 10)
		}
		switch m.Role {
		case "user":
			if m.Text != "" {
				origin := ""
				if m.Origin != "" && m.Origin != "cli" {
					origin = m.Origin
				}
				out = append(out, tui.UserMessage{ID: id, Text: m.Text, Origin: origin, At: at})
			}
		case "assistant":
			if m.Thinking != "" {
				out = append(out, tui.ThinkingDelta{MessageID: id, Text: m.Thinking})
			}
			if m.Text != "" {
				out = append(out, tui.TextDelta{MessageID: id, Text: m.Text})
			}
			if m.Thinking != "" || m.Text != "" {
				out = append(out, tui.MessageDone{MessageID: id})
			}
			for _, call := range m.ToolCalls {
				out = append(out, tui.ToolStarted{ID: call.ID, Name: call.Name, Input: call.Arguments, At: at})
			}
		}
		for _, r := range m.ToolResults {
			out = append(out, tui.ToolFinished{ID: r.ID, Output: r.Content, IsError: r.IsError, At: at})
		}
	}
	return out
}

func usageOf(usage, ctxInfo map[string]any) tui.Usage {
	num := func(m map[string]any, keys ...string) float64 {
		for _, k := range keys {
			if v, ok := m[k].(float64); ok {
				return v
			}
		}
		return 0
	}
	return tui.Usage{
		InputTokens:   int(num(usage, "i", "input_tokens")),
		OutputTokens:  int(num(usage, "o", "output_tokens")),
		CostUSD:       num(usage, "usd", "cost_usd"),
		ContextTokens: int(num(ctxInfo, "tokens", "used", "context_tokens")),
		ContextWindow: int(num(ctxInfo, "window", "context_window", "limit")),
	}
}

func (b *Backend) question(id string, items []client.Question) tui.Question {
	q := tui.Question{ID: id}
	for i, item := range items {
		qi := tui.QuestionItem{Header: item.Header, Text: item.Question, Multi: item.MultiSelect, Custom: true}
		for _, o := range item.Options {
			qi.Options = append(qi.Options, tui.QuestionOption{Label: o.Label, Description: o.Description})
		}
		q.Items = append(q.Items, qi)
		b.mu.Lock()
		b.questions[id+"#"+strconv.Itoa(i)] = item
		b.mu.Unlock()
	}
	return q
}

// follow reads the session stream for as long as the session is on screen.
func (b *Backend) follow(ctx context.Context, id string, stream *client.Stream) {
	defer stream.Close()
	for item := range stream.Items() {
		switch item.Signal {
		case client.SignalHello:
			b.mu.Lock()
			b.streamUp = true
			dropped := b.dropped
			b.dropped = false
			b.mu.Unlock()
			if dropped {
				// Deltas are live only, so whatever streamed while the link was down is read back
				// from the transcript instead.
				b.reread(ctx, id)
			}
			b.connChanged()
		case client.SignalResync:
			b.reread(ctx, id)
		case client.SignalDropped:
			b.mu.Lock()
			b.streamUp = false
			b.dropped = true
			b.mu.Unlock()
			b.emit(tui.ConnectionChanged{State: tui.Reconnecting, Attempt: item.Attempt, RetryIn: item.RetryIn, Err: item.Reason})
		case client.SignalFatal:
			if client.IsUnauthorized(item.Err) {
				b.emit(tui.ConnectionChanged{State: tui.Unauthorized, Err: item.Err.Error()})
			} else {
				b.emit(tui.Notice{Level: tui.NoticeError, Text: "The session is gone: " + item.Err.Error()})
				b.emit(tui.ConnectionChanged{State: tui.Offline, Err: item.Err.Error()})
			}
			return
		case client.SignalEvent:
			for _, e := range b.translate(ctx, id, item.Envelope) {
				b.emit(e)
			}
		}
	}
}

// reread replaces the screen's history with the transcript.
func (b *Backend) reread(ctx context.Context, id string) {
	d, err := b.opt.Client.Session(ctx, id, b.opt.Tail)
	if err != nil {
		b.emit(tui.Notice{Level: tui.NoticeWarning, Text: "Could not read the session again: " + err.Error()})
		return
	}
	snap := b.snapshot(ctx, d)
	b.emit(tui.HistoryReset{History: snap.History})
	for _, p := range snap.Pending {
		b.emit(p)
	}
	b.emit(tui.QueueChanged{Items: snap.Queue})
	b.emit(tui.UsageChanged{Usage: snap.Usage})
}

// translate folds one server event into the screen's events.
func (b *Backend) translate(ctx context.Context, id string, env client.Envelope) []tui.Event {
	var p map[string]any
	_ = json.Unmarshal(env.Payload, &p)
	if p == nil {
		p = map[string]any{}
	}
	str := func(k string) string {
		v, _ := p[k].(string)
		return v
	}
	now := time.Now()
	var out []tui.Event
	b.mu.Lock()
	if env.RunID != "" && env.RunID != b.run && (env.Kind == "message_start" || env.Kind == "run_started" || env.Kind == "tool_use_start") {
		b.run = env.RunID
		b.msgIndex = 0
		out = append(out, tui.TurnStarted{RunID: env.RunID, At: now})
	}
	if b.parked && (env.Kind == "message_start" || env.Kind == "tool_result") {
		// The parked call came back: the device reconnected and the turn goes on.
		b.parked = false
		out = append(out, tui.DeviceWaiting{Waiting: false, Since: now})
	}
	msgID := b.run + ":" + strconv.Itoa(b.msgIndex)
	b.mu.Unlock()

	switch env.Kind {
	case "message_start":
		b.mu.Lock()
		b.msgIndex++
		msgID = b.run + ":" + strconv.Itoa(b.msgIndex)
		b.mu.Unlock()
	case "content_block_delta":
		delta, _ := p["delta"].(map[string]any)
		text, _ := delta["text"].(string)
		switch delta["type"] {
		case "text_delta":
			out = append(out, tui.TextDelta{MessageID: msgID, Text: text})
		case "thinking_delta":
			out = append(out, tui.ThinkingDelta{MessageID: msgID, Text: text})
		}
	case "message_stop":
		out = append(out, tui.MessageDone{MessageID: msgID})
	case "tool_use_start":
		b.mu.Lock()
		b.tools[str("tool_call_id")] = str("tool_name")
		b.mu.Unlock()
	case "tool_use_stop":
		callID := str("tool_call_id")
		b.mu.Lock()
		name := b.tools[callID]
		b.mu.Unlock()
		input, _ := p["final_input"].(map[string]any)
		out = append(out, tui.ToolStarted{ID: callID, Name: name, Input: input, Parent: str("subagent_id"), At: now})
	case "tool_result":
		// The host's stream carries the result as the core's content blocks; only the transcript
		// view flattens them into one content string. Reading content alone showed every live
		// result as empty.
		content := blocksText(p["content_blocks"])
		if content == "" {
			content = str("content")
		}
		isErr, _ := p["is_error"].(bool)
		out = append(out, tui.ToolFinished{ID: str("tool_call_id"), Output: content, IsError: isErr, At: now})
	case "remote_output":
		if delta := str("delta"); delta != "" {
			out = append(out, tui.ToolProgress{ID: str("tool_call_id"), Text: delta})
		}
	case "tool_call_pending":
		if str("kind") == "external_call" {
			// The call waits for the session's home device. When that is this terminal, the
			// reconnect resumes it on its own; the screen says what the turn is waiting for.
			ext, _ := p["external_call_payload"].(map[string]any)
			name, _ := ext["device_name"].(string)
			b.mu.Lock()
			b.parked = true
			b.mu.Unlock()
			out = append(out, tui.DeviceWaiting{Device: name, Waiting: true, Since: now})
			break
		}
		ask, _ := p["ask_user_payload"].(map[string]any)
		raw, _ := json.Marshal(ask["questions"])
		var items []client.Question
		_ = json.Unmarshal(raw, &items)
		if len(items) > 0 {
			out = append(out, tui.QuestionAsked{Question: b.question(str("tool_call_id"), items)})
		}
	case "run_settled":
		b.mu.Lock()
		parked := b.parked && str("status") == "awaiting"
		if !parked {
			b.parked = false
		}
		b.mu.Unlock()
		if parked {
			// Parked on the device, not finished: the server resumes the turn when the device is
			// back, and the turn's end comes then.
			break
		}
		outcome := tui.TurnCompleted
		errText := ""
		switch str("status") {
		case "failed", "error":
			outcome = tui.TurnFailed
			errText = str("error")
		case "stopped", "interrupted", "cancelled", "canceled":
			outcome = tui.TurnInterrupted
		}
		out = append(out, tui.TurnEnded{RunID: env.RunID, Outcome: outcome, Err: errText, At: now})
	case "error":
		out = append(out, tui.Notice{Level: tui.NoticeError, Text: str("message")})
	case "model_changed":
		model := str("to")
		if model == "" {
			model = str("model_name")
		}
		out = append(out, tui.ModelChanged{Model: model})
	case "compaction_started":
		out = append(out, tui.CompactionChanged{Running: true})
	case "compaction_completed":
		out = append(out, tui.CompactionChanged{Running: false, Summary: str("summary")})
	case "subagent_spawn", "subagent_progress", "subagent_complete":
		state := strings.TrimPrefix(env.Kind, "subagent_")
		out = append(out, tui.SubagentChanged{ID: str("subagent_id"), Name: str("name"), State: state, Detail: str("detail")})
	case "steer_changed", "queue_update":
		if queue, err := b.opt.Client.Queue(ctx, id); err == nil {
			items := make([]tui.QueuedMessage, 0, len(queue))
			for _, q := range queue {
				items = append(items, tui.QueuedMessage{ID: q.ID, Text: q.Text})
			}
			out = append(out, tui.QueueChanged{Items: items})
		}
	case "history_cut":
		go b.reread(ctx, id)
	case "usage_committed":
		if d, err := b.opt.Client.Session(ctx, id, 1); err == nil {
			out = append(out, tui.UsageChanged{Usage: usageOf(d.Usage, d.Context)})
		}
	}
	return out
}

// followEvents reads the global stream for what the session stream does not carry: approvals and
// questions, including their answers from the app or Telegram.
func (b *Backend) followEvents(id string, items <-chan client.EventItem) {
	ctx := b.ctx
	for item := range items {
		if item.Fatal != nil {
			return
		}
		if item.Resync || item.Hello {
			continue
		}
		ev := item.Event
		if ev.SessionID != "" && ev.SessionID != id {
			continue
		}
		var p map[string]any
		_ = json.Unmarshal(ev.Payload, &p)
		str := func(k string) string {
			v, _ := p[k].(string)
			return v
		}
		switch ev.Type {
		case "permission.pending":
			req := tui.PermissionRequest{ID: str("request_id"), Source: tui.SourceServer, Kind: str("kind"), Tool: str("tool"),
				Text: str("text"), Elevated: str("risk") == "elevated"}
			if req.Text == "" {
				req.Text = str("title")
			}
			if strings.HasPrefix(str("request_ref"), "policy:") {
				if similar, err := b.opt.Client.Similar(ctx, id, req.ID); err == nil {
					req.Similar = similar
				}
			}
			b.emit(tui.PermissionRequested{Request: req})
		case "permission.resolved":
			b.emit(tui.PermissionResolved{ID: str("request_id"), Decision: str("decision"), Via: str("via")})
		case "ask.answered":
			ref := str("request_ref")
			qid := ref[strings.LastIndex(ref, ":")+1:]
			if qid == "" {
				qid = str("request_id")
			}
			b.emit(tui.QuestionResolved{ID: qid, Via: str("via")})
		case "remote.waiting":
			waiting, _ := p["waiting"].(bool)
			b.mu.Lock()
			name := b.info.Device
			b.mu.Unlock()
			b.emit(tui.DeviceWaiting{Device: name, Waiting: waiting, Since: parseTime(str("since"))})
		case "session.updated", "session.renamed":
			if title := str("title"); title != "" {
				b.mu.Lock()
				b.info.Title = title
				info := b.info
				b.mu.Unlock()
				b.emit(tui.SessionUpdated{Info: info})
			}
		}
	}
}

// presence reports what this terminal shows every 20 seconds, so a session on screen counts as
// attended and keeps its Remote mark.
func (b *Backend) presence(ctx context.Context, id string) {
	tz := os.Getenv("TZ")
	lang := "en"
	if l := os.Getenv("LANG"); strings.HasPrefix(l, "ru") {
		lang = "ru"
	}
	report := func() {
		_ = b.opt.Client.ReportPresence(ctx, client.Presence{Client: b.clientID, Visible: true, Focused: true, Sessions: []string{id}, Lang: lang, TZ: tz})
	}
	report()
	ticker := time.NewTicker(20 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			report()
		}
	}
}

// Send delivers a prompt, uploading its images first.
func (b *Backend) Send(ctx context.Context, p tui.Prompt) error {
	id := b.SessionID()
	if id == "" {
		return errors.New("no session is open")
	}
	for _, img := range p.Images {
		if err := b.opt.Client.Upload(ctx, id, img.Name, img.MIME, img.Data); err != nil {
			return fmt.Errorf("upload %s: %w", img.Name, err)
		}
	}
	if p.ClientID == "" {
		p.ClientID = randomHex(8)
	}
	_, err := b.opt.Client.Send(ctx, id, client.SendBody{Text: p.Text, FollowUp: p.Mode == tui.SendQueue, ClientMessageID: p.ClientID})
	return err
}

// Interrupt stops the running turn.
func (b *Backend) Interrupt(ctx context.Context) error { return b.opt.Client.Stop(ctx, b.SessionID()) }

// Withdraw takes a queued message back.
func (b *Backend) Withdraw(ctx context.Context, queuedID string) (string, error) {
	return b.opt.Client.Withdraw(ctx, b.SessionID(), queuedID)
}

// Confirm is the guard's question to the person, asked through the screen. It blocks the call that
// asked (and only that call) until Decide answers it.
func (b *Backend) Confirm(ctx context.Context, a guard.Action) (guard.Decision, error) {
	b.mu.Lock()
	b.localN++
	id := "local-" + strconv.Itoa(b.localN)
	answer := make(chan guard.Decision, 1)
	b.local[id] = answer
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		delete(b.local, id)
		b.mu.Unlock()
	}()
	tool := "Exec"
	switch a.Kind {
	case guard.Write:
		tool = "Write"
	case guard.Read:
		tool = "Read"
	}
	b.emit(tui.PermissionRequested{Request: tui.PermissionRequest{ID: id, Source: tui.SourceLocal, Kind: a.Kind.String(),
		Tool: tool, Text: a.Summary(), Similar: guard.Similar(a)}})
	select {
	case d := <-answer:
		decision := "deny"
		if d.Allow {
			decision = "allow"
		}
		b.emit(tui.PermissionResolved{ID: id, Decision: decision, Via: "cli"})
		return d, nil
	case <-ctx.Done():
		b.emit(tui.PermissionResolved{ID: id, Decision: "expired", Via: "cli"})
		return guard.Decision{}, ctx.Err()
	}
}

// Decide answers a permission request: the local guard's here, the server's policy there.
func (b *Backend) Decide(ctx context.Context, requestID string, d tui.Decision) error {
	b.mu.Lock()
	answer, local := b.local[requestID]
	b.mu.Unlock()
	if local {
		answer <- guard.Decision{Allow: d.Kind != tui.Deny, Remember: d.Kind == tui.AllowSimilar, Reason: d.Feedback}
		return nil
	}
	id := b.SessionID()
	c := b.opt.Client
	switch d.Kind {
	case tui.Allow:
		return c.Grant(ctx, id, requestID)
	case tui.AllowSimilar:
		return c.GrantSimilar(ctx, id, requestID)
	}
	if err := c.Refuse(ctx, id, requestID); err != nil {
		return err
	}
	if strings.TrimSpace(d.Feedback) != "" {
		_, err := c.Send(ctx, id, client.SendBody{Text: d.Feedback, ClientMessageID: randomHex(8)})
		return err
	}
	return nil
}

// Answer answers an AskUser question.
func (b *Backend) Answer(ctx context.Context, requestID string, answers []tui.Answer) error {
	items := make([]client.AnswerItem, 0, len(answers))
	for i, a := range answers {
		b.mu.Lock()
		q := b.questions[requestID+"#"+strconv.Itoa(i)]
		b.mu.Unlock()
		item := client.AnswerItem{Question: q.Question, Selected: a.Selected}
		if item.Selected == nil {
			item.Selected = []string{}
		}
		if a.Custom != "" {
			custom := a.Custom
			item.Custom = &custom
		}
		items = append(items, item)
	}
	return b.opt.Client.Answer(ctx, b.SessionID(), items)
}

// Commands lists the server's commands and skills.
func (b *Backend) Commands(ctx context.Context) ([]tui.Command, error) {
	cmds, err := b.opt.Client.Commands(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]tui.Command, 0, len(cmds))
	for _, c := range cmds {
		out = append(out, tui.Command{Name: strings.TrimPrefix(c.Name, "/"), Description: c.Description, Source: tui.CommandServer, Args: c.Args})
	}
	if skills, err := b.opt.Client.Skills(ctx); err == nil {
		for _, s := range skills {
			out = append(out, tui.Command{Name: s.Name, Description: s.Description, Source: tui.CommandSkill})
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// RunCommand runs a server command and shows its text.
func (b *Backend) RunCommand(ctx context.Context, line string) error {
	text, err := b.opt.Client.RunCommand(ctx, b.SessionID(), line)
	if err != nil {
		return err
	}
	if text != "" {
		b.emit(tui.Notice{Level: tui.NoticeInfo, Text: text})
	}
	return nil
}

// Models lists the server's presets.
func (b *Backend) Models(ctx context.Context) ([]tui.ModelOption, error) {
	m, err := b.opt.Client.Models(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]tui.ModelOption, 0, len(m.Presets))
	for _, p := range m.Presets {
		out = append(out, tui.ModelOption{ID: p.ID, Label: p.Label, Efforts: m.Efforts, Default: p.ID == m.Default, ContextWindow: p.ContextWindow})
	}
	return out, nil
}

// SetModel switches the preset and the effort.
func (b *Backend) SetModel(ctx context.Context, model, effort string) error {
	body := map[string]any{}
	if model != "" {
		body["preset"] = model
	}
	if effort != "" {
		body["reasoning_effort"] = effort
	}
	return b.opt.Client.SetModel(ctx, b.SessionID(), body)
}

// Compact summarises the history.
func (b *Backend) Compact(ctx context.Context, focus string) error {
	return b.opt.Client.Compact(ctx, b.SessionID(), focus)
}

// Clear starts a fresh session in the same folder.
func (b *Backend) Clear(ctx context.Context) (tui.Session, error) {
	return b.Start(ctx, tui.StartRequest{})
}

// Checkpoints are the operator's prompts in the transcript, each a point to rewind the conversation
// to. Code snapshots of this folder are not kept yet, so none has Code set.
func (b *Backend) Checkpoints(ctx context.Context) ([]tui.Checkpoint, error) {
	d, err := b.opt.Client.Session(ctx, b.SessionID(), b.opt.Tail)
	if err != nil {
		return nil, err
	}
	var out []tui.Checkpoint
	for _, m := range d.Messages {
		if m.Role == "user" && m.Seq != nil && !m.Internal && m.Text != "" {
			out = append(out, tui.Checkpoint{ID: strconv.FormatInt(*m.Seq, 10), Prompt: m.Text, At: parseTime(m.CreatedAt)})
		}
	}
	return out, nil
}

// ErrNoCodeSnapshots is a rewind of code asked of a client that keeps no snapshots of the folder.
var ErrNoCodeSnapshots = errors.New("this client keeps no snapshots of the folder yet, so only the conversation can be rewound")

// Rewind cuts the conversation back to a checkpoint.
func (b *Backend) Rewind(ctx context.Context, checkpointID string, scope tui.RewindScope) error {
	if scope == tui.RewindCode {
		return ErrNoCodeSnapshots
	}
	if scope == tui.RewindSummarize {
		return errors.New("summarising from a point is not offered by the server yet")
	}
	seq, err := strconv.ParseInt(checkpointID, 10, 64)
	if err != nil {
		return fmt.Errorf("checkpoint %q: %w", checkpointID, err)
	}
	return b.opt.Client.Revert(ctx, b.SessionID(), seq)
}

// Shell runs a `!` command in the working folder.
func (b *Backend) Shell(ctx context.Context, command string) (tui.ShellResult, error) {
	res, err := b.opt.Executor.Shell(ctx, b.opt.Cwd, command, 10*time.Minute)
	if err != nil {
		return tui.ShellResult{}, err
	}
	out := res.Stdout
	if strings.TrimSpace(res.Stderr) != "" {
		if out != "" && !strings.HasSuffix(out, "\n") {
			out += "\n"
		}
		out += res.Stderr
	}
	return tui.ShellResult{Output: out, ExitCode: res.ExitCode}, nil
}

// TUIGuard is a guard level as the interface names it.
func TUIGuard(l guard.Level) tui.Guard {
	switch l {
	case guard.Ask:
		return tui.GuardAsk
	case guard.Plan:
		return tui.GuardPlan
	}
	return tui.GuardAuto
}

// AttachLink gives the backend its device connection, made after the backend because the link asks
// the backend which sessions it shows.
func (b *Backend) AttachLink(l *device.Link) {
	b.mu.Lock()
	b.opt.Link = l
	b.mu.Unlock()
}

// SetGuard changes the local guard and tells the server, through the device connection when it is
// up and the REST route otherwise.
func (b *Backend) SetGuard(ctx context.Context, g tui.Guard) error {
	level := guard.Auto
	switch g {
	case tui.GuardAsk:
		level = guard.Ask
	case tui.GuardPlan:
		level = guard.Plan
	}
	if b.opt.Guard != nil {
		b.opt.Guard.Set(level)
	}
	b.reportGuard()
	return nil
}

func (b *Backend) reportGuard() {
	id := b.SessionID()
	if id == "" || b.opt.Guard == nil {
		return
	}
	level := b.opt.Guard.Level()
	ctx, cancel := context.WithTimeout(b.ctx, 10*time.Second)
	defer cancel()
	if b.opt.Link != nil && b.opt.Link.NotifyGuard(ctx, id, level) {
		return
	}
	if client.IsDeviceToken(b.opt.Client.Token()) {
		_ = b.opt.Client.SetGuard(ctx, id, level.String())
	}
}

// Status is what /status lists.
func (b *Backend) Status(ctx context.Context) (tui.Status, error) {
	b.mu.Lock()
	info := b.info
	dev := b.device
	b.mu.Unlock()
	items := []tui.StatusItem{
		{Label: "Server", Value: b.opt.Client.Base()},
		{Label: "Client", Value: "daedalus-cli " + b.opt.Version + " (" + runtime.GOOS + "/" + runtime.GOARCH + ")"},
		{Label: "Folder", Value: b.opt.Cwd},
	}
	if b.opt.Link != nil {
		state := dev.State.String()
		if w := b.opt.Link.Welcome(); w != nil {
			items = append(items, tui.StatusItem{Label: "Device", Value: w.DeviceID})
			items = append(items, tui.StatusItem{Label: "Server version", Value: w.ServerVersion})
		}
		items = append(items, tui.StatusItem{Label: "Device connection", Value: state})
	} else {
		items = append(items, tui.StatusItem{Label: "Device connection", Value: "none (not paired; tools cannot run here)"})
	}
	if b.opt.StoreBackend != "" {
		items = append(items, tui.StatusItem{Label: "Credential", Value: b.opt.StoreBackend})
	}
	if b.opt.Guard != nil {
		items = append(items, tui.StatusItem{Label: "Guard", Value: b.opt.Guard.Level().String()})
	}
	if info.ID != "" {
		items = append(items,
			tui.StatusItem{Label: "Session", Value: info.ID + "  " + info.Title},
			tui.StatusItem{Label: "Model", Value: info.Model})
	}
	return tui.Status{Items: items}, nil
}

// Login pairs this device. The challenge is answered at once; the outcome comes as LoginChanged, and
// the new token is stored before that event is sent.
func (b *Backend) Login(ctx context.Context) (tui.LoginChallenge, error) {
	ch, err := auth.Start(ctx, b.opt.Client, auth.ThisDevice(b.opt.DeviceName, b.opt.Version))
	if err != nil {
		return tui.LoginChallenge{}, err
	}
	go func() {
		approved, err := auth.Wait(b.ctx, b.opt.Client, ch)
		if err != nil {
			state := tui.LoginRefused
			if errors.Is(err, auth.ErrExpired) {
				state = tui.LoginExpired
			}
			b.emit(tui.LoginChanged{State: state, Err: err.Error()})
			return
		}
		if err := SaveLogin(b.opt.Store, b.opt.Config, b.opt.Profile, b.opt.Client.Base(), approved); err != nil {
			b.emit(tui.LoginChanged{State: tui.LoginRefused, Err: "approved, but the token could not be stored: " + err.Error()})
			return
		}
		b.emit(tui.LoginChanged{State: tui.LoginApproved, Device: approved.Device.Name})
	}()
	return tui.LoginChallenge{Code: ch.UserCode, URL: ch.VerificationURIComplete, ExpiresAt: ch.Expires(time.Now())}, nil
}

// SaveLogin keeps a pairing's token in the store and its device in the profile.
func SaveLogin(store auth.Store, cfg *config.File, profile, server string, approved auth.Approved) error {
	if store == nil {
		return errors.New("no credential store")
	}
	if err := store.Set(profile, approved.Token); err != nil {
		return err
	}
	if cfg == nil {
		return nil
	}
	p := cfg.Profiles[profile]
	p.Server = server
	p.DeviceID = approved.Device.ID
	p.DeviceName = approved.Device.Name
	cfg.Put(profile, p)
	return cfg.Save()
}
