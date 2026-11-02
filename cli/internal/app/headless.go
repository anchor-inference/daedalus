package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/tui"
)

// Exit codes of headless mode (`daedalus-cli -p`). They are the contract with scripts; cli/README.md
// lists them.
const (
	ExitOK = 0
	// ExitFailed is a turn that ended in an error, or any failure not listed below.
	ExitFailed = 1
	// ExitUsage is a command line that does not parse.
	ExitUsage = 2
	// ExitAuth is a device that is not paired, or whose token the server refused.
	ExitAuth = 3
	// ExitUnreachable is a server that could not be reached or spoke another contract.
	ExitUnreachable = 4
	// ExitWaiting is a turn that stopped on the operator: an approval or a question headless mode
	// cannot give. The session stays open, waiting, in the app.
	ExitWaiting = 5
	// ExitTimeout is --timeout running out, as timeout(1) reports it.
	ExitTimeout = 124
	// ExitInterrupted is Ctrl+C or a stop from elsewhere: the turn was stopped.
	ExitInterrupted = 130
)

// Format is how a headless run accounts for itself.
type Format string

const (
	// FormatText prints the answer on stdout; with Verbose, the tool calls on stderr.
	FormatText Format = "text"
	// FormatJSON prints one object when the turn ends.
	FormatJSON Format = "json"
	// FormatStreamJSON prints one object per line as things happen, the last of type "result".
	FormatStreamJSON Format = "stream-json"
)

// ParseFormat reads --output-format.
func ParseFormat(s string) (Format, error) {
	switch Format(s) {
	case FormatText, FormatJSON, FormatStreamJSON:
		return Format(s), nil
	}
	return "", fmt.Errorf("--output-format %q: want text, json or stream-json", s)
}

// Headless is one turn without a screen.
type Headless struct {
	Backend *Backend
	Format  Format
	Stdout  io.Writer
	Stderr  io.Writer
	Verbose bool
	// Resume continues a session instead of starting one; Continue continues this folder's latest.
	Resume   string
	Continue bool
}

// Record is one object of the json and stream-json formats. Type says which fields mean something:
// init, text, thinking, tool_use, tool_result, permission, question, notice, connection, result.
type Record struct {
	Type      string         `json:"type"`
	SessionID string         `json:"session_id,omitempty"`
	RunID     string         `json:"run_id,omitempty"`
	Text      string         `json:"text,omitempty"`
	ID        string         `json:"id,omitempty"`
	Name      string         `json:"name,omitempty"`
	Input     map[string]any `json:"input,omitempty"`
	Output    *string        `json:"output,omitempty"`
	IsError   *bool          `json:"is_error,omitempty"`
	Cwd       string         `json:"cwd,omitempty"`
	Model     string         `json:"model,omitempty"`
	Guard     string         `json:"guard,omitempty"`
	State     string         `json:"state,omitempty"`
	// Result fields.
	Subtype    string `json:"subtype,omitempty"`
	Result     string `json:"result,omitempty"`
	ExitCode   *int   `json:"exit_code,omitempty"`
	DurationMs int64  `json:"duration_ms,omitempty"`
	ToolCalls  int    `json:"tool_calls,omitempty"`
	Error      string `json:"error,omitempty"`
}

// Run runs one turn of prompt and returns the exit code.
func (h *Headless) Run(ctx context.Context, prompt string) int {
	start := time.Now()
	enc := json.NewEncoder(h.Stdout)
	enc.SetEscapeHTML(false)
	stream := func(r Record) {
		if h.Format == FormatStreamJSON {
			_ = enc.Encode(r)
		}
	}
	progress := func(format string, args ...any) {
		if h.Format == FormatText && h.Verbose {
			fmt.Fprintf(h.Stderr, format+"\n", args...)
		}
	}

	snap, err := h.Backend.Start(ctx, tui.StartRequest{Resume: h.Resume, Continue: h.Continue})
	if err != nil {
		return h.finish(enc, start, "", "", 0, ExitCodeFor(err), "", err)
	}
	sid := snap.Info.ID
	stream(Record{Type: "init", SessionID: sid, Cwd: snap.Info.Cwd, Model: snap.Info.Model, Guard: h.Backend.guardName()})
	progress("session %s", sid)
	if err := h.Backend.Send(ctx, tui.Prompt{Text: prompt}); err != nil {
		return h.finish(enc, start, sid, "", 0, ExitCodeFor(err), "", err)
	}

	var run string
	tools := 0
	started := false
	for {
		var ev tui.Event
		var ok bool
		select {
		case ev, ok = <-h.Backend.Events():
			if !ok {
				return h.finish(enc, start, sid, run, tools, ExitFailed, "", errors.New("the backend closed"))
			}
		case <-ctx.Done():
			code := ExitInterrupted
			if errors.Is(ctx.Err(), context.DeadlineExceeded) {
				code = ExitTimeout
			}
			// The turn is stopped, not left running unseen: a script that gave up on it does not
			// expect it to go on changing files.
			stopCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			_ = h.Backend.Interrupt(stopCtx)
			cancel()
			return h.finish(enc, start, sid, run, tools, code, "", ctx.Err())
		}
		switch e := ev.(type) {
		case tui.TurnStarted:
			started = true
			run = e.RunID
		case tui.TextDelta:
			if started {
				stream(Record{Type: "text", SessionID: sid, RunID: run, Text: e.Text})
			}
		case tui.ThinkingDelta:
			if started && h.Verbose {
				stream(Record{Type: "thinking", SessionID: sid, RunID: run, Text: e.Text})
			}
		case tui.ToolStarted:
			if !started {
				continue
			}
			tools++
			stream(Record{Type: "tool_use", SessionID: sid, RunID: run, ID: e.ID, Name: e.Name, Input: e.Input})
			progress("● %s(%s)", e.Name, describeInput(e.Input))
		case tui.ToolFinished:
			if !started {
				continue
			}
			out := e.Output
			isErr := e.IsError
			stream(Record{Type: "tool_result", SessionID: sid, RunID: run, ID: e.ID, Output: &out, IsError: &isErr})
			progress("  ⎿ %s", firstLine(e.Output))
		case tui.PermissionRequested:
			stream(Record{Type: "permission", SessionID: sid, RunID: run, ID: e.Request.ID, Name: e.Request.Tool, Text: e.Request.Text})
			if e.Request.Source == tui.SourceServer {
				msg := "the turn waits for an approval: " + strings.TrimSpace(e.Request.Tool+" "+e.Request.Text) + " (answer it in the app, or run without -p)"
				return h.finish(enc, start, sid, run, tools, ExitWaiting, "", errors.New(msg))
			}
		case tui.QuestionAsked:
			text := ""
			if len(e.Question.Items) > 0 {
				text = e.Question.Items[0].Text
			}
			stream(Record{Type: "question", SessionID: sid, RunID: run, ID: e.Question.ID, Text: text})
			return h.finish(enc, start, sid, run, tools, ExitWaiting, "", errors.New("the agent asks: "+text+" (answer it in the app, or run without -p)"))
		case tui.DeviceWaiting:
			state := "resumed"
			if e.Waiting {
				state = "waiting"
				progress("the turn waits for the device %s to reconnect", e.Device)
			}
			stream(Record{Type: "device", SessionID: sid, RunID: run, State: state, Name: e.Device})
		case tui.ToolProgress:
			if h.Verbose {
				stream(Record{Type: "tool_output", SessionID: sid, RunID: run, ID: e.ID, Text: e.Text})
			}
		case tui.Notice:
			stream(Record{Type: "notice", SessionID: sid, RunID: run, Text: e.Text})
			if e.Level == tui.NoticeError {
				progress("%s", e.Text)
			}
		case tui.ConnectionChanged:
			stream(Record{Type: "connection", SessionID: sid, State: connName(e.State), Error: e.Err})
			if e.State == tui.Unauthorized {
				return h.finish(enc, start, sid, run, tools, ExitAuth, "", errors.New("the server refused this device's credential: run daedalus-cli login"))
			}
			if e.State == tui.Reconnecting {
				progress("connection lost, reconnecting (%s)", e.Err)
			}
		case tui.TurnEnded:
			if !started || (run != "" && e.RunID != "" && e.RunID != run) {
				continue
			}
			answer := h.answer(ctx, sid, run)
			switch e.Outcome {
			case tui.TurnCompleted:
				return h.finish(enc, start, sid, run, tools, ExitOK, answer, nil)
			case tui.TurnInterrupted:
				return h.finish(enc, start, sid, run, tools, ExitInterrupted, answer, errors.New("the turn was stopped"))
			default:
				msg := e.Err
				if msg == "" {
					msg = "the turn failed"
				}
				return h.finish(enc, start, sid, run, tools, ExitFailed, answer, errors.New(msg))
			}
		}
	}
}

// answer is the turn's final answer as the transcript has it: the streamed deltas are live only, and
// a drop part way would leave a streamed copy short.
func (h *Headless) answer(ctx context.Context, sid, run string) string {
	d, err := h.Backend.opt.Client.Session(ctx, sid, 40)
	if err != nil {
		return ""
	}
	for i := len(d.Messages) - 1; i >= 0; i-- {
		m := d.Messages[i]
		if m.Role == "assistant" && strings.TrimSpace(m.Text) != "" && (run == "" || m.RunID == "" || m.RunID == run) {
			return m.Text
		}
		if m.Role == "user" && !m.Internal {
			break
		}
	}
	return ""
}

func (h *Headless) finish(enc *json.Encoder, start time.Time, sid, run string, tools, code int, answer string, err error) int {
	subtype := map[int]string{ExitOK: "success", ExitFailed: "error", ExitAuth: "unauthorized", ExitUnreachable: "unreachable",
		ExitWaiting: "waiting_for_operator", ExitTimeout: "timeout", ExitInterrupted: "interrupted", ExitUsage: "usage"}[code]
	switch h.Format {
	case FormatText:
		if answer != "" {
			fmt.Fprintln(h.Stdout, strings.TrimRight(answer, "\n"))
		}
		if err != nil {
			fmt.Fprintln(h.Stderr, "daedalus-cli: "+err.Error())
		}
	default:
		c, isErr := code, code != ExitOK
		r := Record{Type: "result", Subtype: subtype, SessionID: sid, RunID: run, Result: answer, IsError: &isErr,
			ExitCode: &c, DurationMs: time.Since(start).Milliseconds(), ToolCalls: tools}
		if err != nil {
			r.Error = err.Error()
		}
		_ = enc.Encode(r)
	}
	return code
}

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i] + " …"
	}
	if len(s) > 160 {
		return s[:160] + "…"
	}
	return s
}

func connName(s tui.ConnState) string {
	switch s {
	case tui.Connecting:
		return "connecting"
	case tui.Online:
		return "online"
	case tui.Reconnecting:
		return "reconnecting"
	case tui.Offline:
		return "offline"
	case tui.Unauthorized:
		return "unauthorized"
	}
	return "unknown"
}
