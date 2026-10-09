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
	"sync"
	"time"
)

// codexParser reads the Codex CLI: <codex home>/sessions/YYYY/MM/DD/rollout-<time>-<uuid>.jsonl, and
// the rollouts the program moved to <codex home>/archived_sessions.
//
// What the format makes a reader get right:
//
//   - The first record is session_meta, 20 to 45 KB because it carries the program's base
//     instructions; the listing needs it and nothing else of the head.
//   - Reasoning is always encrypted. Only an open summary, when the model wrote one, carries over.
//   - A tool call is written two ways. Older versions and the plain tools use response_item
//     function_call with its function_call_output. "Code mode" calls everything through one
//     custom_tool_call named exec whose input is JavaScript, which says nothing about what was done;
//     the meaning is in the event_msg item_completed records beside it (CommandExecution,
//     FileChange, McpToolCall…). Where an item has the id of a call already seen, it repeats that
//     call and is dropped.
//   - One item record is both a call and its result, which belong in two different turns (the model
//     says the call, the tool answers). The record is named by both turns and Aux tells Build which
//     half each turn takes from it.
//   - A sub-agent is a rollout of its own whose session_meta names the thread that spawned it. It
//     is listed inside its parent, never beside it. A fork (forked_from_id) is an independent session.
//   - A compacted record keeps the history the program replaced; its summary is encrypted, so the
//     compaction part usually has no text.
//   - Compressed rollouts (.jsonl.zst) are not read in this build: there is no decompressor in the
//     module yet, and they are the oldest, least useful ones. They are skipped, not failed.
type codexParser struct{}

func (codexParser) ID() string      { return "codex" }
func (codexParser) Name() string    { return "Codex" }
func (codexParser) Procs() []string { return []string{"codex"} }

// codexHome follows the program's own override variable.
func codexHome(e *Env) string {
	if dir := e.getenv("CODEX_HOME"); dir != "" {
		return e.expand(dir)
	}
	return filepath.Join(e.Home, ".codex")
}

func (codexParser) Roots(e *Env) []string {
	home := codexHome(e)
	return []string{filepath.Join(home, "sessions"), filepath.Join(home, "archived_sessions")}
}

// codexDepth is how deep a root is searched: sessions/YYYY/MM/DD/ is three folders down. The
// archive is flat but may keep the dated layout.
const codexDepth = 3

func (codexParser) Discover(e *Env, root string, emit func(Candidate)) error {
	return codexWalk(root, 0, emit)
}

func codexWalk(dir string, depth int, emit func(Candidate)) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, d := range entries {
		name := d.Name()
		switch {
		// A link is never followed or listed: the store is the program's, a link is someone's.
		case d.IsDir():
			if depth < codexDepth {
				_ = codexWalk(filepath.Join(dir, name), depth+1, emit)
			}
		case d.Type().IsRegular() && strings.HasPrefix(name, "rollout-") && strings.HasSuffix(name, ".jsonl"):
			info, err := d.Info()
			if err != nil {
				continue
			}
			id := strings.TrimSuffix(name, ".jsonl")
			if len(id) > 36 {
				id = id[len(id)-36:]
			}
			emit(Candidate{ID: id, Path: filepath.Join(dir, name), Size: info.Size(), Mtime: info.ModTime()})
		}
	}
	return nil
}

// codexNoise are the starts of messages the CLI wrote into the conversation for its model: the
// environment, the instructions files, permission text, the notice that a sub-agent finished.
// They become notes, not words.
var codexNoise = []string{"<environment_context", "<recommended_plugins", "<user_instructions", "# AGENTS.md",
	"<skills_instructions", "<permissions", "<turn_aborted", "<user_action", "<subagent_notification",
	"<codex_internal_context", "<collaboration_mode", "<multi_agent", "<model_switch", "<apps_instructions",
	"<codex_apps", "<external_codex_apps", "<skill>", "<plugins_instructions"}

func codexIsNoise(text string) bool {
	t := strings.TrimLeft(text, " \n\t")
	for _, n := range codexNoise {
		if strings.HasPrefix(t, n) {
			return true
		}
	}
	return false
}

// codexIsImageTag is the wrapper Codex puts around a pasted picture; the picture itself is the
// input_image beside it, so the wrapper says nothing.
func codexIsImageTag(text string) bool {
	t := strings.TrimLeft(text, " \n\t")
	return strings.HasPrefix(t, "<image") || strings.HasPrefix(t, "</image")
}

// codexContentItem is one entry of a message's content.
type codexContentItem struct {
	Type     string `json:"type"`
	Text     string `json:"text"`
	ImageURL string `json:"image_url"`
}

func codexContent(raw []byte) []codexContentItem {
	if len(raw) == 0 {
		return nil
	}
	if raw[0] == '"' {
		return []codexContentItem{{Type: "input_text", Text: str(raw)}}
	}
	var items []codexContentItem
	_ = json.Unmarshal(raw, &items)
	return items
}

// codexUserWords is what an operator-side message says: its text, whether the CLI wrote it, and how
// many pictures it carries.
func codexUserWords(content []byte) (text string, noise bool, images int) {
	var texts []string
	first := true
	for _, it := range codexContent(content) {
		switch it.Type {
		case "input_image":
			images++
		case "input_text", "text":
			if codexIsImageTag(it.Text) {
				continue
			}
			if first && strings.TrimSpace(it.Text) != "" {
				noise, first = codexIsNoise(it.Text), false
			}
			texts = append(texts, it.Text)
		}
	}
	return strings.Join(texts, "\n\n"), noise, images
}

// codexHead collects the header facts a pass meets.
type codexHead struct {
	id, cwd, branch, model, version   string
	parent, agentPath, nickname, role string
	started, updated                  time.Time
	firstPrompt                       string
	sawMeta                           bool
}

func (h *codexHead) meta(payload []byte, at time.Time) {
	m := fields(payload, "id", "timestamp", "cwd", "cli_version", "source", "git", "parent_thread_id",
		"agent_nickname", "agent_role", "agent_path", "history_base")
	h.sawMeta = true
	h.id, h.cwd, h.version = str(m[0]), str(m[2]), str(m[3])
	if len(m[10]) > 0 && m[10][0] == '{' {
		// A page of a paginated thread (rollout-…-<thread>_<page>.jsonl) carries the thread's id,
		// as the thread's first file does. Under that id a read found whichever file came first, and
		// a 58 MB page was listed while its 33-turn first file was imported. Until pages are spliced,
		// each is a session of its own, named by its file.
		h.id = ""
	}
	if t := stamp(str(m[1])); !t.IsZero() {
		h.started = t
	} else {
		h.started = at
	}
	if g := fields(m[5], "branch"); g[0] != nil {
		h.branch = str(g[0])
	}
	h.parent, h.nickname, h.role, h.agentPath = str(m[6]), str(m[7]), str(m[8]), str(m[9])
	// A spawned thread names its parent inside source; older rollouts also wrote it at the top.
	if len(m[4]) > 0 && m[4][0] == '{' {
		if sub := fields(m[4], "subagent"); len(sub[0]) > 0 && sub[0][0] == '{' {
			if sp := fields(sub[0], "thread_spawn"); len(sp[0]) > 0 && sp[0][0] == '{' {
				t := fields(sp[0], "parent_thread_id", "agent_path", "agent_nickname", "agent_role")
				h.parent = first(str(t[0]), h.parent)
				h.agentPath = first(str(t[1]), h.agentPath)
				h.nickname = first(str(t[2]), h.nickname)
				h.role = first(str(t[3]), h.role)
			}
		}
	}
}

func (h *codexHead) seen(at time.Time) {
	if at.After(h.updated) {
		h.updated = at
	}
}

// scanForPeek reads one record of a head or a tail window.
func (h *codexHead) scanForPeek(raw []byte, wantPrompt bool) {
	f := fields(raw, "timestamp", "type", "payload")
	at := stamp(str(f[0]))
	h.seen(at)
	switch str(f[1]) {
	case "session_meta":
		if wantPrompt { // only the head carries it
			h.meta(f[2], at)
		}
	case "turn_context":
		if m := str(fields(f[2], "model")[0]); m != "" {
			h.model = m
		}
	case "response_item":
		if !wantPrompt || h.firstPrompt != "" {
			return
		}
		m := fields(f[2], "type", "role", "content")
		if str(m[0]) == "message" && str(m[1]) == "user" {
			if text, noise, _ := codexUserWords(m[2]); !noise && strings.TrimSpace(text) != "" {
				h.firstPrompt = text
			}
		}
	}
}

// The windows Peek reads: session_meta alone is up to 45 KB, so the head starts large and grows
// when the first record is longer still.
const (
	codexHeadWindow = 128 << 10
	codexHeadMax    = 2 << 20
	codexTailWindow = 64 << 10
)

func (codexParser) Peek(e *Env, c *Candidate) (Header, error) {
	f, st, err := e.open(c.Root, c.Path)
	if err != nil {
		return Header{}, err
	}
	defer f.Close()
	size := st.Size()
	head := readHead(f, min(size, codexHeadWindow))
	for bytes.IndexByte(head, '\n') < 0 && int64(len(head)) < size && len(head) < codexHeadMax {
		head = readHead(f, min(size, int64(len(head))*4, codexHeadMax))
	}
	whole := int64(len(head)) >= size
	var h codexHead
	for _, l := range lines(head, whole) {
		h.scanForPeek(l, true)
	}
	if !h.sawMeta {
		return Header{}, ErrInvalid
	}
	if !whole {
		// The last record's time is the session's: the head's times are its beginning. A tail that
		// holds no whole record (one huge output) gives none, and the file's mtime stands in.
		h.updated = time.Time{}
		for _, l := range lines(readTail(f, size, codexTailWindow), true) {
			h.scanForPeek(l, false)
		}
	}
	title := codexTitle(e, h.id)
	return h.header(c, title), nil
}

func (h *codexHead) header(c *Candidate, title string) Header {
	return Header{ID: first(h.id, c.ID), Cwd: h.cwd, Title: title, StartedAt: Stamp{h.started}, UpdatedAt: Stamp{h.updated},
		Branch: h.branch, Model: h.model, firstPrompt: h.firstPrompt, parent: h.parent, version: h.version}
}

// codexTitles is the program's own thread names, session_index.jsonl, read again only when the file
// changes: a scan peeks at thousands of rollouts and each would otherwise read the same index.
var codexTitles struct {
	mu      sync.Mutex
	entries map[string]codexTitleEntry
}

type codexTitleEntry struct {
	size   int64
	mtime  time.Time
	titles map[string]string
}

// codexTitle is the thread name the program gave a session, or "". The last line of an id wins:
// renaming appends.
func codexTitle(e *Env, id string) string {
	if id == "" {
		return ""
	}
	home := codexHome(e)
	path := filepath.Join(home, "session_index.jsonl")
	f, st, err := e.open(home, path)
	if err != nil {
		return ""
	}
	defer f.Close()
	codexTitles.mu.Lock()
	defer codexTitles.mu.Unlock()
	if ent, ok := codexTitles.entries[path]; ok && ent.size == st.Size() && ent.mtime.Equal(st.ModTime()) {
		return ent.titles[id]
	}
	titles := map[string]string{}
	_, _, _ = eachLine(f, 0, func(_ int64, raw []byte) bool {
		m := fields(raw, "id", "thread_name")
		if key, name := str(m[0]), str(m[1]); key != "" && name != "" {
			titles[key] = name
		}
		return true
	})
	if codexTitles.entries == nil {
		codexTitles.entries = map[string]codexTitleEntry{}
	}
	codexTitles.entries[path] = codexTitleEntry{st.Size(), st.ModTime(), titles}
	return titles[id]
}

// codexAux is what Build needs that a record alone does not say.
type codexAux struct {
	// modes is one byte per span: 0 the whole record, 'c' the call half of an item, 'r' its result
	// half, 'm' an output kept as a note.
	modes      []byte
	compaction bool
	covers     *[2]int
	sidechain  map[string]any // the first turn of a sub-agent: who it was and which call started it
}

func (a *codexAux) mode(i int) byte {
	if a == nil || i >= len(a.modes) {
		return 0
	}
	return a.modes[i]
}

const (
	codexUser      = 'u'
	codexAssistant = 'a'
	codexResult    = 'r'
	codexNote      = 'n'
	codexNeutral   = 'x' // a note that joins the turn open, whichever it is
)

// codexPass is one streaming pass over a rollout.
type codexPass struct {
	file      int
	sidechain string
	isChild   bool

	head       codexHead
	model      string
	turns      []TurnRef
	openKind   byte // the kind of the last turn while more of it may follow, else 0
	messages   int
	textTurn   int
	compacted  int
	damaged    int
	callIDs    map[string]bool
	execCalls  map[string]bool
	spawnCalls map[string]bool
	childIDs   []string
	spawnedBy  map[string]string // a child's id → the spawn call that started it
}

func newCodexPass(file int, sidechain string) *codexPass {
	return &codexPass{file: file, sidechain: sidechain, textTurn: -1, callIDs: map[string]bool{}, execCalls: map[string]bool{},
		spawnCalls: map[string]bool{}, spawnedBy: map[string]string{}}
}

// add puts one record into the turns. A user message and a note are turns of their own; the parts
// the model says and the results of its tools each continue the turn of their kind.
func (p *codexPass) add(kind byte, off int64, n int, at time.Time, id string, mode byte) {
	sp := Span{p.file, off, n}
	if kind == codexNeutral {
		if p.openKind == 0 {
			kind = codexNote
		} else {
			p.appendSpan(&p.turns[len(p.turns)-1], sp, mode)
			return
		}
	}
	if (kind == codexAssistant || kind == codexResult) && p.openKind == kind {
		p.appendSpan(&p.turns[len(p.turns)-1], sp, mode)
		return
	}
	if id == "" {
		id = "line:" + itoa(off)
	} else if kind == codexResult {
		id += ":result"
	}
	t := TurnRef{ExtID: id, Model: p.model, At: at, Sidechain: p.sidechain}
	switch kind {
	case codexUser:
		t.Role = RoleUser
	case codexAssistant:
		t.Role = RoleAssistant
	case codexResult:
		t.Role = RoleUser // the shape the host converts from: results come back as a user message
	default:
		t.Role = RoleNote
	}
	p.appendSpan(&t, sp, mode)
	p.turns = append(p.turns, t)
	if kind == codexAssistant || kind == codexResult {
		p.openKind = kind
	} else {
		p.openKind = 0
	}
}

func (p *codexPass) appendSpan(t *TurnRef, sp Span, mode byte) {
	aux, _ := t.Aux.(*codexAux)
	if mode != 0 && aux == nil {
		aux = &codexAux{modes: make([]byte, len(t.Spans))}
		t.Aux = aux
	}
	if aux != nil {
		for len(aux.modes) < len(t.Spans) {
			aux.modes = append(aux.modes, 0)
		}
		aux.modes = append(aux.modes, mode)
	}
	t.Spans = append(t.Spans, sp)
}

// line reads one record.
func (p *codexPass) line(off int64, raw []byte) bool {
	f := fields(raw, "timestamp", "type", "payload")
	at := stamp(str(f[0]))
	p.head.seen(at)
	n := len(raw)
	if f[1] == nil {
		// Braces without a type: a line torn by a crash or a writer that was cut off.
		p.damaged++
		return true
	}
	switch typ := str(f[1]); typ {
	case "session_meta":
		if !p.head.sawMeta {
			p.head.meta(f[2], at)
			if p.isChild && p.head.id != "" {
				p.sidechain = p.head.id
			}
		}
	case "turn_context":
		if m := str(fields(f[2], "model")[0]); m != "" {
			p.model, p.head.model = m, m
		}
	case "world_state", "token_usage_record", "inter_agent_communication_metadata":
	case "compacted":
		p.compacted++
		p.add(codexNote, off, n, at, "", 0)
		t := &p.turns[len(p.turns)-1]
		aux, _ := t.Aux.(*codexAux)
		if aux == nil {
			aux = &codexAux{}
			t.Aux = aux
		}
		aux.compaction = true
	case "response_item":
		p.responseItem(off, n, at, raw, f[2])
	case "event_msg":
		p.event(off, n, at, f[2])
	default:
		p.add(codexNote, off, n, at, "", 0)
	}
	return true
}

func (p *codexPass) responseItem(off int64, n int, at time.Time, raw, payload []byte) {
	m := fields(payload, "type", "role", "call_id", "id", "name", "content", "summary", "encrypted_content")
	id := str(m[3])
	call := str(m[2])
	switch typ := str(m[0]); typ {
	case "message":
		switch role := str(m[1]); role {
		case "assistant":
			p.add(codexAssistant, off, n, at, id, 0)
			if p.textTurn != len(p.turns)-1 {
				p.textTurn = len(p.turns) - 1
				if !p.isChild {
					p.messages++
				}
			}
		case "user":
			text, noise, images := codexUserWords(m[5])
			if noise || (strings.TrimSpace(text) == "" && images == 0) {
				p.add(codexNote, off, n, at, id, 0)
				return
			}
			p.add(codexUser, off, n, at, id, 0)
			if !p.isChild {
				p.messages++
			}
			if p.head.firstPrompt == "" && strings.TrimSpace(text) != "" {
				p.head.firstPrompt = text
			}
		default:
			p.add(codexNote, off, n, at, id, 0)
		}
	case "reasoning":
		hasSummary := len(m[6]) > 2 && string(m[6]) != "null"
		if hasSummary || str(m[7]) != "" {
			p.add(codexAssistant, off, n, at, id, 0)
		}
	case "function_call":
		p.callIDs[call] = true
		if str(m[4]) == "spawn_agent" {
			p.spawnCalls[call] = true
		}
		p.add(codexAssistant, off, n, at, "", 0)
	case "custom_tool_call":
		p.callIDs[call] = true
		if str(m[4]) == "exec" {
			p.execCalls[call] = true
		}
		p.add(codexAssistant, off, n, at, "", 0)
	case "local_shell_call", "tool_search_call":
		p.callIDs[call] = true
		p.add(codexAssistant, off, n, at, "", 0)
	case "web_search_call":
		p.add(codexAssistant, off, n, at, "", 'c')
		p.add(codexResult, off, n, at, "", 'r')
	case "function_call_output":
		if p.spawnCalls[call] {
			for _, child := range p.childIDs {
				if bytes.Contains(raw, []byte(child)) {
					p.spawnedBy[child] = call
				}
			}
		}
		p.add(codexResult, off, n, at, "", 0)
	case "custom_tool_call_output":
		if p.execCalls[call] {
			p.add(codexNeutral, off, n, at, "", 'm')
		} else {
			p.add(codexResult, off, n, at, "", 0)
		}
	case "tool_search_output":
		p.add(codexResult, off, n, at, "", 0)
	default:
		p.add(codexNote, off, n, at, id, 0)
	}
}

func (p *codexPass) event(off int64, n int, at time.Time, payload []byte) {
	m := fields(payload, "type", "item")
	switch str(m[0]) {
	case "turn_aborted":
		p.add(codexNote, off, n, at, "", 0)
	case "item_completed":
		it := fields(m[1], "type", "id")
		typ, id := str(it[0]), str(it[1])
		// An item with the id of a call already seen repeats that call and its output.
		if id != "" && p.callIDs[id] {
			return
		}
		switch typ {
		case "Reasoning", "AgentMessage", "UserMessage", "ContextCompaction":
		case "CommandExecution", "FileChange", "McpToolCall", "WebSearch", "ImageView", "Plan", "Extension", "CollabAgentToolCall":
			p.add(codexAssistant, off, n, at, id, 'c')
			p.add(codexResult, off, n, at, id, 'r')
		default: // SubAgentActivity, the review modes and what a later version adds
			p.add(codexNeutral, off, n, at, id, 0)
		}
	}
}

func (codexParser) Index(e *Env, c *Candidate, sidechains bool) (*Index, error) {
	f, _, err := e.open(c.Root, c.Path)
	if err != nil {
		return nil, err
	}
	main := newCodexPass(0, "")
	if sidechains {
		for _, ch := range c.Children {
			main.childIDs = append(main.childIDs, ch.ID)
		}
	}
	consumed, damaged, err := eachLine(f, 0, main.line)
	f.Close()
	if err != nil {
		return nil, err
	}
	idx := &Index{Files: []string{c.Path}, Consumed: consumed, Damaged: damaged + main.damaged}
	header := main.head.header(c, codexTitle(e, main.head.id))
	header.Messages, header.Flags.Compacted = main.messages, main.compacted
	idx.Header = header
	idx.Turns = main.turns
	if sidechains && len(c.Children) > 0 {
		idx.Turns = codexWithChildren(e, c, idx, main)
	}
	codexCovers(idx.Turns)
	return idx, nil
}

// codexWithChildren indexes each sub-agent rollout and places its turns after the last turn of the
// parent that is not later than the sub-agent's first.
func codexWithChildren(e *Env, c *Candidate, idx *Index, main *codexPass) []TurnRef {
	type placed struct {
		turns []TurnRef
		first time.Time
		pos   int
	}
	var kids []placed
	for k, ch := range c.Children {
		file := 1 + len(c.Extra) + k
		idx.Files = append(idx.Files, ch.Path) // the file's number must hold even when it cannot be read
		cf, _, err := e.open(ch.Root, ch.Path)
		if err != nil {
			continue
		}
		pass := newCodexPass(file, ch.ID)
		pass.isChild = true
		_, damaged, _ := eachLine(cf, 0, pass.line)
		cf.Close()
		idx.Damaged += damaged + pass.damaged
		if len(pass.turns) == 0 {
			continue
		}
		data := map[string]any{"id": pass.sidechain, "agent_path": pass.head.agentPath, "agent_nickname": pass.head.nickname,
			"agent_role": pass.head.role}
		if call := main.spawnedBy[ch.ID]; call != "" {
			data["parent_call"] = call
		}
		first := &pass.turns[0]
		aux, _ := first.Aux.(*codexAux)
		if aux == nil {
			aux = &codexAux{}
			first.Aux = aux
		}
		aux.sidechain = data
		kids = append(kids, placed{turns: pass.turns, first: first.At})
	}
	sort.SliceStable(kids, func(i, j int) bool { return kids[i].first.Before(kids[j].first) })
	for i := range kids {
		for t, ref := range main.turns {
			if !ref.At.IsZero() && !ref.At.After(kids[i].first) {
				kids[i].pos = t + 1
			}
		}
	}
	var out []TurnRef
	next := 0
	emit := func(upTo int) {
		for ; next < len(kids) && kids[next].pos <= upTo; next++ {
			out = append(out, kids[next].turns...)
		}
	}
	emit(0)
	for t, ref := range main.turns {
		out = append(out, ref)
		emit(t + 1)
	}
	emit(len(main.turns))
	// A sub-agent placed before a later one's position still comes out in order of position.
	for ; next < len(kids); next++ {
		out = append(out, kids[next].turns...)
	}
	idx.Header.Flags.Sidechains = len(kids)
	return out
}

// codexCovers gives each compaction the turns since the previous one, by their place in the final
// order; sub-agents' turns are not the parent's history and do not move the cut.
func codexCovers(turns []TurnRef) {
	lastCut := 0
	for i := range turns {
		a, ok := turns[i].Aux.(*codexAux)
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

// codexRecord is one record, decoded for Build.
type codexRecord struct {
	Type    string          `json:"type"`
	Payload json.RawMessage `json:"payload"`
}

type codexPayload struct {
	Type             string                  `json:"type"`
	Role             string                  `json:"role"`
	Name             string                  `json:"name"`
	Namespace        string                  `json:"namespace"`
	CallID           string                  `json:"call_id"`
	ID               string                  `json:"id"`
	Content          json.RawMessage         `json:"content"`
	Summary          []struct{ Text string } `json:"summary"`
	EncryptedContent string                  `json:"encrypted_content"`
	Arguments        json.RawMessage         `json:"arguments"`
	Input            json.RawMessage         `json:"input"`
	Output           json.RawMessage         `json:"output"`
	Action           json.RawMessage         `json:"action"`
	Tools            []struct {
		Name string `json:"name"`
	} `json:"tools"`
	Message string          `json:"message"`
	Reason  string          `json:"reason"`
	Item    json.RawMessage `json:"item"`
	Author  string          `json:"author"`
}

func (p codexParser) Build(e *Env, idx *Index, ref *TurnRef, records [][]byte) []Part {
	var parts []Part
	aux, _ := ref.Aux.(*codexAux)
	if aux != nil && aux.sidechain != nil {
		parts = append(parts, Meta("sidechain", aux.sidechain))
	}
	for i, raw := range records {
		var r codexRecord
		if json.Unmarshal(raw, &r) != nil {
			parts = append(parts, Meta("unreadable", map[string]any{"bytes": len(raw)}))
			continue
		}
		mode := aux.mode(i)
		switch r.Type {
		case "compacted":
			var pl codexPayload
			_ = json.Unmarshal(r.Payload, &pl)
			var covers *[2]int
			if aux != nil {
				covers = aux.covers
			}
			parts = append(parts, Compaction(pl.Message, covers, true))
		case "response_item":
			var pl codexPayload
			if json.Unmarshal(r.Payload, &pl) != nil {
				parts = append(parts, Meta("unreadable", map[string]any{"bytes": len(raw)}))
				continue
			}
			parts = append(parts, p.responseItem(pl, mode)...)
		case "event_msg":
			var pl codexPayload
			_ = json.Unmarshal(r.Payload, &pl)
			switch pl.Type {
			case "turn_aborted":
				parts = append(parts, Meta("turn_aborted", map[string]any{"reason": pl.Reason}))
			case "item_completed":
				parts = append(parts, codexItem(pl.Item, mode)...)
			}
		default:
			parts = append(parts, Meta(r.Type, nil))
		}
	}
	return parts
}

func (codexParser) responseItem(pl codexPayload, mode byte) []Part {
	switch pl.Type {
	case "message":
		return codexMessage(pl)
	case "reasoning":
		var texts []string
		for _, s := range pl.Summary {
			if strings.TrimSpace(s.Text) != "" {
				texts = append(texts, s.Text)
			}
		}
		switch {
		case len(texts) > 0:
			return []Part{Thinking(strings.Join(texts, "\n\n"), false)}
		case pl.EncryptedContent != "":
			return []Part{Thinking("", true)}
		}
		return nil
	case "function_call":
		kind, server := codexToolKind(pl.Name, pl.Namespace)
		return []Part{ToolCall(pl.CallID, pl.Name, codexArguments(pl.Arguments), kind, server)}
	case "custom_tool_call":
		input := codexString(pl.Input)
		if pl.Name == "exec" {
			// JavaScript that calls the tools: the calls themselves come from the items beside it.
			return []Part{Meta("codex_exec", map[string]any{"call_id": pl.CallID, "code": clip(input, noteLimit)})}
		}
		in, _ := json.Marshal(map[string]string{"input": input})
		kind, server := codexToolKind(pl.Name, pl.Namespace)
		return []Part{ToolCall(pl.CallID, pl.Name, in, kind, server)}
	case "local_shell_call":
		return []Part{ToolCall(first(pl.CallID, pl.ID), "local_shell", codexObject(pl.Action), ToolNative, "")}
	case "web_search_call":
		id := "web_search:" + first(pl.ID, "call")
		if mode == 'r' {
			return []Part{ToolResult(id, "", false)}
		}
		return []Part{ToolCall(id, "web_search", codexObject(pl.Action), ToolCustom, "")}
	case "tool_search_call":
		return []Part{ToolCall(pl.CallID, "tool_search", codexArguments(pl.Arguments), ToolCustom, "")}
	case "tool_search_output":
		var names []string
		for _, t := range pl.Tools {
			names = append(names, t.Name)
		}
		return []Part{ToolResult(pl.CallID, clip(strings.Join(names, ", "), noteLimit), false)}
	case "function_call_output", "custom_tool_call_output":
		text, isErr, images := codexOutput(pl.Output)
		if mode == 'm' {
			return []Part{Meta("codex_exec_output", map[string]any{"call_id": pl.CallID, "text": clip(text, noteLimit)})}
		}
		part := ToolResult(pl.CallID, text, isErr)
		part.Images = images
		return []Part{part}
	}
	return []Part{Meta("response_item:"+pl.Type, nil)}
}

// codexMessage is a message by role.
func codexMessage(pl codexPayload) []Part {
	items := codexContent(pl.Content)
	var parts []Part
	switch pl.Role {
	case "assistant":
		var texts []string
		for _, it := range items {
			if it.Text != "" {
				texts = append(texts, it.Text)
			}
		}
		if text := strings.Join(texts, "\n\n"); strings.TrimSpace(text) != "" {
			parts = append(parts, Text(text))
		}
	case "user":
		text, noise, _ := codexUserWords(pl.Content)
		if noise {
			return []Part{Meta("cli", map[string]any{"text": clip(text, noteLimit)})}
		}
		if strings.TrimSpace(text) != "" {
			parts = append(parts, Text(text))
		}
		for _, it := range items {
			if it.Type == "input_image" {
				parts = append(parts, codexImage(it.ImageURL))
			}
		}
	default:
		var texts []string
		for _, it := range items {
			texts = append(texts, it.Text)
		}
		parts = append(parts, Meta(pl.Role, map[string]any{"text": clip(strings.Join(texts, "\n\n"), noteLimit)}))
	}
	return parts
}

// codexImage reads a picture the program kept as a data: address; any other address stays a path.
func codexImage(url string) Part {
	img := Part{Kind: KindImage}
	if rest, ok := strings.CutPrefix(url, "data:"); ok {
		head, data, found := strings.Cut(rest, ",")
		if found {
			img.Mime, _, _ = strings.Cut(head, ";")
			img.DataB64 = data
			img.Bytes = int64(base64.StdEncoding.DecodedLen(len(data)))
			return img
		}
	}
	img.Path = url
	return img
}

// codexString is a JSON string value, or "" when it is anything else.
func codexString(raw json.RawMessage) string {
	if len(raw) == 0 || raw[0] != '"' {
		return ""
	}
	var s string
	_ = json.Unmarshal(raw, &s)
	return s
}

// codexObject is an object kept as it is, and {} for anything else.
func codexObject(raw json.RawMessage) json.RawMessage {
	if len(raw) > 0 && raw[0] == '{' {
		return raw
	}
	return json.RawMessage("{}")
}

// codexArguments is the arguments of a call. Codex writes them as a string holding JSON; one that
// is not an object (a truncated stream, a plain string) is kept whole under "raw" rather than lost.
func codexArguments(raw json.RawMessage) json.RawMessage {
	if len(raw) > 0 && raw[0] == '{' {
		return raw
	}
	s := strings.TrimSpace(codexString(raw))
	if s == "" {
		return json.RawMessage("{}")
	}
	if s[0] == '{' && json.Valid([]byte(s)) {
		return json.RawMessage(s)
	}
	wrapped, _ := json.Marshal(map[string]string{"raw": s})
	return wrapped
}

func codexToolKind(name, namespace string) (kind, server string) {
	if rest, ok := strings.CutPrefix(name, "mcp__"); ok {
		server, _, _ = strings.Cut(rest, "__")
		return ToolMCP, server
	}
	if namespace != "" {
		return ToolCustom, namespace
	}
	return ToolNative, ""
}

var codexExitLine = regexp.MustCompile(`(?m)^Process exited with code (-?\d+)\s*$`)

// codexOutput is a tool's output: a string (older versions wrap it as {"output","metadata"} with
// the exit code; newer ones print "Process exited with code N" in a header line) or a list of
// text and picture items.
func codexOutput(raw json.RawMessage) (text string, isErr bool, images []Image) {
	if len(raw) == 0 {
		return "", false, nil
	}
	if raw[0] == '"' {
		s := codexString(raw)
		if strings.HasPrefix(s, "{") {
			var wrapped struct {
				Output   *string `json:"output"`
				Metadata struct {
					ExitCode *int `json:"exit_code"`
				} `json:"metadata"`
			}
			if json.Unmarshal([]byte(s), &wrapped) == nil && wrapped.Output != nil {
				return *wrapped.Output, wrapped.Metadata.ExitCode != nil && *wrapped.Metadata.ExitCode != 0, nil
			}
		}
		if m := codexExitLine.FindStringSubmatch(clip(s, 600)); m != nil {
			isErr = m[1] != "0"
		}
		return s, isErr, nil
	}
	var texts []string
	for _, it := range codexContent(raw) {
		switch it.Type {
		case "input_image":
			img := codexImage(it.ImageURL)
			images = append(images, Image{Mime: img.Mime, DataB64: img.DataB64, Bytes: img.Bytes})
		default:
			if it.Text != "" {
				texts = append(texts, it.Text)
			}
		}
	}
	return strings.Join(texts, "\n"), false, images
}

// codexMetaData keeps a small record whole and cuts a large one to text.
func codexMetaData(raw []byte) any {
	if len(raw) <= noteLimit {
		return json.RawMessage(raw)
	}
	return map[string]any{"text": clip(string(raw), noteLimit)}
}

// codexShellText is the command line an argv stands for, without the shell wrapper Codex puts
// around it ("bash -lc '…'"): the script is what was run.
func codexShellText(argv []string) string {
	if len(argv) == 3 {
		switch filepath.Base(argv[0]) {
		case "bash", "sh", "zsh":
			if argv[1] == "-lc" || argv[1] == "-c" || argv[1] == "-ic" {
				return argv[2]
			}
		}
	}
	return strings.Join(argv, " ")
}

// codexItem makes the parts of one item_completed record: its call half or its result half.
func codexItem(raw json.RawMessage, mode byte) []Part {
	var it struct {
		Type    string          `json:"type"`
		ID      string          `json:"id"`
		Command []string        `json:"command"`
		Cwd     string          `json:"cwd"`
		Output  string          `json:"aggregated_output"`
		Exit    *int            `json:"exit_code"`
		Status  string          `json:"status"`
		Changes json.RawMessage `json:"changes"`
		Stdout  string          `json:"stdout"`
		Server  string          `json:"server"`
		Tool    string          `json:"tool"`
		Args    json.RawMessage `json:"arguments"`
		Result  *struct {
			Content []struct {
				Text string `json:"text"`
			} `json:"content"`
			IsError bool `json:"isError"`
		} `json:"result"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
		Query   string          `json:"query"`
		Action  json.RawMessage `json:"action"`
		Path    string          `json:"path"`
		Text    string          `json:"text"`
		Kind    string          `json:"kind"`
		Results []struct {
			Title string `json:"title"`
			URL   string `json:"url"`
		} `json:"results"`
	}
	if json.Unmarshal(raw, &it) != nil {
		return []Part{Meta("item:unreadable", nil)}
	}
	call := func(name string, input any, kind, server string) []Part {
		if mode == 'r' {
			return []Part{ToolResult(it.ID, "", false)}
		}
		b, _ := json.Marshal(input)
		return []Part{ToolCall(it.ID, name, b, kind, server)}
	}
	switch it.Type {
	case "CommandExecution":
		if mode == 'r' {
			failed := (it.Exit != nil && *it.Exit != 0) || (it.Exit == nil && (it.Status == "failed" || it.Status == "declined"))
			return []Part{ToolResult(it.ID, it.Output, failed)}
		}
		return call("exec_command", map[string]any{"command": codexShellText(it.Command), "argv": it.Command, "cwd": it.Cwd}, ToolNative, "")
	case "FileChange":
		if mode == 'r' {
			return []Part{ToolResult(it.ID, it.Stdout, it.Status != "completed")}
		}
		return call("apply_patch", map[string]any{"changes": it.Changes}, ToolNative, "")
	case "McpToolCall":
		if mode == 'r' {
			var texts []string
			isErr := it.Status == "failed"
			if it.Result != nil {
				for _, c := range it.Result.Content {
					texts = append(texts, c.Text)
				}
				isErr = isErr || it.Result.IsError
			} else if it.Error != nil {
				texts, isErr = []string{it.Error.Message}, true
			}
			return []Part{ToolResult(it.ID, strings.Join(texts, "\n"), isErr)}
		}
		return call(it.Tool, codexAny(it.Args), ToolMCP, it.Server)
	case "WebSearch":
		return call("web_search", map[string]any{"query": it.Query, "action": codexAny(it.Action)}, ToolCustom, "")
	case "ImageView":
		return call("view_image", map[string]any{"path": it.Path}, ToolNative, "")
	case "Plan":
		return call("update_plan", map[string]any{"text": it.Text}, ToolCustom, "")
	case "Extension":
		if mode == 'r' {
			type hit struct {
				Title string `json:"title"`
				URL   string `json:"url"`
			}
			hits := make([]hit, 0, len(it.Results))
			for _, r := range it.Results {
				hits = append(hits, hit{r.Title, r.URL})
			}
			b, _ := json.Marshal(hits)
			return []Part{ToolResult(it.ID, clip(string(b), noteLimit), false)}
		}
		return call("extension:"+it.Kind, map[string]any{"query": it.Query, "action": codexAny(it.Action)}, ToolCustom, "")
	case "CollabAgentToolCall":
		return call(first(it.Tool, "collab_agent"), codexAny(raw), ToolCustom, "")
	case "SubAgentActivity":
		return []Part{Meta("subagent_activity", codexMetaData(raw))}
	case "EnteredReviewMode":
		return []Part{Meta("review_entered", codexMetaData(raw))}
	case "ExitedReviewMode":
		return []Part{Meta("review_exited", codexMetaData(raw))}
	}
	return []Part{Meta("item:"+it.Type, codexMetaData(raw))}
}

// codexAny is a raw JSON value as a value that marshals back to itself, null when it is absent.
func codexAny(raw json.RawMessage) any {
	if len(raw) == 0 {
		return nil
	}
	return raw
}
