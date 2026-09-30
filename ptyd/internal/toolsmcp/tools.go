package toolsmcp

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

// The tools' names are the ones a Daedalus staff member has in-process, so a brief, a skill or the
// orchestrator's advice reads the same whichever CLI the member runs.
const (
	reportTool = "Report"
	askTool    = "AskOrchestrator"
)

// reportKinds are the states a report may name, as the host's staff service accepts them.
var reportKinds = []string{"checkpoint", "needs_input", "stuck", "done"}

// Fallback results for a post the host did not answer in time. A report was published the moment
// it was posted, so it was heard. A question stays open with the host after the call gives up: its
// answer is delivered as the worker's next message, so the worker is told to expect it rather than
// to ask again.
const (
	reportRecorded = "recorded"
	askExpired     = "Pending: nobody has answered yet. The answer will arrive as a message; carry on with what the brief allows meanwhile, or end your turn and wait for it. Do not ask the same question again."
)

// tool is one tool as tools/list describes it.
type tool struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

func stringList() map[string]any {
	return map[string]any{"type": "array", "items": map[string]any{"type": "string"}}
}

func toolList(askHold time.Duration) []tool {
	wait := "until the answer arrives"
	if askHold > 0 {
		wait = fmt.Sprintf("up to %s for the answer", roughly(askHold))
	}
	return []tool{
		{
			Name: reportTool,
			Description: "Tell your team how your task stands. kind: 'checkpoint' (progress worth knowing), 'needs_input' " +
				"(you cannot go on without a decision), 'stuck' (something outside your task blocks you) or 'done' (the " +
				"deliverable meets the task's done-when; it hands the task in: work on your own branch goes to review for " +
				"the operator to merge, and a worktree with uncommitted changes is refused — commit first; any other task " +
				"goes to done, handed in for the orchestrator to check against your note). note: a short factual summary. " +
				"evidence (with done): [{item, how, result}] for each check (C1 …) and requirement (R1 …) of the task — " +
				"what you ran or looked at and what it showed. acknowledged: the requirements (R…) sent to you that you " +
				"have taken into your plan, or the input files (R…) you have read. artifacts: paths or links of what you " +
				"produced. remember: one line to keep in your notes for every later session.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"kind":      map[string]any{"type": "string", "enum": reportKinds},
					"note":      map[string]any{"type": "string", "description": "A short factual summary."},
					"artifacts": stringList(),
					"remember":  map[string]any{"type": "string", "description": "One line for your notes."},
					"evidence": map[string]any{
						"type": "array",
						"items": map[string]any{
							"type": "object",
							"properties": map[string]any{
								"item":   map[string]any{"type": "string", "description": "The check or requirement: C1, R2, or its words."},
								"how":    map[string]any{"type": "string", "description": "What you ran, opened or looked at to check it."},
								"result": map[string]any{"type": "string", "description": "What that showed."},
							},
							"required": []string{"item", "how", "result"},
						},
					},
					"acknowledged": map[string]any{"type": "array", "items": map[string]any{"type": "string"}, "description": "Requirements (R…) you have taken into your plan."},
				},
				"required": []string{"kind", "note"},
			},
		},
		{
			Name: askTool,
			Description: "Ask your project's orchestrator a question you cannot settle yourself. The call waits " + wait +
				". If the result says the answer is not in yet, or that it will come as a message, end your turn: the " +
				"answer arrives as your next message. Give the options you see when there are some, and the context the " +
				"orchestrator needs to decide without reading your whole session.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"question": map[string]any{"type": "string", "description": "The question, in one or two sentences."},
					"options":  map[string]any{"type": "array", "items": map[string]any{"type": "string"}, "description": "The choices you see, if any."},
					"context":  map[string]any{"type": "string", "description": "What the orchestrator needs to know to decide."},
				},
				"required": []string{"question"},
			},
		},
	}
}

// roughly says a hold in the words a model reads: "5 minutes", "30 seconds".
func roughly(d time.Duration) string {
	if d >= time.Minute && d%time.Minute == 0 {
		if d == time.Minute {
			return "a minute"
		}
		return fmt.Sprintf("%d minutes", int(d/time.Minute))
	}
	return fmt.Sprintf("%d seconds", int((d+time.Second-1)/time.Second))
}

// errInvalid is an argument the model got wrong. It becomes a tool result marked as an error, not a
// protocol error, so the model sees what to correct.
type errInvalid struct{ msg string }

func (e errInvalid) Error() string { return e.msg }

// request is one call to the host: the operation and its fields, in the shape both routes take.
type request struct {
	op          string // "report" or "ask" for the team; the tool's name for a file's set
	fields      map[string]any
	fallback    string // the result when the host stays silent
	fallbackErr bool   // whether that result is an error
	hold        time.Duration
	source      string // the name the post carries at the hook listener
	withTool    bool   // the listener's body names the operation as "tool" (the team's wire)
}

// toolset is what one server offers: its name, its tools, how a call is checked and posted.
type toolset interface {
	name() string
	instructions() string
	list() []tool
	decode(name string, raw json.RawMessage) (request, error)
	source() string
	hello() map[string]any
	unreachable() string
}

// teamSet is the team's two tools, compiled in.
type teamSet struct{ askHold, reportHold time.Duration }

func newTeamSet(askHold, reportHold time.Duration) teamSet {
	return teamSet{askHold: askHold, reportHold: reportHold}
}

func (teamSet) name() string { return ServerName }

func (teamSet) instructions() string {
	return "Report tells your project's orchestrator how your task stands; AskOrchestrator asks it " +
		"something you cannot decide yourself."
}

func (t teamSet) list() []tool { return toolList(t.askHold) }

func (teamSet) source() string { return ingressSource }

func (teamSet) hello() map[string]any { return nil }

func (teamSet) unreachable() string { return "the team is not available" }

func (t teamSet) decode(name string, raw json.RawMessage) (request, error) {
	req, err := decode(name, raw)
	if err != nil {
		return req, err
	}
	req.source, req.withTool = ingressSource, true
	req.hold = t.reportHold
	if req.op == "ask" {
		req.hold = t.askHold
	}
	return req, nil
}

// decode checks a team call's arguments and turns them into the request the host is sent. Optional
// strings are left out when empty and lists are always present, so the host reads one shape.
func decode(name string, raw json.RawMessage) (request, error) {
	switch name {
	case reportTool:
		var a struct {
			Kind         string     `json:"kind"`
			Note         string     `json:"note"`
			Artifacts    []string   `json:"artifacts"`
			Remember     string     `json:"remember"`
			Evidence     []evidence `json:"evidence"`
			Acknowledged []string   `json:"acknowledged"`
		}
		if err := strict(raw, &a); err != nil {
			return request{}, err
		}
		if !contains(reportKinds, a.Kind) {
			return request{}, errInvalid{fmt.Sprintf("kind must be one of %s", strings.Join(reportKinds, ", "))}
		}
		if strings.TrimSpace(a.Note) == "" {
			return request{}, errInvalid{"note is empty"}
		}
		fields := map[string]any{"kind": a.Kind, "note": a.Note, "artifacts": nonNil(a.Artifacts)}
		if strings.TrimSpace(a.Remember) != "" {
			fields["remember"] = a.Remember
		}
		if len(a.Evidence) > 0 {
			fields["evidence"] = a.Evidence
		}
		if len(a.Acknowledged) > 0 {
			fields["acknowledged"] = a.Acknowledged
		}
		return request{op: "report", fields: fields, fallback: reportRecorded}, nil
	case askTool:
		var a struct {
			Question string   `json:"question"`
			Options  []string `json:"options"`
			Context  string   `json:"context"`
		}
		if err := strict(raw, &a); err != nil {
			return request{}, err
		}
		if strings.TrimSpace(a.Question) == "" {
			return request{}, errInvalid{"question is empty"}
		}
		fields := map[string]any{"question": a.Question, "options": nonNil(a.Options)}
		if strings.TrimSpace(a.Context) != "" {
			fields["context"] = a.Context
		}
		return request{op: "ask", fields: fields, fallback: askExpired}, nil
	}
	return request{}, errUnknownTool
}

var errUnknownTool = errors.New("unknown tool")

// evidence is a done report's word on one check or requirement of the task; the host keeps it on the
// item it names and does not judge it.
type evidence struct {
	Item   string `json:"item"`
	How    string `json:"how"`
	Result string `json:"result"`
}

// strict decodes the arguments, refusing a field of the wrong type with a message naming it.
// Unknown fields are ignored: a newer model may add one, and the host would not read it anyway.
func strict(raw json.RawMessage, into any) error {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || string(raw) == "null" {
		raw = []byte("{}")
	}
	if err := json.Unmarshal(raw, into); err != nil {
		var te *json.UnmarshalTypeError
		if errors.As(err, &te) && te.Field != "" {
			field, _, _ := strings.Cut(te.Field, ".")
			return errInvalid{fmt.Sprintf("%s must be %s", field, shapeOf(field))}
		}
		return errInvalid{"the arguments must be an object"}
	}
	return nil
}

// shapeOf names what an argument must be; the decoder's own type names the element of a list, not
// the list.
func shapeOf(field string) string {
	if field == "artifacts" || field == "options" || field == "acknowledged" {
		return "a list of strings"
	}
	if field == "evidence" {
		return "a list of objects with item, how and result"
	}
	return "a string"
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// resultText reads the host's reply to a call: an object's `text` (with `error: true` marking a
// refusal), the `detail` of an HTTP error, a JSON string, or plain text. An empty reply is "": the
// caller's fallback applies.
func resultText(body []byte) (string, bool) {
	body = bytes.TrimSpace(body)
	if len(body) == 0 {
		return "", false
	}
	var obj map[string]any
	if json.Unmarshal(body, &obj) == nil && obj != nil {
		isErr, _ := obj["error"].(bool)
		for _, key := range []string{"text", "detail"} {
			if s, ok := obj[key].(string); ok {
				return s, isErr
			}
		}
		return string(body), isErr
	}
	var s string
	if json.Unmarshal(body, &s) == nil {
		return s, false
	}
	return string(body), false
}
