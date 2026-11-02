package sessions

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// grokParser reads Grok CLI: ~/.grok/sessions/<percent-encoded cwd>/<session id>/, which holds
// chat_history.jsonl (the conversation) and summary.json (everything the listing needs), beside
// events, updates and rewind files that are not read.
//
// What the format makes a reader get right:
//
//   - chat_history.jsonl records carry no times at all. The session's times come from summary.json,
//     and the turns have none; a sub-agent therefore lands after the parent's last turn.
//   - The model's answer is several records: reasoning (and a server-side tool's backend_tool_call)
//     first, then the assistant record with the text and the tool calls. They are one turn. The
//     results of one batch of calls are consecutive tool_result records and are one turn too.
//   - A user record with a synthetic_reason is the program talking, not the operator: reminders,
//     project instructions, a finished task's notice. They are notes, not words. The one with the
//     reason compaction_meta carries the summary a compaction left behind.
//   - A sub-agent is a session of its own (session_kind subagent, subagent_fork or subagent_resume).
//     Forks and resumes name their parent in parent_session_id; a plain subagent names nothing, so
//     it can be neither nested nor shown beside the sessions the operator started.
type grokParser struct{}

func (grokParser) ID() string      { return "grok" }
func (grokParser) Name() string    { return "Grok CLI" }
func (grokParser) Procs() []string { return []string{"grok"} }

func (grokParser) Roots(e *Env) []string {
	if dir := e.getenv("GROK_HOME"); dir != "" {
		return []string{filepath.Join(e.expand(dir), "sessions")}
	}
	return []string{filepath.Join(e.Home, ".grok", "sessions")}
}

const (
	grokHistory = "chat_history.jsonl"
	grokSummary = "summary.json"
	// grokUnlinked is the parent of a sub-agent that does not say whose it is: no session has this
	// id, so the listing hides it without nesting it under anything.
	grokUnlinked = "(unlinked sub-agent)"
	// grokSummaryLimit bounds summary.json, which holds the agent's whole profile and is a few
	// kilobytes; anything much larger is not what it should be.
	grokSummaryLimit = 4 << 20
)

func (grokParser) Discover(e *Env, root string, emit func(Candidate)) error {
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
		sessions, err := os.ReadDir(folder)
		if err != nil {
			continue
		}
		for _, s := range sessions {
			if !s.IsDir() {
				continue
			}
			dir := filepath.Join(folder, s.Name())
			history, err := os.Lstat(filepath.Join(dir, grokHistory))
			if err != nil || !history.Mode().IsRegular() {
				continue
			}
			c := Candidate{ID: s.Name(), Path: filepath.Join(dir, grokHistory), Size: history.Size(), Mtime: history.ModTime()}
			if sum, err := os.Lstat(filepath.Join(dir, grokSummary)); err == nil && sum.Mode().IsRegular() {
				c.Size += sum.Size()
				if sum.ModTime().After(c.Mtime) {
					c.Mtime = sum.ModTime()
				}
			}
			emit(c)
		}
	}
	return nil
}

// grokSummaryDoc is the part of summary.json the parser reads.
type grokSummaryDoc struct {
	GeneratedTitle  string `json:"generated_title"`
	CreatedAt       string `json:"created_at"`
	UpdatedAt       string `json:"updated_at"`
	LastActiveAt    string `json:"last_active_at"`
	NumMessages     int    `json:"num_messages"`
	SessionKind     string `json:"session_kind"`
	ParentSessionID string `json:"parent_session_id"`
	CurrentModelID  string `json:"current_model_id"`
	HeadBranch      string `json:"head_branch"`
	AgentName       string `json:"agent_name"`
	Info            struct {
		Cwd string `json:"cwd"`
	} `json:"info"`
}

// readGrokSummary reads the summary.json beside a session's chat_history.jsonl; a missing or
// damaged one gives an empty summary, as the conversation can be read without it.
func readGrokSummary(e *Env, root, history string) grokSummaryDoc {
	var doc grokSummaryDoc
	f, st, err := e.open(root, filepath.Join(filepath.Dir(history), grokSummary))
	if err != nil {
		return doc
	}
	defer f.Close()
	if st.Size() > grokSummaryLimit {
		return doc
	}
	_ = json.Unmarshal(readHead(f, st.Size()), &doc)
	return doc
}

// cwd is the folder the session ran in: the program's own record of it, else the folder name,
// which is that path percent-encoded.
func (d grokSummaryDoc) cwd(history string) string {
	if d.Info.Cwd != "" {
		return d.Info.Cwd
	}
	name := filepath.Base(filepath.Dir(filepath.Dir(history)))
	if p, err := url.PathUnescape(name); err == nil {
		return p
	}
	return name
}

func (d grokSummaryDoc) isSubagent() bool {
	return d.ParentSessionID != "" || strings.HasPrefix(d.SessionKind, "subagent")
}

// parent is the session a sub-agent belongs to, empty for a session the operator started.
func (d grokSummaryDoc) parent() string {
	if d.ParentSessionID != "" {
		return d.ParentSessionID
	}
	if d.isSubagent() {
		return grokUnlinked
	}
	return ""
}

func (d grokSummaryDoc) header(c *Candidate, history string) Header {
	return Header{ID: c.ID, Cwd: d.cwd(history), Title: d.GeneratedTitle, StartedAt: Stamp{stamp(d.CreatedAt)},
		UpdatedAt: Stamp{stamp(first(d.LastActiveAt, d.UpdatedAt))}, Messages: d.NumMessages, Branch: d.HeadBranch,
		Model: d.CurrentModelID, parent: d.parent()}
}

// Peek reads only summary.json: the history of a session is megabytes, and the summary already
// has the folder, the title, the times and whose sub-agent the session is.
func (grokParser) Peek(e *Env, c *Candidate) (Header, error) {
	return readGrokSummary(e, c.Root, c.Path).header(c, c.Path), nil
}

// grokRec is what the index pass takes from one record without decoding its content.
type grokRec struct {
	off       int64
	n         int
	ord       int
	typ       string
	synthetic string
	model     string
	visible   bool
	text      string // the first request's words, for the title
}

var grokKeys = []string{"type", "synthetic_reason", "content", "model_id"}

func grokScan(ord int, off int64, raw []byte) grokRec {
	f := fields(raw, grokKeys...)
	r := grokRec{off: off, n: len(raw), ord: ord, typ: str(f[0]), synthetic: str(f[1]), model: str(f[3])}
	content := f[2]
	switch r.typ {
	case "user":
		if r.synthetic != "" {
			break
		}
		r.visible = bytes.Contains(content, []byte(`"type":"text"`)) || bytes.Contains(content, []byte(`"type":"image"`))
		if r.visible {
			r.text = grokUserText(content)
		}
	case "assistant":
		r.visible = strings.TrimSpace(strPrefix(content, 200)) != ""
	}
	return r
}

// grokUserText is the words of a user record's content, a list of blocks.
func grokUserText(content []byte) string {
	var blocks []struct {
		Type, Text string
	}
	if json.Unmarshal(content, &blocks) != nil {
		return ""
	}
	var texts []string
	for _, b := range blocks {
		if b.Type == "text" && strings.TrimSpace(b.Text) != "" {
			texts = append(texts, b.Text)
		}
	}
	return strings.Join(texts, "\n\n")
}

// grokAux is what Build needs that one record does not say.
type grokAux struct {
	compaction bool
	covers     *[2]int
	agent      map[string]any // the first turn of a sub-agent: which session it was
}

// grokStream passes over one history file and returns its records, with the damaged count.
func grokStream(e *Env, root, path string) (recs []grokRec, consumed int64, damaged int, err error) {
	f, _, err := e.open(root, path)
	if err != nil {
		return nil, 0, 0, err
	}
	defer f.Close()
	consumed, damaged, err = eachLine(f, 0, func(off int64, raw []byte) bool {
		recs = append(recs, grokScan(len(recs), off, raw))
		return true
	})
	return recs, consumed, damaged, err
}

// grokTurns groups records into turns and counts what the header counts.
func grokTurns(recs []grokRec, file int, sidechain string) (turns []TurnRef, messages, compacted int) {
	ext := func(r grokRec) string {
		if sidechain != "" {
			return sidechain + "/r" + strconv.Itoa(r.ord)
		}
		return "r" + strconv.Itoa(r.ord)
	}
	span := func(r grokRec) Span { return Span{file, r.off, r.n} }
	var pending []grokRec // reasoning and server-side calls waiting for the answer they belong to
	flush := func() {
		if len(pending) == 0 {
			return
		}
		ref := TurnRef{ExtID: ext(pending[0]), Role: RoleAssistant, Sidechain: sidechain}
		for _, p := range pending {
			ref.Spans = append(ref.Spans, span(p))
		}
		turns = append(turns, ref)
		pending = nil
	}
	for i := 0; i < len(recs); i++ {
		r := recs[i]
		switch r.typ {
		case "reasoning", "backend_tool_call":
			pending = append(pending, r)
		case "assistant":
			ref := TurnRef{ExtID: ext(pending0(pending, r)), Role: RoleAssistant, Model: r.model, Sidechain: sidechain}
			for _, p := range pending {
				ref.Spans = append(ref.Spans, span(p))
			}
			ref.Spans = append(ref.Spans, span(r))
			pending = nil
			if r.visible {
				messages++
			}
			turns = append(turns, ref)
		case "tool_result":
			flush()
			ref := TurnRef{ExtID: ext(r), Role: RoleUser, Sidechain: sidechain, Spans: []Span{span(r)}}
			for i+1 < len(recs) && recs[i+1].typ == "tool_result" {
				i++
				ref.Spans = append(ref.Spans, span(recs[i]))
			}
			turns = append(turns, ref)
		default:
			flush()
			ref := TurnRef{ExtID: ext(r), Role: RoleNote, Sidechain: sidechain, Spans: []Span{span(r)}}
			switch {
			case r.typ == "user" && r.synthetic == "compaction_meta":
				ref.Aux = &grokAux{compaction: true}
				compacted++
			case r.typ == "user" && r.synthetic == "":
				ref.Role = RoleUser
				if r.visible {
					messages++
				}
			}
			turns = append(turns, ref)
		}
	}
	flush()
	return turns, messages, compacted
}

// pending0 names a turn by its first record: the reasoning that opened it, else the answer itself.
func pending0(pending []grokRec, answer grokRec) grokRec {
	if len(pending) > 0 {
		return pending[0]
	}
	return answer
}

func (grokParser) Index(e *Env, c *Candidate, sidechains bool) (*Index, error) {
	recs, consumed, damaged, err := grokStream(e, c.Root, c.Path)
	if err != nil {
		return nil, err
	}
	sum := readGrokSummary(e, c.Root, c.Path)
	idx := &Index{Files: []string{c.Path}, Consumed: consumed, Damaged: damaged}
	idx.Files = append(idx.Files, c.Extra...)
	header := sum.header(c, c.Path)
	var model, firstAsk string
	for _, r := range recs {
		if r.typ == "assistant" && r.model != "" {
			model = r.model
		}
		if firstAsk == "" && r.text != "" {
			firstAsk = r.text
		}
	}
	turns, messages, compacted := grokTurns(recs, 0, "")
	header.Messages, header.Flags.Compacted = messages, compacted
	header.Model, header.firstPrompt = first(model, header.Model), firstAsk
	idx.Header = header
	idx.Turns = turns
	if sidechains {
		for k, child := range c.Children {
			// The numbering of files is fixed by the order here, so a child that cannot be read still
			// takes its place in the list.
			idx.Files = append(idx.Files, child.Path)
			file := 1 + len(c.Extra) + k
			crecs, _, cdamaged, err := grokStream(e, c.Root, child.Path)
			if err != nil {
				continue
			}
			idx.Damaged += cdamaged
			cturns, _, _ := grokTurns(crecs, file, child.ID)
			if len(cturns) == 0 {
				continue
			}
			csum := readGrokSummary(e, c.Root, child.Path)
			agent := map[string]any{"id": child.ID}
			if csum.AgentName != "" {
				agent["agent_name"] = csum.AgentName
			}
			if aux, ok := cturns[0].Aux.(*grokAux); ok {
				aux.agent = agent
			} else {
				cturns[0].Aux = &grokAux{agent: agent}
			}
			idx.Turns = grokPlace(idx.Turns, cturns, stamp(csum.CreatedAt))
		}
	}
	grokCovers(idx.Turns)
	return idx, nil
}

// grokPlace puts a sub-agent's turns after the last turn that is not later than its start. A start
// the program never wrote puts them at the end, as does a conversation without times, which is
// what chat_history.jsonl is: a sub-agent is then read after everything its parent said.
func grokPlace(turns, child []TurnRef, start time.Time) []TurnRef {
	pos := len(turns)
	if !start.IsZero() {
		pos = 0
		for i, t := range turns {
			if t.Sidechain == "" && !t.At.After(start) {
				pos = i + 1
			}
		}
	}
	out := make([]TurnRef, 0, len(turns)+len(child))
	out = append(out, turns[:pos]...)
	out = append(out, child...)
	return append(out, turns[pos:]...)
}

// grokCovers gives each compaction the range of turns since the previous one, by their place in the
// final order (sub-agents included).
func grokCovers(turns []TurnRef) {
	lastCut := 0
	for i := range turns {
		a, ok := turns[i].Aux.(*grokAux)
		if !ok || !a.compaction || turns[i].Sidechain != "" {
			continue
		}
		a.covers = nil
		if i-1 >= lastCut {
			a.covers = &[2]int{lastCut, i - 1}
		}
		lastCut = i + 1
	}
}

// grokRecord is one record, decoded for Build.
type grokRecord struct {
	Type          string          `json:"type"`
	Synthetic     string          `json:"synthetic_reason"`
	Content       json.RawMessage `json:"content"`
	ToolCalls     []grokToolCall  `json:"tool_calls"`
	ToolCallID    string          `json:"tool_call_id"`
	Images        []grokImage     `json:"images"`
	EncryptedBody string          `json:"encrypted_content"`
	Summary       []struct {
		Text string `json:"text"`
	} `json:"summary"`
	Kind json.RawMessage `json:"kind"`
}

type grokToolCall struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

type grokImage struct {
	Type string `json:"type"`
	URL  string `json:"url"`
}

// grokContentText is a content as plain words: a string, or the text blocks of a list.
func grokContentText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	if raw[0] == '"' {
		var s string
		_ = json.Unmarshal(raw, &s)
		return s
	}
	return grokUserText(raw)
}

// grokDataURL splits a data: URL into its type and base64 body; anything else is not inline.
func grokDataURL(u string) (mime, body string, ok bool) {
	rest, found := strings.CutPrefix(u, "data:")
	if !found {
		return "", "", false
	}
	head, body, found := strings.Cut(rest, ",")
	if !found || !strings.HasSuffix(head, ";base64") {
		return "", "", false
	}
	return strings.TrimSuffix(head, ";base64"), body, true
}

func grokImagePart(img grokImage) Part {
	if mime, body, ok := grokDataURL(img.URL); ok {
		return Part{Kind: KindImage, Mime: mime, DataB64: body, Bytes: int64(base64.StdEncoding.DecodedLen(len(body)))}
	}
	return Part{Kind: KindImage, Path: img.URL}
}

func (p grokParser) Build(e *Env, idx *Index, ref *TurnRef, records [][]byte) []Part {
	var parts []Part
	aux, _ := ref.Aux.(*grokAux)
	if aux != nil && aux.agent != nil {
		parts = append(parts, Meta("sidechain", aux.agent))
	}
	for _, raw := range records {
		var r grokRecord
		if json.Unmarshal(raw, &r) != nil {
			parts = append(parts, Meta("unreadable", map[string]any{"bytes": len(raw)}))
			continue
		}
		switch r.Type {
		case "system":
			parts = append(parts, Meta("system", map[string]any{"text": clip(grokContentText(r.Content), noteLimit)}))
		case "user":
			parts = append(parts, p.user(r, aux)...)
		case "reasoning":
			var texts []string
			for _, s := range r.Summary {
				if strings.TrimSpace(s.Text) != "" {
					texts = append(texts, s.Text)
				}
			}
			if len(texts) > 0 {
				parts = append(parts, Thinking(strings.Join(texts, "\n\n"), false))
			} else if r.EncryptedBody != "" {
				parts = append(parts, Thinking("", true))
			}
		case "assistant":
			if text := grokContentText(r.Content); strings.TrimSpace(text) != "" {
				parts = append(parts, Text(text))
			}
			for _, tc := range r.ToolCalls {
				input := json.RawMessage(tc.Arguments)
				if !json.Valid(input) {
					input, _ = json.Marshal(map[string]string{"raw": tc.Arguments})
				}
				parts = append(parts, ToolCall(tc.ID, tc.Name, input, ToolNative, ""))
			}
		case "tool_result":
			result := ToolResult(r.ToolCallID, grokContentText(r.Content), false)
			for _, img := range r.Images {
				if part := grokImagePart(img); part.DataB64 != "" {
					result.Images = append(result.Images, Image{Mime: part.Mime, DataB64: part.DataB64, Bytes: part.Bytes})
				}
			}
			parts = append(parts, result)
		case "backend_tool_call":
			parts = append(parts, Meta("backend_tool_call", map[string]any{"text": clip(string(r.Kind), noteLimit)}))
		default:
			parts = append(parts, Meta("record:"+r.Type, map[string]any{"text": clip(string(raw), noteLimit)}))
		}
	}
	return parts
}

// user makes the parts of a user record: the operator's words and pictures, or, when the program
// wrote it, a note (or the compaction summary).
func (p grokParser) user(r grokRecord, aux *grokAux) []Part {
	if r.Synthetic == "compaction_meta" {
		var covers *[2]int
		if aux != nil {
			covers = aux.covers
		}
		return []Part{Compaction(grokContentText(r.Content), covers, false)}
	}
	if r.Synthetic != "" {
		return []Part{Meta("synthetic:"+r.Synthetic, map[string]any{"text": clip(grokContentText(r.Content), noteLimit)})}
	}
	var parts []Part
	if text := grokContentText(r.Content); strings.TrimSpace(text) != "" {
		parts = append(parts, Text(text))
	}
	var blocks []grokImage
	if len(r.Content) > 0 && r.Content[0] == '[' && json.Unmarshal(r.Content, &blocks) == nil {
		for _, b := range blocks {
			if b.URL != "" {
				parts = append(parts, grokImagePart(b))
			}
		}
	}
	return parts
}
