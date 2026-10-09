package sessions

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// piParser reads pi: ~/.pi/agent/sessions/--<folder>--/<timestamp>_<uuid>.jsonl.
//
// What the format makes a reader get right:
//
//   - The folder name is the cwd with its separators flattened, which cannot be undone; the cwd is
//     the one in the first record (type "session"), never the name.
//   - Every entry but that header has an id and a parentId, so the file is a tree. After a branch
//     the abandoned side stays in the file; the conversation is the path from the last entry back
//     to the root. A session_info entry (a rename) is on that path like any other, so it must stay
//     in the chain even though it is not a turn: dropping it would cut the history there.
//   - A toolResult is its own message with the role "toolResult"; it is a turn of the user's side,
//     as the model is asked about it next.
//   - A compaction entry names the first entry the program kept (firstKeptEntryId); what precedes
//     it on the path is what the summary replaced.
type piParser struct{}

func (piParser) ID() string      { return "pi" }
func (piParser) Name() string    { return "pi" }
func (piParser) Procs() []string { return []string{"pi"} }

func (piParser) Roots(e *Env) []string {
	if dir := e.getenv("PI_CODING_AGENT_SESSION_DIR"); dir != "" {
		return []string{e.expand(dir)}
	}
	if dir := e.getenv("PI_CODING_AGENT_DIR"); dir != "" {
		return []string{filepath.Join(e.expand(dir), "sessions")}
	}
	return []string{filepath.Join(e.Home, ".pi", "agent", "sessions")}
}

func (piParser) Discover(e *Env, root string, emit func(Candidate)) error {
	dirs, err := os.ReadDir(root)
	if err != nil {
		return err
	}
	for _, d := range dirs {
		// A link in the store is someone's, not the program's: it is not followed.
		if !d.IsDir() {
			continue
		}
		folder := filepath.Join(root, d.Name())
		files, err := os.ReadDir(folder)
		if err != nil {
			continue
		}
		for _, f := range files {
			name := f.Name()
			if !f.Type().IsRegular() || !strings.HasSuffix(name, ".jsonl") {
				continue
			}
			info, err := f.Info()
			if err != nil {
				continue
			}
			id := strings.TrimSuffix(name, ".jsonl")
			// The timestamp has no underscore in it, so the first one starts the uuid.
			if _, rest, ok := strings.Cut(id, "_"); ok {
				id = rest
			}
			emit(Candidate{ID: id, Path: filepath.Join(folder, name), Size: info.Size(), Mtime: info.ModTime()})
		}
	}
	return nil
}

// piLine is what the index pass takes from one entry without decoding its content.
type piLine struct {
	off     int64
	n       int
	typ     string
	id      string
	parent  string
	at      time.Time
	role    string
	model   string
	usage   *Usage
	visible bool   // a request or an answer in words, for the count
	kept    string // a compaction's firstKeptEntryId
}

var piKeys = []string{"type", "id", "parentId", "timestamp", "message", "name", "modelId", "cwd", "firstKeptEntryId"}

// piHead collects the header facts a pass meets.
type piHead struct {
	cwd, model, name, firstAsk string
	started, updated           time.Time
}

func (h *piHead) title() string { return first(h.name, h.firstAsk) }

// scanLine reads one record into the head facts and, for an entry of the tree, a line.
func (h *piHead) scanLine(off int64, raw []byte) (piLine, bool) {
	f := fields(raw, piKeys...)
	l := piLine{off: off, n: len(raw), typ: str(f[0]), id: str(f[1]), parent: str(f[2])}
	if at := stamp(str(f[3])); !at.IsZero() {
		l.at = at
		if h.started.IsZero() || at.Before(h.started) {
			h.started = at
		}
		if at.After(h.updated) {
			h.updated = at
		}
	}
	if l.typ == "session" {
		if cwd := str(f[7]); cwd != "" && h.cwd == "" {
			h.cwd = cwd
		}
		return l, false
	}
	if l.id == "" {
		return l, false
	}
	switch l.typ {
	case "session_info":
		if name := strings.TrimSpace(str(f[5])); name != "" {
			h.name = name
		}
	case "model_change":
		if m := str(f[6]); m != "" {
			h.model = m
		}
	case "message":
		m := fields(f[4], "role", "content", "model", "usage")
		l.role, l.model = str(m[0]), str(m[2])
		content := m[1]
		switch l.role {
		case "assistant":
			if l.model != "" {
				h.model = l.model
			}
			if u := fields(m[3], "input", "output", "cacheRead"); u[0] != nil || u[1] != nil {
				l.usage = &Usage{Input: intv(u[0]), Output: intv(u[1]), CacheRead: intv(u[2])}
			}
			l.visible = bytes.Contains(content, []byte(`"type":"text"`))
		case "user":
			if len(content) > 0 && content[0] == '"' {
				l.visible = strings.TrimSpace(strPrefix(content, 200)) != ""
				if l.visible && h.firstAsk == "" {
					h.firstAsk = str(content)
				}
				break
			}
			l.visible = bytes.Contains(content, []byte(`"type":"text"`)) || bytes.Contains(content, []byte(`"type":"image"`))
			if l.visible && h.firstAsk == "" {
				var blocks []struct {
					Type, Text string
				}
				if json.Unmarshal(content, &blocks) == nil {
					for _, b := range blocks {
						if b.Type == "text" && strings.TrimSpace(b.Text) != "" {
							h.firstAsk = b.Text
							break
						}
					}
				}
			}
		}
	case "compaction":
		l.kept = str(f[8])
	}
	return l, true
}

func (piParser) Peek(e *Env, c *Candidate) (Header, error) {
	f, st, err := e.open(c.Root, c.Path)
	if err != nil {
		return Header{}, err
	}
	defer f.Close()
	const window = 64 << 10
	var h piHead
	head := readHead(f, window)
	whole := st.Size() <= 2*window
	if whole {
		head = readHead(f, st.Size())
	}
	for _, l := range lines(head, whole) {
		h.scanLine(0, l)
	}
	if !whole {
		var tail piHead
		for _, l := range lines(readTail(f, st.Size(), window), true) {
			tail.scanLine(0, l)
		}
		h.updated = maxTime(h.updated, tail.updated)
		h.name, h.model = first(tail.name, h.name), first(tail.model, h.model)
	}
	return h.header(c), nil
}

func (h *piHead) header(c *Candidate) Header {
	return Header{ID: c.ID, Cwd: h.cwd, Title: h.title(), StartedAt: Stamp{h.started}, UpdatedAt: Stamp{h.updated},
		Model: h.model, firstPrompt: h.firstAsk}
}

// piAux is what Build needs that one entry does not say.
type piAux struct {
	covers *[2]int
}

// piChain is the conversation of the file: its entries on the path from the last one to the root,
// in order.
func piChain(recs []piLine) []piLine {
	if len(recs) == 0 {
		return nil
	}
	byID := make(map[string]int, len(recs))
	for i, r := range recs {
		byID[r.id] = i
	}
	seen := make(map[int]bool)
	var path []int
	for i, ok := len(recs)-1, true; ok && !seen[i]; i, ok = byID[recs[i].parent] {
		seen[i] = true
		path = append(path, i)
		if recs[i].parent == "" {
			break
		}
	}
	out := make([]piLine, len(path))
	for k, i := range path {
		out[len(path)-1-k] = recs[i]
	}
	return out
}

func (piParser) Index(e *Env, c *Candidate, sidechains bool) (*Index, error) {
	f, _, err := e.open(c.Root, c.Path)
	if err != nil {
		return nil, err
	}
	var h piHead
	var recs []piLine
	consumed, damaged, err := eachLine(f, 0, func(off int64, raw []byte) bool {
		if l, ok := h.scanLine(off, raw); ok {
			recs = append(recs, l)
		}
		return true
	})
	f.Close()
	if err != nil {
		return nil, err
	}
	idx := &Index{Files: []string{c.Path}, Consumed: consumed, Damaged: damaged}
	header := h.header(c)
	byID := map[string]int{} // an entry's id → its place among the turns, for what a compaction kept
	lastCut := 0
	for _, r := range piChain(recs) {
		if r.typ == "session_info" {
			continue // a rename: it names the session, nobody said it
		}
		ref := TurnRef{ExtID: r.id, Parent: r.parent, At: r.at, Spans: []Span{{0, r.off, r.n}}}
		switch {
		case r.typ == "message" && r.role == "assistant":
			ref.Role, ref.Model, ref.Usage = RoleAssistant, r.model, r.usage
		case r.typ == "message" && (r.role == "user" || r.role == "toolResult"):
			ref.Role = RoleUser
		default:
			ref.Role = RoleNote
		}
		if r.visible {
			header.Messages++
		}
		if r.typ == "compaction" {
			header.Flags.Compacted++
			aux := &piAux{}
			if k, ok := byID[r.kept]; ok && k-1 >= lastCut {
				aux.covers = &[2]int{lastCut, k - 1}
			}
			lastCut = len(idx.Turns) + 1
			ref.Aux = aux
		}
		byID[r.id] = len(idx.Turns)
		idx.Turns = append(idx.Turns, ref)
	}
	idx.Header = header
	return idx, nil
}

// piRecord is one entry, decoded for Build.
type piRecord struct {
	Type          string `json:"type"`
	Provider      string `json:"provider"`
	ModelID       string `json:"modelId"`
	ThinkingLevel string `json:"thinkingLevel"`
	Summary       string `json:"summary"`
	FromID        string `json:"fromId"`
	Message       struct {
		Role       string          `json:"role"`
		Content    json.RawMessage `json:"content"`
		ToolCallID string          `json:"toolCallId"`
		ToolName   string          `json:"toolName"`
		IsError    bool            `json:"isError"`
	} `json:"message"`
}

type piBlock struct {
	Type      string          `json:"type"`
	Text      string          `json:"text"`
	Thinking  string          `json:"thinking"`
	Redacted  bool            `json:"redacted"`
	ID        string          `json:"id"`
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments"`
	Data      string          `json:"data"`
	MimeType  string          `json:"mimeType"`
}

func (p piParser) Build(e *Env, idx *Index, ref *TurnRef, records [][]byte) []Part {
	var parts []Part
	aux, _ := ref.Aux.(*piAux)
	for _, raw := range records {
		var r piRecord
		if json.Unmarshal(raw, &r) != nil {
			parts = append(parts, Meta("unreadable", map[string]any{"bytes": len(raw)}))
			continue
		}
		switch r.Type {
		case "message":
			parts = append(parts, p.message(raw, r)...)
		case "compaction":
			var covers *[2]int
			if aux != nil {
				covers = aux.covers
			}
			parts = append(parts, Compaction(r.Summary, covers, false))
		case "model_change":
			parts = append(parts, Meta("model_change", map[string]any{"provider": r.Provider, "model": r.ModelID}))
		case "thinking_level_change":
			parts = append(parts, Meta("thinking_level_change", map[string]any{"level": r.ThinkingLevel}))
		case "branch_summary":
			parts = append(parts, Meta("branch_summary", map[string]any{"from_id": r.FromID, "text": clip(r.Summary, noteLimit)}))
		default:
			// context_edit and whatever a later version adds: kept as a note of the entry's own
			// fields, so nothing is silently lost and nothing is guessed at.
			var rest map[string]json.RawMessage
			_ = json.Unmarshal(raw, &rest)
			for _, k := range []string{"type", "id", "parentId", "timestamp"} {
				delete(rest, k)
			}
			body, _ := json.Marshal(rest)
			parts = append(parts, Meta(r.Type, map[string]any{"text": clip(string(body), noteLimit)}))
		}
	}
	return parts
}

// message makes the parts of a message entry by its role.
func (p piParser) message(raw []byte, r piRecord) []Part {
	var parts []Part
	content := r.Message.Content
	if r.Message.Role == "toolResult" {
		result := ToolResult(r.Message.ToolCallID, "", r.Message.IsError)
		var texts []string
		var blocks []piBlock
		if len(content) > 0 && content[0] == '"' {
			var s string
			_ = json.Unmarshal(content, &s)
			texts = append(texts, s)
		} else if json.Unmarshal(content, &blocks) == nil {
			for _, b := range blocks {
				switch b.Type {
				case "text":
					texts = append(texts, b.Text)
				case "image":
					result.Images = append(result.Images, Image{Mime: b.MimeType, DataB64: b.Data, Bytes: int64(base64.StdEncoding.DecodedLen(len(b.Data)))})
				}
			}
		}
		result.Output = strings.Join(texts, "\n")
		return []Part{result}
	}
	if r.Message.Role != "user" && r.Message.Role != "assistant" {
		// A shell command the operator ran, a custom message of an extension: shown as a note.
		var m struct {
			Message json.RawMessage `json:"message"`
		}
		_ = json.Unmarshal(raw, &m)
		return []Part{Meta("message:"+r.Message.Role, map[string]any{"text": clip(string(m.Message), noteLimit)})}
	}
	if len(content) > 0 && content[0] == '"' {
		var s string
		_ = json.Unmarshal(content, &s)
		if strings.TrimSpace(s) != "" {
			parts = append(parts, Text(s))
		}
		return parts
	}
	var blocks []piBlock
	if json.Unmarshal(content, &blocks) != nil {
		return parts
	}
	for _, b := range blocks {
		switch b.Type {
		case "text":
			if strings.TrimSpace(b.Text) != "" {
				parts = append(parts, Text(b.Text))
			}
		case "thinking":
			// The signature is the provider's and is dropped: no other model may be sent it.
			parts = append(parts, Thinking(b.Thinking, b.Redacted && b.Thinking == ""))
		case "toolCall":
			parts = append(parts, ToolCall(b.ID, b.Name, b.Arguments, ToolNative, ""))
		case "image":
			parts = append(parts, Part{Kind: KindImage, Mime: b.MimeType, DataB64: b.Data, Bytes: int64(base64.StdEncoding.DecodedLen(len(b.Data)))})
		default:
			parts = append(parts, Meta("block:"+b.Type, nil))
		}
	}
	return parts
}
