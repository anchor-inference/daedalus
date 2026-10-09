package sessions

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// claudeParser reads Claude Code: ~/.claude/projects/<folder>/<session>.jsonl, with the sub-agents
// of a session in <session>/subagents/agent-<id>.jsonl beside it.
//
// What the format makes a reader get right:
//
//   - One API response is written as one record per content block, all with the same message.id;
//     they are one turn.
//   - The file is a tree (uuid, parentUuid). After a rewind or an edited prompt the abandoned branch
//     stays in the file; the conversation is the path from the last record back to the root. A
//     compaction starts a new root (parentUuid null) whose logicalParentUuid is the last record
//     before it, so the walk crosses it and the history before the compaction is kept. A compaction
//     that preserves the last few messages verbatim (compactMetadata.preservedSegment) may instead
//     point it at the tail of that segment, which can be written after the summary: a descendant of
//     the boundary. Following it would close a loop, and the walk used to stop there, so the session
//     began at the boundary and lost everything before it, the first request included. There the
//     walk takes the record written just before the boundary, which is the last one before it.
//   - A compaction is a system record with subtype compact_boundary followed by a user record with
//     isCompactSummary, which carries the summary: one compaction part. Newer versions write the
//     context they re-inject (instructions, session context, the date) as attachments between the
//     two, so the summary is looked for past those; pairing only an adjacent one made every such
//     compaction two, the first of them "without its summary".
//   - The folder name encodes the cwd lossily ("-" stands for "/", ".", "_" and itself); the cwd is
//     taken from the records, never from the name.
//   - A large tool output is moved to <session>/tool-results/ and the record keeps a preview; the
//     file is read back, below the store's root only.
type claudeParser struct{}

func (claudeParser) ID() string      { return "claude" }
func (claudeParser) Name() string    { return "Claude Code" }
func (claudeParser) Procs() []string { return []string{"claude"} }

func (claudeParser) Roots(e *Env) []string {
	if dir := e.getenv("CLAUDE_CONFIG_DIR"); dir != "" {
		return []string{filepath.Join(e.expand(dir), "projects")}
	}
	return []string{filepath.Join(e.Home, ".claude", "projects")}
}

func (claudeParser) Discover(e *Env, root string, emit func(Candidate)) error {
	dirs, err := os.ReadDir(root)
	if err != nil {
		return err
	}
	for _, d := range dirs {
		// Links are not followed: a folder in the store is the program's, a link is someone's.
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
			// agent-*.jsonl at this level are sub-agents of versions before 2.x, read with their
			// session's own sub-agents only when they carry its id: not listed as sessions.
			if !f.Type().IsRegular() || !strings.HasSuffix(name, ".jsonl") || strings.HasPrefix(name, "agent-") {
				continue
			}
			info, err := f.Info()
			if err != nil {
				continue
			}
			id := strings.TrimSuffix(name, ".jsonl")
			c := Candidate{ID: id, Path: filepath.Join(folder, name), Size: info.Size(), Mtime: info.ModTime()}
			sub := filepath.Join(folder, id, "subagents")
			if agents, err := os.ReadDir(sub); err == nil {
				for _, a := range agents {
					if !a.Type().IsRegular() || !strings.HasSuffix(a.Name(), ".jsonl") {
						continue
					}
					ai, err := a.Info()
					if err != nil {
						continue
					}
					c.Extra = append(c.Extra, filepath.Join(sub, a.Name()))
					c.Size += ai.Size()
					if ai.ModTime().After(c.Mtime) {
						c.Mtime = ai.ModTime()
					}
				}
				sort.Strings(c.Extra)
			}
			emit(c)
		}
	}
	return nil
}

// claudeNoise are the starts of user records that the CLI wrote, not the operator: slash-command
// echoes, hook output, reminders, a background task's notice. They become notes, not words.
var claudeNoise = []string{"<command-name>", "<command-message>", "<command-args>", "<local-command-stdout>",
	"<local-command-stderr>", "<local-command-caveat>", "<system-reminder>", "<task-notification>", "<bash-input>",
	"<bash-stdout>", "<bash-stderr>", "<user-prompt-submit-hook>", "Caveat: The messages below were generated"}

func claudeIsNoise(text string) bool {
	t := strings.TrimLeft(text, " \n\t")
	for _, n := range claudeNoise {
		if strings.HasPrefix(t, n) {
			return true
		}
	}
	return false
}

// claudePasted takes off the tags Claude Code wraps a collapsed paste in, as
// daedalus.harness.claude.unpasted does.
var (
	claudePastedOpen  = regexp.MustCompile(`\n*<pasted_content id="[^"]*">\n?`)
	claudePastedClose = regexp.MustCompile(`\n?</pasted_content id="[^"]*">\n*`)
)

func claudeUnpasted(s string) string {
	return strings.TrimSpace(claudePastedClose.ReplaceAllString(claudePastedOpen.ReplaceAllString(s, "\n\n"), "\n\n"))
}

// claudeLine is what the index pass takes from one record without decoding its content.
type claudeLine struct {
	off       int64
	n         int
	typ       string
	uuid      string
	parent    string
	at        time.Time
	msgID     string
	model     string
	usage     *Usage
	meta      bool
	summary   bool // isCompactSummary
	boundary  bool // a compact_boundary
	subtype   string
	visible   bool // a request or an answer in words, for the count
	results   []string
	toolUses  []string
	sidechain string
}

var claudeKeys = []string{"type", "uuid", "parentUuid", "logicalParentUuid", "timestamp", "isMeta", "isCompactSummary",
	"message", "subtype", "cwd", "gitBranch", "customTitle", "aiTitle", "agentName", "lastPrompt", "version"}

// claudeHead collects the header facts a pass meets.
type claudeHead struct {
	cwd, branch, model, version             string
	started, updated                        time.Time
	custom, ai, agent, lastPrompt, firstAsk string
}

func (h *claudeHead) title() string {
	for _, t := range []string{h.custom, h.ai, h.agent, h.lastPrompt, h.firstAsk} {
		if strings.TrimSpace(t) != "" {
			return t
		}
	}
	return ""
}

// scanLine reads one record into the head facts and, for a record of the conversation, a line.
func (h *claudeHead) scanLine(off int64, raw []byte) (claudeLine, bool) {
	f := fields(raw, claudeKeys...)
	l := claudeLine{off: off, n: len(raw), typ: str(f[0]), uuid: str(f[1]), parent: str(f[2])}
	if l.parent == "" {
		l.parent = str(f[3])
	}
	if at := stamp(str(f[4])); !at.IsZero() {
		l.at = at
		if h.started.IsZero() || at.Before(h.started) {
			h.started = at
		}
		if at.After(h.updated) {
			h.updated = at
		}
	}
	if cwd := str(f[9]); cwd != "" && h.cwd == "" {
		h.cwd = cwd
	}
	if b := str(f[10]); b != "" {
		h.branch = b
	}
	if v := str(f[15]); v != "" {
		h.version = v
	}
	switch l.typ {
	case "custom-title":
		h.custom = str(f[11])
		return l, false
	case "ai-title":
		h.ai = str(f[12])
		return l, false
	case "agent-name":
		h.agent = str(f[13])
		return l, false
	case "last-prompt":
		h.lastPrompt = str(f[14])
		return l, false
	case "user", "assistant", "system", "attachment":
	default:
		return l, false
	}
	if l.uuid == "" {
		return l, false
	}
	l.meta, l.summary = boolv(f[5]), boolv(f[6])
	l.subtype = str(f[8])
	l.boundary = l.typ == "system" && l.subtype == "compact_boundary"
	if l.typ == "user" || l.typ == "assistant" {
		m := fields(f[7], "id", "model", "usage", "content")
		l.msgID, l.model = str(m[0]), str(m[1])
		content := m[3]
		if l.typ == "assistant" {
			if l.model != "" && l.model != "<synthetic>" {
				h.model = l.model
			}
			if u := fields(m[2], "input_tokens", "output_tokens", "cache_read_input_tokens"); u[0] != nil || u[1] != nil {
				l.usage = &Usage{Input: intv(u[0]), Output: intv(u[1]), CacheRead: intv(u[2])}
			}
			l.visible = bytes.Contains(content, []byte(`"type":"text"`))
			l.toolUses = idsAfter(content, `"type":"tool_use","id":"`)
		} else {
			switch {
			case len(content) > 0 && content[0] == '"':
				head := strPrefix(content, 200)
				l.visible = !l.meta && !l.summary && !claudeIsNoise(head) && !strings.HasPrefix(head, "[Request interrupted")
				if l.visible && h.firstAsk == "" {
					h.firstAsk = claudeUnpasted(str(content))
				}
			case bytes.Contains(content, []byte(`"tool_result"`)):
				l.results = idsAfter(content, `"tool_use_id":"`)
			default:
				l.visible = !l.meta && !l.summary && (bytes.Contains(content, []byte(`"type":"text"`)) || bytes.Contains(content, []byte(`"type":"image"`)))
				if l.visible && h.firstAsk == "" {
					var blocks []struct {
						Type, Text string
					}
					if json.Unmarshal(content, &blocks) == nil {
						for _, b := range blocks {
							if b.Type == "text" && !claudeIsNoise(b.Text) {
								h.firstAsk = claudeUnpasted(b.Text)
								break
							}
						}
					}
				}
			}
		}
	}
	return l, true
}

// idsAfter finds every quoted value that follows a marker, such as the tool_use_id of each result.
func idsAfter(b []byte, marker string) []string {
	var out []string
	m := []byte(marker)
	for {
		i := bytes.Index(b, m)
		if i < 0 {
			return out
		}
		b = b[i+len(m):]
		j := bytes.IndexByte(b, '"')
		if j < 0 {
			return out
		}
		out = append(out, string(b[:j]))
		b = b[j:]
	}
}

func (claudeParser) Peek(e *Env, c *Candidate) (Header, error) {
	f, st, err := e.open(c.Root, c.Path)
	if err != nil {
		return Header{}, err
	}
	defer f.Close()
	const window = 64 << 10
	var h claudeHead
	head := readHead(f, window)
	whole := st.Size() <= 2*window
	if whole {
		head = readHead(f, st.Size())
	}
	for _, l := range lines(head, whole) {
		h.scanLine(0, l)
	}
	if !whole {
		var tail claudeHead
		for _, l := range lines(readTail(f, st.Size(), window), true) {
			tail.scanLine(0, l)
		}
		h.updated = maxTime(h.updated, tail.updated)
		h.custom, h.ai, h.agent, h.lastPrompt = first(tail.custom, h.custom), first(tail.ai, h.ai), first(tail.agent, h.agent), first(tail.lastPrompt, h.lastPrompt)
		h.model, h.branch, h.version = first(tail.model, h.model), first(tail.branch, h.branch), first(tail.version, h.version)
		if h.cwd == "" {
			h.cwd = tail.cwd
		}
	}
	return h.header(c), nil
}

func (h *claudeHead) header(c *Candidate) Header {
	return Header{ID: c.ID, Cwd: h.cwd, Title: h.title(), StartedAt: Stamp{h.started}, UpdatedAt: Stamp{h.updated},
		Branch: h.branch, Model: h.model, Flags: Flags{Sidechains: len(c.Extra)}, firstPrompt: h.firstAsk, version: h.version}
}

func first(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func maxTime(a, b time.Time) time.Time {
	if b.After(a) {
		return b
	}
	return a
}

// claudeAux is what Build needs that one record does not say.
type claudeAux struct {
	covers *[2]int
	agent  map[string]any // the first turn of a sub-agent: who it was and which call started it
}

// claudeChain is the conversation of one file: its records on the path from the last one to the
// root, in order.
func claudeChain(recs []claudeLine) []claudeLine {
	if len(recs) == 0 {
		return nil
	}
	byID := make(map[string]int, len(recs))
	for i, r := range recs {
		byID[r.uuid] = i
	}
	seen := make(map[int]bool)
	var path []int
	for i := len(recs) - 1; i >= 0 && !seen[i]; {
		seen[i] = true
		path = append(path, i)
		if recs[i].parent == "" {
			break
		}
		next, ok := byID[recs[i].parent]
		if (!ok || seen[next]) && recs[i].boundary {
			// The logical parent is a preserved message written after the boundary (see the type's
			// comment), or not in this file: the record before the boundary is the one it follows.
			next, ok = i-1, true
		}
		if !ok {
			break
		}
		i = next
	}
	out := make([]claudeLine, len(path))
	for k, i := range path {
		out[len(path)-1-k] = recs[i]
	}
	return out
}

// claudeTurns groups a chain into turns.
func claudeTurns(chain []claudeLine, file int, sidechain string) []TurnRef {
	var out []TurnRef
	paired := map[int]bool{} // summaries already joined to the boundary before them
	for i := 0; i < len(chain); i++ {
		if paired[i] {
			continue
		}
		r := chain[i]
		ref := TurnRef{ExtID: r.uuid, Parent: r.parent, At: r.at, Sidechain: sidechain, Spans: []Span{{file, r.off, r.n}}}
		switch {
		case r.typ == "assistant":
			ref.Role, ref.Model, ref.Usage = RoleAssistant, r.model, r.usage
			for i+1 < len(chain) && chain[i+1].typ == "assistant" && r.msgID != "" && chain[i+1].msgID == r.msgID {
				i++
				ref.Spans = append(ref.Spans, Span{file, chain[i].off, chain[i].n})
				if chain[i].usage != nil {
					ref.Usage = chain[i].usage
				}
			}
		case r.boundary:
			ref.Role = RoleNote
			for j := i + 1; j < len(chain) && (chain[j].typ == "attachment" || chain[j].summary); j++ {
				if chain[j].summary {
					// The attachments between stay where they are, as their own note after this one.
					ref.Spans = append(ref.Spans, Span{file, chain[j].off, chain[j].n})
					paired[j] = true
					break
				}
			}
		case r.summary:
			ref.Role = RoleNote // a summary whose boundary is not on the chain
		case r.typ == "system":
			switch r.subtype {
			case "turn_duration", "stop_hook_summary", "bridge_status":
				continue // timings and hook bookkeeping: nothing anyone said
			}
			ref.Role = RoleNote
		case r.typ == "attachment":
			ref.Role = RoleNote
			for i+1 < len(chain) && chain[i+1].typ == "attachment" {
				i++
				ref.Spans = append(ref.Spans, Span{file, chain[i].off, chain[i].n})
			}
		case r.meta:
			ref.Role = RoleNote
		default:
			ref.Role = RoleUser
		}
		out = append(out, ref)
	}
	return out
}

func (p claudeParser) Index(e *Env, c *Candidate, sidechains bool) (*Index, error) {
	f, _, err := e.open(c.Root, c.Path)
	if err != nil {
		return nil, err
	}
	var h claudeHead
	var recs []claudeLine
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
	chain := claudeChain(recs)
	at := make(map[int64]int, len(chain)) // a main record's offset → its place in the chain
	for i, r := range chain {
		at[r.off] = i
	}
	turns := claudeTurns(chain, 0, "")
	header := h.header(c)
	callTurn := map[string]int{} // a call's id → the turn holding its result, or the call itself
	for i := range turns {
		t := &turns[i]
		counted := false
		for _, sp := range t.Spans {
			r := chain[at[sp.Off]]
			if r.visible && !counted {
				header.Messages++
				counted = true
			}
			for _, id := range r.toolUses {
				if _, ok := callTurn[id]; !ok {
					callTurn[id] = i
				}
			}
			for _, id := range r.results {
				callTurn[id] = i
			}
		}
		if r := chain[at[t.Spans[0].Off]]; t.Role == RoleNote && (r.boundary || r.summary) {
			header.Flags.Compacted++
			t.Aux = &claudeAux{}
		}
	}
	idx.Header = header
	if !sidechains {
		idx.Turns = turns
		claudeCovers(idx.Turns)
		return idx, nil
	}
	// Sub-agents: each file's own chain, placed after the turn that holds the result of the call that
	// started it (or the call, while it runs), or at the end when neither is found.
	after := map[int][]TurnRef{}
	var tail []TurnRef
	for k, path := range c.Extra {
		file := k + 1
		idx.Files = append(idx.Files, path)
		sf, _, err := e.open(c.Root, path)
		if err != nil {
			continue
		}
		var sh claudeHead
		var srecs []claudeLine
		_, sd, _ := eachLine(sf, 0, func(off int64, raw []byte) bool {
			if l, ok := sh.scanLine(off, raw); ok {
				srecs = append(srecs, l)
			}
			return true
		})
		sf.Close()
		idx.Damaged += sd
		agentID := strings.TrimPrefix(strings.TrimSuffix(filepath.Base(path), ".jsonl"), "agent-")
		sturns := claudeTurns(claudeChain(srecs), file, agentID)
		if len(sturns) == 0 {
			continue
		}
		meta := claudeAgentMeta(e, c.Root, path)
		meta["id"] = agentID
		sturns[0].Aux = &claudeAux{agent: meta}
		call, _ := meta["call_id"].(string)
		if i, ok := callTurn[call]; ok && call != "" {
			after[i] = append(after[i], sturns...)
		} else {
			tail = append(tail, sturns...)
		}
	}
	for i, t := range turns {
		idx.Turns = append(idx.Turns, t)
		idx.Turns = append(idx.Turns, after[i]...)
	}
	idx.Turns = append(idx.Turns, tail...)
	claudeCovers(idx.Turns)
	return idx, nil
}

// claudeCovers gives each compaction the range of turns it summarised: those since the previous
// compaction, by their place in the final order (sub-agents included).
func claudeCovers(turns []TurnRef) {
	lastCut := 0
	for i := range turns {
		a, ok := turns[i].Aux.(*claudeAux)
		if !ok || a.agent != nil || turns[i].Sidechain != "" {
			continue
		}
		a.covers = nil
		if i-1 >= lastCut {
			a.covers = &[2]int{lastCut, i - 1}
		}
		lastCut = i + 1
	}
}

// claudeAgentMeta reads a sub-agent's .meta.json: its type, the task it was given and the call
// that started it.
func claudeAgentMeta(e *Env, root, path string) map[string]any {
	out := map[string]any{}
	f, st, err := e.open(root, strings.TrimSuffix(path, ".jsonl")+".meta.json")
	if err != nil || st.Size() > 1<<20 {
		if f != nil {
			f.Close()
		}
		return out
	}
	defer f.Close()
	var m struct {
		AgentType   string `json:"agentType"`
		Description string `json:"description"`
		ToolUseID   string `json:"toolUseId"`
		Model       string `json:"model"`
	}
	if json.Unmarshal(readHead(f, st.Size()), &m) == nil {
		out["agent_type"], out["description"], out["call_id"], out["model"] = m.AgentType, m.Description, m.ToolUseID, m.Model
	}
	return out
}

// claudeRecord is one record, decoded for Build.
type claudeRecord struct {
	Type             string          `json:"type"`
	Subtype          string          `json:"subtype"`
	IsMeta           bool            `json:"isMeta"`
	IsCompactSummary bool            `json:"isCompactSummary"`
	Content          json.RawMessage `json:"content"`
	Level            string          `json:"level"`
	Message          struct {
		Content json.RawMessage `json:"content"`
	} `json:"message"`
	ToolUseResult   json.RawMessage `json:"toolUseResult"`
	CompactMetadata struct {
		Trigger   string `json:"trigger"`
		PreTokens int64  `json:"preTokens"`
	} `json:"compactMetadata"`
	Attachment json.RawMessage `json:"attachment"`
	Rendered   []struct {
		Content string `json:"content"`
	} `json:"rendered"`
	ContinuedIn string `json:"continuedInSessionId"`
}

type claudeBlock struct {
	Type      string          `json:"type"`
	Text      string          `json:"text"`
	Thinking  string          `json:"thinking"`
	ID        string          `json:"id"`
	Name      string          `json:"name"`
	Input     json.RawMessage `json:"input"`
	ToolUseID string          `json:"tool_use_id"`
	Content   json.RawMessage `json:"content"`
	IsError   bool            `json:"is_error"`
	Source    struct {
		Type      string `json:"type"`
		MediaType string `json:"media_type"`
		Data      string `json:"data"`
		URL       string `json:"url"`
	} `json:"source"`
}

// noteLimit bounds the text a note carries: a reminder or an attachment listing is context the
// program gave its model, worth seeing in the transcript, not worth megabytes.
const noteLimit = 4000

// persistedLimit bounds what is read back of a tool output the program moved to a file.
const persistedLimit = 256 << 10

func (p claudeParser) Build(e *Env, idx *Index, ref *TurnRef, records [][]byte) []Part {
	var parts []Part
	aux, _ := ref.Aux.(*claudeAux)
	if aux != nil && aux.agent != nil {
		parts = append(parts, Meta("sidechain", aux.agent))
	}
	var summary string
	var boundary *claudeRecord
	for _, raw := range records {
		var r claudeRecord
		if json.Unmarshal(raw, &r) != nil {
			parts = append(parts, Meta("unreadable", map[string]any{"bytes": len(raw)}))
			continue
		}
		switch {
		case r.Type == "system" && r.Subtype == "compact_boundary":
			rr := r
			boundary = &rr
		case r.IsCompactSummary:
			summary = claudeText(r.Message.Content)
		case r.Type == "system":
			parts = append(parts, Meta("system:"+r.Subtype, map[string]any{"level": r.Level, "text": clip(claudeText(r.Content), noteLimit)}))
		case r.Type == "attachment":
			var a struct {
				Type string `json:"type"`
			}
			_ = json.Unmarshal(r.Attachment, &a)
			text := ""
			for _, rd := range r.Rendered {
				text += rd.Content
			}
			parts = append(parts, Meta("attachment:"+a.Type, map[string]any{"text": clip(text, noteLimit)}))
		case r.IsMeta:
			parts = append(parts, Meta("context", map[string]any{"text": clip(claudeText(r.Message.Content), noteLimit)}))
		default:
			parts = append(parts, p.blocks(e, idx, r)...)
		}
	}
	if boundary != nil || summary != "" {
		var covers *[2]int
		if aux != nil {
			covers = aux.covers
		}
		auto := boundary != nil && boundary.CompactMetadata.Trigger == "auto"
		parts = append(parts, Compaction(summary, covers, auto))
	}
	return parts
}

// claudeText is the words of a content: a string, or the text blocks of a list.
func claudeText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	if raw[0] == '"' {
		var s string
		_ = json.Unmarshal(raw, &s)
		return s
	}
	var blocks []claudeBlock
	if json.Unmarshal(raw, &blocks) != nil {
		return ""
	}
	var texts []string
	for _, b := range blocks {
		if b.Type == "text" {
			texts = append(texts, b.Text)
		}
	}
	return strings.Join(texts, "\n\n")
}

func (p claudeParser) blocks(e *Env, idx *Index, r claudeRecord) []Part {
	var parts []Part
	content := r.Message.Content
	user := r.Type == "user"
	words := func(text string) {
		if user && claudeIsNoise(text) {
			parts = append(parts, Meta("cli", map[string]any{"text": clip(text, noteLimit)}))
			return
		}
		if user {
			text = claudeUnpasted(text)
			if strings.HasPrefix(text, "[Request interrupted") {
				parts = append(parts, Meta("interrupted", map[string]any{"text": text}))
				return
			}
		}
		if strings.TrimSpace(text) != "" {
			parts = append(parts, Text(text))
		}
	}
	if len(content) > 0 && content[0] == '"' {
		var s string
		_ = json.Unmarshal(content, &s)
		words(s)
		return parts
	}
	var blocks []claudeBlock
	if json.Unmarshal(content, &blocks) != nil {
		return parts
	}
	for _, b := range blocks {
		switch b.Type {
		case "text":
			words(b.Text)
		case "thinking":
			// The signature is the provider's and is dropped: no other model may be sent it. Claude
			// Code keeps most thinking only signed, with an empty text; that is thinking that happened
			// and cannot be read, as a redacted block is, and passed on as visible it vanished.
			parts = append(parts, Thinking(b.Thinking, strings.TrimSpace(b.Thinking) == ""))
		case "redacted_thinking":
			parts = append(parts, Thinking("", true))
		case "tool_use", "server_tool_use":
			kind, server := ToolNative, ""
			if b.Type == "server_tool_use" {
				kind = ToolCustom
			} else if rest, ok := strings.CutPrefix(b.Name, "mcp__"); ok {
				kind = ToolMCP
				server, _, _ = strings.Cut(rest, "__")
			}
			parts = append(parts, ToolCall(b.ID, b.Name, b.Input, kind, server))
		case "tool_result", "web_search_tool_result":
			parts = append(parts, p.result(e, idx, b, r.ToolUseResult))
		case "image":
			parts = append(parts, claudeImage(b))
		default:
			parts = append(parts, Meta("block:"+b.Type, nil))
		}
	}
	return parts
}

func claudeImage(b claudeBlock) Part {
	img := Part{Kind: KindImage, Mime: b.Source.MediaType}
	if b.Source.Type == "base64" {
		img.DataB64 = b.Source.Data
		img.Bytes = int64(base64.StdEncoding.DecodedLen(len(b.Source.Data)))
	} else {
		img.Path = b.Source.URL
	}
	return img
}

// result is a tool result: its words, its pictures, and when the program moved a large output to a
// file, that file read back (bounded, and only from below the store's root).
func (p claudeParser) result(e *Env, idx *Index, b claudeBlock, structured json.RawMessage) Part {
	part := ToolResult(b.ToolUseID, "", b.IsError)
	if len(b.Content) > 0 && b.Content[0] == '"' {
		_ = json.Unmarshal(b.Content, &part.Output)
	} else {
		var inner []claudeBlock
		if json.Unmarshal(b.Content, &inner) == nil {
			var texts []string
			for _, ib := range inner {
				switch ib.Type {
				case "text":
					texts = append(texts, ib.Text)
				case "image":
					img := claudeImage(ib)
					part.Images = append(part.Images, Image{Mime: img.Mime, DataB64: img.DataB64, Bytes: img.Bytes})
				}
			}
			part.Output = strings.Join(texts, "\n")
		}
	}
	var persisted struct {
		Path string `json:"persistedOutputPath"`
	}
	if len(structured) > 0 && structured[0] == '{' && json.Unmarshal(structured, &persisted) == nil && persisted.Path != "" {
		root := filepath.Dir(idx.Files[0])
		if f, st, err := e.open(root, persisted.Path); err == nil {
			n := min(st.Size(), persistedLimit)
			part.Output = string(readHead(f, n))
			part.Truncated = st.Size() > n
			f.Close()
		}
	}
	return part
}
