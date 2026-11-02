package sessions

import (
	"encoding/json"
	"time"
)

// ModelVersion is the `v` of the normalised model. The host refuses a version it does not know
// rather than guess at fields that changed meaning.
const ModelVersion = 1

// Stamp is a time on the wire: RFC 3339 in UTC, or null when the source never said. A zero time
// written as "0001-01-01T00:00:00Z" would sort as the oldest session instead of an unknown one.
type Stamp struct{ time.Time }

func (s Stamp) MarshalJSON() ([]byte, error) {
	if s.IsZero() {
		return []byte("null"), nil
	}
	return json.Marshal(s.UTC().Format(time.RFC3339Nano))
}

func (s *Stamp) UnmarshalJSON(b []byte) error {
	if string(b) == "null" || string(b) == `""` {
		s.Time = time.Time{}
		return nil
	}
	var text string
	if err := json.Unmarshal(b, &text); err != nil {
		return err
	}
	t, err := time.Parse(time.RFC3339Nano, text)
	s.Time = t
	return err
}

// Header is one session as sessions.scan lists it (ForeignSessionHeader).
type Header struct {
	V         int    `json:"v"`
	Harness   string `json:"harness"`
	ID        string `json:"id"`
	Cwd       string `json:"cwd"`
	Title     string `json:"title"`
	StartedAt Stamp  `json:"started_at"`
	UpdatedAt Stamp  `json:"updated_at"`
	Messages  int    `json:"messages"`
	Bytes     int64  `json:"bytes"`
	Branch    string `json:"branch"`
	Model     string `json:"model"`
	Flags     Flags  `json:"flags"`
	Source    Source `json:"source"`

	// What the listing needs and the wire does not carry.
	firstPrompt string // the first request, for the query, before the title replaced it
	parent      string // a sub-agent's parent session: such a session is listed inside its parent, not beside it
	version     string // the program's version that wrote the session, for sessions.harnesses
	full        bool   // the counts come from a pass over every line, not from the head and the tail
}

// Flags are the counts and states the listing shows beside a session.
type Flags struct {
	Compacted  int  `json:"compacted"`
	Sidechains int  `json:"sidechains"`
	Live       bool `json:"live"`
}

// Source is the opaque reference sessions.read takes back. Only the daemon interprets it.
type Source struct {
	Path string `json:"path"`
}

// Turn is one entry of sessions.read (ForeignTurn).
type Turn struct {
	Seq       int    `json:"seq"`
	ExtID     string `json:"ext_id"`
	Parent    string `json:"parent"`
	Role      string `json:"role"` // user | assistant | system_note
	At        Stamp  `json:"at"`
	Sidechain string `json:"sidechain"`
	Model     string `json:"model"`
	Parts     []Part `json:"parts"`
	Usage     *Usage `json:"usage,omitempty"`
}

// Usage is what the model reported for the turn, when the source kept it.
type Usage struct {
	Input     int64 `json:"input"`
	Output    int64 `json:"output"`
	CacheRead int64 `json:"cache_read"`
}

// The roles of a turn.
const (
	RoleUser      = "user"
	RoleAssistant = "assistant"
	RoleNote      = "system_note"
)

// The kinds of a part.
const (
	KindText       = "text"
	KindThinking   = "thinking"
	KindImage      = "image"
	KindToolCall   = "tool_call"
	KindToolResult = "tool_result"
	KindCompaction = "compaction"
	KindMeta       = "meta"
)

// The kinds of a tool call. The wire field is `tool_kind`, because `kind` is already the part's
// discriminator and one JSON object cannot carry the key twice.
const (
	ToolNative = "native"
	ToolMCP    = "mcp"
	ToolCustom = "custom"
)

// Part is one ForeignPart. It is one struct for every kind so a parser can build parts without a
// type switch; MarshalJSON writes only the fields the kind has, so the host sees exactly the shape
// the model names for each kind.
type Part struct {
	Kind string

	Text      string // text, thinking
	Encrypted bool   // thinking whose text the program kept only encrypted

	Mime    string // image
	DataB64 string
	Path    string
	Bytes   int64

	CallID   string // tool_call, tool_result
	Name     string
	Input    json.RawMessage
	ToolKind string
	Server   string

	Output    string // tool_result
	IsError   bool
	Truncated bool
	Images    []Image

	Summary string // compaction
	Covers  *[2]int
	Auto    bool

	MetaType string // meta
	Data     any
}

// Image is a picture inside a tool result.
type Image struct {
	Mime    string `json:"mime"`
	DataB64 string `json:"data_b64,omitempty"`
	Bytes   int64  `json:"bytes"`
}

func (p Part) MarshalJSON() ([]byte, error) {
	switch p.Kind {
	case KindText:
		return json.Marshal(struct {
			Kind string `json:"kind"`
			Text string `json:"text"`
		}{p.Kind, p.Text})
	case KindThinking:
		return json.Marshal(struct {
			Kind      string `json:"kind"`
			Text      string `json:"text"`
			Encrypted bool   `json:"encrypted"`
		}{p.Kind, p.Text, p.Encrypted})
	case KindImage:
		return json.Marshal(struct {
			Kind    string `json:"kind"`
			Mime    string `json:"mime"`
			DataB64 string `json:"data_b64,omitempty"`
			Path    string `json:"path,omitempty"`
			Bytes   int64  `json:"bytes"`
		}{p.Kind, p.Mime, p.DataB64, p.Path, p.Bytes})
	case KindToolCall:
		input := p.Input
		if len(input) == 0 {
			input = json.RawMessage("{}")
		}
		kind := p.ToolKind
		if kind == "" {
			kind = ToolNative
		}
		return json.Marshal(struct {
			Kind     string          `json:"kind"`
			CallID   string          `json:"call_id"`
			Name     string          `json:"name"`
			Input    json.RawMessage `json:"input"`
			ToolKind string          `json:"tool_kind"`
			Server   string          `json:"server,omitempty"`
		}{p.Kind, p.CallID, p.Name, input, kind, p.Server})
	case KindToolResult:
		images := p.Images
		if images == nil {
			images = []Image{}
		}
		return json.Marshal(struct {
			Kind      string  `json:"kind"`
			CallID    string  `json:"call_id"`
			Output    string  `json:"output"`
			IsError   bool    `json:"is_error"`
			Truncated bool    `json:"truncated"`
			Images    []Image `json:"images"`
		}{p.Kind, p.CallID, p.Output, p.IsError, p.Truncated, images})
	case KindCompaction:
		return json.Marshal(struct {
			Kind    string  `json:"kind"`
			Summary string  `json:"summary"`
			Covers  *[2]int `json:"covers"`
			Auto    bool    `json:"auto"`
		}{p.Kind, p.Summary, p.Covers, p.Auto})
	default:
		return json.Marshal(struct {
			Kind string `json:"kind"`
			Type string `json:"type"`
			Data any    `json:"data"`
		}{KindMeta, p.MetaType, p.Data})
	}
}

// Text, Thinking, Meta and the others are the constructors the parsers use.
func Text(text string) Part { return Part{Kind: KindText, Text: text} }

func Thinking(text string, encrypted bool) Part {
	return Part{Kind: KindThinking, Text: text, Encrypted: encrypted}
}

func Meta(kind string, data any) Part { return Part{Kind: KindMeta, MetaType: kind, Data: data} }

func ToolCall(callID, name string, input json.RawMessage, kind, server string) Part {
	return Part{Kind: KindToolCall, CallID: callID, Name: name, Input: input, ToolKind: kind, Server: server}
}

func ToolResult(callID, output string, isError bool) Part {
	return Part{Kind: KindToolResult, CallID: callID, Output: output, IsError: isError}
}

func Compaction(summary string, covers *[2]int, auto bool) Part {
	return Part{Kind: KindCompaction, Summary: summary, Covers: covers, Auto: auto}
}
