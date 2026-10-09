package sessions

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// The bounds of the RPCs. A reply is one frame of a megabyte, and config.MaxReplyData keeps file
// data under 640 KiB; a page of turns stays under MaxPage so the header and the JSON around it fit.
const (
	MaxPage      = 512 << 10
	MinPage      = 16 << 10
	DefaultLimit = 200
	MaxLimit     = 2000
	// ScanBudget is how long one scan may read before it answers with what it has, as fs.browse.
	ScanBudget = 1500 * time.Millisecond
	// harnessTTL is how long sessions.harnesses answers from its last count.
	harnessTTL = 30 * time.Second
	// LiveWindow is how recent a session's last record must be for it to count as still running.
	LiveWindow = 120 * time.Second
	// indexCache is how many session indexes are kept for paging: a read pages through one session
	// at a time, and an index is small, so a few cover a host reading two while the app previews.
	indexCache  = 6
	peekWorkers = 8
	// findFresh is how long a read trusts the last listing to find a session by its id.
	findFresh = 5 * time.Second
	// snapshotFor is how long paging keeps reading one index of a session that is still growing. A
	// live session changes with every record; indexing it again for each page cost seconds a page
	// and could shift the turns under the reader. The reply's offset says where the snapshot ends.
	snapshotFor = time.Minute
	// titleLimit is how long a title may be, in bytes.
	titleLimit = 120 * 4
)

// Parsers are the programs this build reads, in the order the app lists them.
func Parsers() []Parser { return []Parser{claudeParser{}, codexParser{}, grokParser{}, piParser{}} }

// Service answers the sessions.* calls.
type Service struct {
	env     *Env
	parsers []Parser
	byID    map[string]Parser
	heavy   chan struct{} // bounds the full passes running at once

	mu       sync.Mutex
	light    map[string]lightEntry
	lightAt  map[string]time.Time // per program: when its files were last compared with the cache
	full     map[string]Header
	indexes  []cachedIndex
	harness  []HarnessInfo
	harnessT time.Time
}

type lightEntry struct {
	sig    string
	cand   Candidate
	header Header
	err    bool
}

type cachedIndex struct {
	key        string
	path       string
	sidechains bool
	at         time.Time
	idx        *Index
}

// NewService serves the given parsers over an environment.
func NewService(env *Env, parsers []Parser) *Service {
	s := &Service{env: env, parsers: parsers, byID: map[string]Parser{}, heavy: make(chan struct{}, 2),
		light: map[string]lightEntry{}, lightAt: map[string]time.Time{}, full: map[string]Header{}}
	for _, p := range parsers {
		s.byID[p.ID()] = p
	}
	return s
}

func (s *Service) parser(id string) (Parser, error) {
	p, ok := s.byID[id]
	if !ok {
		return nil, fmt.Errorf("%w: unknown harness %q", ErrInvalid, id)
	}
	return p, nil
}

// HarnessInfo is one entry of sessions.harnesses.
type HarnessInfo struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Found    bool   `json:"found"`
	Root     string `json:"root"`
	Sessions int    `json:"sessions"`
	Folders  int    `json:"folders"`
	Version  string `json:"version"`
}

// Harnesses counts each program's sessions and folders. The count comes from the same headers a
// scan reads, which are cached by file; the answer itself is kept for harnessTTL.
func (s *Service) Harnesses() []HarnessInfo {
	s.mu.Lock()
	if s.harness != nil && s.env.now().Sub(s.harnessT) < harnessTTL {
		out := append([]HarnessInfo{}, s.harness...)
		s.mu.Unlock()
		return out
	}
	s.mu.Unlock()
	out := []HarnessInfo{}
	for _, p := range s.parsers {
		info := HarnessInfo{ID: p.ID(), Name: p.Name()}
		roots := existingRoots(s.env, p)
		if len(roots) > 0 {
			info.Found = true
			info.Root = s.env.display(roots[0])
		} else if all := p.Roots(s.env); len(all) > 0 {
			info.Root = s.env.display(all[0])
		}
		entries, _ := s.lightPass(p, time.Now().Add(ScanBudget))
		folders := map[string]bool{}
		var newest time.Time
		for _, e := range entries {
			if e.header.parent != "" {
				continue
			}
			info.Sessions++
			folders[foldKey(s.env, e.header.Cwd)] = true
			if t := e.header.UpdatedAt.Time; t.After(newest) && e.header.version != "" {
				newest, info.Version = t, e.header.version
			}
		}
		info.Folders = len(folders)
		out = append(out, info)
	}
	s.mu.Lock()
	s.harness, s.harnessT = out, s.env.now()
	s.mu.Unlock()
	return append([]HarnessInfo{}, out...)
}

func foldKey(e *Env, p string) string {
	if e.fold() {
		return strings.ToLower(p)
	}
	return p
}

func existingRoots(e *Env, p Parser) []string {
	var out []string
	for _, r := range p.Roots(e) {
		if isDir(r) && !e.refused(r) {
			out = append(out, r)
		}
	}
	return out
}

// lightPass gives every session of a program its light header, from the cache when the files have
// not changed and from Peek otherwise, several at once. It stops peeking at the deadline; the
// sessions not reached are left out and truncated says so.
func (s *Service) lightPass(p Parser, deadline time.Time) ([]lightEntry, bool) {
	var cands []Candidate
	for _, root := range existingRoots(s.env, p) {
		_ = p.Discover(s.env, root, func(c Candidate) {
			c.Harness, c.Root = p.ID(), root
			cands = append(cands, c)
		})
	}
	out := make([]lightEntry, len(cands))
	done := make([]bool, len(cands))
	var todo []int
	s.mu.Lock()
	for i, c := range cands {
		if e, ok := s.light[c.Path]; ok && e.sig == c.signature() {
			out[i], done[i] = e, true
			continue
		}
		todo = append(todo, i)
	}
	s.mu.Unlock()
	work := make(chan int)
	var wg sync.WaitGroup
	for w := 0; w < peekWorkers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range work {
				c := cands[i]
				h, err := p.Peek(s.env, &c)
				e := lightEntry{sig: c.signature(), cand: c, header: h, err: err != nil}
				if err == nil {
					s.finishHeader(p, &c, &e.header)
				}
				out[i], done[i] = e, true
			}
		}()
	}
	truncated := false
	for _, i := range todo {
		if time.Now().After(deadline) {
			truncated = true
			break
		}
		work <- i
	}
	close(work)
	wg.Wait()
	kept := out[:0]
	s.mu.Lock()
	seen := map[string]bool{}
	for i, e := range out {
		if !done[i] {
			continue
		}
		s.light[e.cand.Path] = e
		seen[e.cand.Path] = true
		if !e.err {
			kept = append(kept, e)
		}
	}
	if !truncated {
		s.lightAt[p.ID()] = time.Now()
	}
	// Forget the files of this program that are gone, so the cache does not grow with every session
	// the program deletes.
	for path, e := range s.light {
		if e.cand.Harness == p.ID() && !seen[path] && !truncated {
			delete(s.light, path)
		}
	}
	s.mu.Unlock()
	return kept, truncated
}

// finishHeader sets what every header has whatever program wrote it.
func (s *Service) finishHeader(p Parser, c *Candidate, h *Header) {
	h.V, h.Harness = ModelVersion, p.ID()
	if h.ID == "" {
		h.ID = c.ID
	}
	h.Cwd = NormPath(h.Cwd)
	h.Bytes = c.Size
	h.Source = Source{Path: c.Path}
	if h.UpdatedAt.IsZero() {
		h.UpdatedAt = Stamp{c.Mtime.UTC()}
	}
	var r Redactor
	h.Title = clip(r.Text(oneLine(h.Title)), titleLimit)
	h.firstPrompt = clip(r.Text(oneLine(h.firstPrompt)), titleLimit)
	if h.Title == "" {
		h.Title = h.firstPrompt
	}
}

// oneLine is a title: the first non-empty line, trimmed.
func oneLine(s string) string {
	for _, l := range strings.Split(s, "\n") {
		if l = strings.TrimSpace(l); l != "" {
			return l
		}
	}
	return ""
}

// ScanRequest is sessions.scan's parameters.
type ScanRequest struct {
	Harness string `json:"harness"`
	Path    string `json:"path"`
	Depth   int    `json:"depth"`
	Query   string `json:"query"`
	Deep    bool   `json:"deep"`
	Limit   int    `json:"limit"`
	Cursor  string `json:"cursor"`
}

// Folder is a folder with sessions somewhere inside it.
type Folder struct {
	Name     string `json:"name,omitempty"`
	Path     string `json:"path"`
	Sessions int    `json:"sessions"`
	Latest   Stamp  `json:"latest"`
}

// ScanReply is sessions.scan's answer.
type ScanReply struct {
	Path      string   `json:"path"`
	Here      []Header `json:"here"`
	Children  []Folder `json:"children"`
	Folders   []Folder `json:"folders"`
	Truncated bool     `json:"truncated"`
	Cursor    string   `json:"cursor"`
}

// Scan lists one folder's sessions, the subfolders that hold sessions further down, and, for the
// empty path, every folder the program has sessions in.
//
// Grouping uses the light headers of every session (one head and tail read each, cached by file);
// the sessions of the folder asked about get their full header, which is one pass over each of
// their files, cached the same way. Both stop at ScanBudget: what was not reached is left for the
// next call, which `cursor` continues.
func (s *Service) Scan(req ScanRequest) (ScanReply, error) {
	p, err := s.parser(req.Harness)
	if err != nil {
		return ScanReply{}, err
	}
	if req.Limit < 0 || req.Limit > MaxLimit {
		return ScanReply{}, fmt.Errorf("%w: limit is 0..%d", ErrInvalid, MaxLimit)
	}
	if req.Limit == 0 {
		req.Limit = DefaultLimit
	}
	offset := 0
	if req.Cursor != "" {
		if offset, err = strconv.Atoi(req.Cursor); err != nil || offset < 0 {
			return ScanReply{}, fmt.Errorf("%w: cursor %q", ErrInvalid, req.Cursor)
		}
	}
	base := ""
	if req.Path != "" {
		base = NormPath(s.env.expand(req.Path))
	}
	deadline := time.Now().Add(ScanBudget)
	entries, truncated := s.lightPass(p, deadline)
	reply := ScanReply{Path: base, Here: []Header{}, Children: []Folder{}, Folders: []Folder{}, Truncated: truncated}

	children := map[string]*Folder{}
	folders := map[string]*Folder{}
	var here []lightEntry
	query := strings.ToLower(strings.TrimSpace(req.Query))
	for _, e := range entries {
		h := e.header
		if h.parent != "" {
			continue // a sub-agent: read inside its parent
		}
		if query != "" {
			if (base == "" || s.env.within(base, h.Cwd)) && matches(h, query) {
				here = append(here, e)
			}
			continue
		}
		latest := h.UpdatedAt
		if base == "" {
			key := foldKey(s.env, h.Cwd)
			f := folders[key]
			if f == nil {
				f = &Folder{Path: h.Cwd}
				folders[key] = f
			}
			f.Sessions++
			if latest.After(f.Latest.Time) {
				f.Latest = latest
			}
			if h.Cwd == "" {
				here = append(here, e)
			}
			continue
		}
		if s.env.samePath(h.Cwd, base) {
			here = append(here, e)
			continue
		}
		if !s.env.within(base, h.Cwd) {
			continue
		}
		rest := strings.TrimPrefix(h.Cwd[len(base):], "/")
		name, _, _ := strings.Cut(rest, "/")
		key := foldKey(s.env, name)
		f := children[key]
		if f == nil {
			child := strings.TrimSuffix(base, "/") + "/" + name
			f = &Folder{Name: name, Path: child}
			children[key] = f
		}
		f.Sessions++
		if latest.After(f.Latest.Time) {
			f.Latest = latest
		}
	}
	for _, f := range children {
		reply.Children = append(reply.Children, *f)
	}
	for _, f := range folders {
		if f.Path != "" {
			reply.Folders = append(reply.Folders, *f)
		}
	}
	sort.Slice(reply.Children, func(i, j int) bool {
		return strings.ToLower(reply.Children[i].Name) < strings.ToLower(reply.Children[j].Name)
	})
	sort.Slice(reply.Folders, func(i, j int) bool { return reply.Folders[i].Latest.After(reply.Folders[j].Latest.Time) })
	sort.Slice(here, func(i, j int) bool {
		return here[i].header.UpdatedAt.After(here[j].header.UpdatedAt.Time)
	})

	if offset > len(here) {
		offset = len(here)
	}
	for i := offset; i < len(here); i++ {
		if len(reply.Here) >= req.Limit || (len(reply.Here) > 0 && time.Now().After(deadline)) {
			reply.Truncated, reply.Cursor = true, strconv.Itoa(i)
			break
		}
		h, err := s.fullHeader(p, here[i])
		if err != nil {
			h = here[i].header
		}
		h.Flags.Live = s.live(p, &here[i].cand, h)
		reply.Here = append(reply.Here, h)
	}
	return reply, nil
}

func matches(h Header, query string) bool {
	return strings.Contains(strings.ToLower(h.Title), query) || strings.Contains(strings.ToLower(h.firstPrompt), query) ||
		strings.Contains(strings.ToLower(h.ID), query)
}

// withChildren attaches a candidate's sub-agent sessions, from the light headers of its program.
func (s *Service) withChildren(e lightEntry) Candidate {
	c := e.cand
	c.Children = nil
	s.mu.Lock()
	for _, other := range s.light {
		if other.cand.Harness == c.Harness && !other.err && other.header.parent == e.header.ID && other.cand.Path != c.Path {
			c.Children = append(c.Children, other.cand)
		}
	}
	s.mu.Unlock()
	sort.Slice(c.Children, func(i, j int) bool { return c.Children[i].Path < c.Children[j].Path })
	return c
}

// fullHeader is a session's header with its counts, from one pass over its files.
func (s *Service) fullHeader(p Parser, e lightEntry) (Header, error) {
	c := s.withChildren(e)
	key := c.signature()
	s.mu.Lock()
	if h, ok := s.full[key]; ok {
		s.mu.Unlock()
		return h, nil
	}
	s.mu.Unlock()
	idx, err := s.index(p, &c, true, false)
	if err != nil {
		return Header{}, err
	}
	return idx.Header, nil
}

// index builds (or finds) the index of a candidate.
func (s *Service) index(p Parser, c *Candidate, sidechains, snapshot bool) (*Index, error) {
	key := c.signature()
	if !sidechains {
		key += "\x00main"
	}
	s.mu.Lock()
	for i, ci := range s.indexes {
		same := ci.key == key
		if !same && snapshot && ci.path == c.Path && ci.sidechains == sidechains && time.Since(ci.at) < snapshotFor {
			same = true
		}
		if same {
			// Most recent last.
			s.indexes = append(append(s.indexes[:i:i], s.indexes[i+1:]...), ci)
			s.mu.Unlock()
			return ci.idx, nil
		}
	}
	s.mu.Unlock()
	s.heavy <- struct{}{}
	idx, err := p.Index(s.env, c, sidechains)
	<-s.heavy
	if err != nil {
		return nil, err
	}
	h := &idx.Header
	s.finishHeader(p, c, h)
	h.full = true
	if sidechains {
		h.Flags.Sidechains = max(h.Flags.Sidechains, len(c.Children))
	}
	s.mu.Lock()
	if sidechains {
		s.full[c.signature()] = *h
	}
	s.indexes = append(s.indexes, cachedIndex{key: key, path: c.Path, sidechains: sidechains, at: time.Now(), idx: idx})
	if len(s.indexes) > indexCache {
		s.indexes = s.indexes[len(s.indexes)-indexCache:]
	}
	s.mu.Unlock()
	return idx, nil
}

// live is the live check: a last record within LiveWindow and, where the process table can be
// read, a process of the program with the session's file open or working in its folder.
func (s *Service) live(p Parser, c *Candidate, h Header) bool {
	if h.UpdatedAt.IsZero() || s.env.now().Sub(h.UpdatedAt.Time) > LiveWindow {
		return false
	}
	confirmed, known := procsConfirm(s.env, p.Procs(), append([]string{c.Path}, c.Extra...), h.Cwd)
	return !known || confirmed
}

// ReadRequest is sessions.read's parameters.
type ReadRequest struct {
	Harness    string `json:"harness"`
	ID         string `json:"id"`
	From       int64  `json:"from"`
	MaxBytes   int    `json:"max_bytes"`
	Sidechains *bool  `json:"sidechains"`
	Raw        bool   `json:"raw"`
	// File picks which of the session's files a raw read pages through: 0 is the main one, then the
	// sub-agents' in the order `files` lists them.
	File int `json:"file"`
}

// ReadReply is sessions.read's answer.
type ReadReply struct {
	Header Header `json:"header"`
	Turns  []Turn `json:"turns"`
	Next   int64  `json:"next"`
	Done   bool   `json:"done"`
	Live   bool   `json:"live"`
	Masked int    `json:"masked"`
	// Offset is how many bytes of the main file the turns cover: a later read of only what is new
	// starts its count there.
	Offset int64 `json:"offset"`
	// Total is how many turns the read pages through, for a reader's progress. The header's
	// `messages` counts only what a person would call a message, so a reader that measured its turns
	// against it ran far past a hundred percent on a session of many tool calls.
	Total int `json:"total"`
}

// RawReply is sessions.read with raw: the source's own bytes, a page of whole records at a time,
// masked as text.
type RawReply struct {
	Header Header    `json:"header"`
	File   int       `json:"file"`
	Files  []RawFile `json:"files"`
	Data   []byte    `json:"data_b64"`
	Next   int64     `json:"next"`
	Done   bool      `json:"done"`
	Live   bool      `json:"live"`
	Masked int       `json:"masked"`
}

// RawFile is one file of a session, for raw paging.
type RawFile struct {
	Name  string `json:"name"`
	Bytes int64  `json:"bytes"`
}

// find locates a session by its id among the program's sessions. `listed` lets it answer from the
// last listing when that is recent; a caller that needs the files as they are now passes false.
func (s *Service) find(p Parser, id string, listed bool) (lightEntry, error) {
	if id == "" {
		return lightEntry{}, fmt.Errorf("%w: id is empty", ErrInvalid)
	}
	// A read pages through a session in quick calls; listing every file of the program again for
	// each page (four thousand rollouts for Codex) was most of the time of a read.
	s.mu.Lock()
	fresh := listed && time.Since(s.lightAt[p.ID()]) < findFresh
	if fresh {
		for _, e := range s.light {
			if e.cand.Harness == p.ID() && !e.err && (e.header.ID == id || e.cand.ID == id) {
				s.mu.Unlock()
				return e, nil
			}
		}
	}
	s.mu.Unlock()
	entries, _ := s.lightPass(p, time.Now().Add(10*ScanBudget))
	for _, e := range entries {
		if e.header.ID == id || e.cand.ID == id {
			return e, nil
		}
	}
	return lightEntry{}, fmt.Errorf("%w: no %s session %s", ErrNotFound, p.ID(), id)
}

// Read pages through one session's turns, from turn `from`, as many as fit in max_bytes (at least
// one). Every text is masked first; masked counts what was.
func (s *Service) Read(req ReadRequest) (any, error) {
	p, err := s.parser(req.Harness)
	if err != nil {
		return nil, err
	}
	if req.MaxBytes == 0 {
		req.MaxBytes = MaxPage
	}
	if req.MaxBytes < MinPage || req.MaxBytes > MaxPage {
		return nil, fmt.Errorf("%w: max_bytes is %d..%d", ErrInvalid, MinPage, MaxPage)
	}
	if req.From < 0 {
		return nil, fmt.Errorf("%w: from must not be negative", ErrInvalid)
	}
	e, err := s.find(p, req.ID, true)
	if err != nil {
		return nil, err
	}
	sidechains := req.Sidechains == nil || *req.Sidechains
	c := s.withChildren(e)
	if req.Raw {
		return s.readRaw(p, &c, e.header, req)
	}
	idx, err := s.index(p, &c, sidechains, true)
	if err != nil {
		return nil, err
	}
	if req.From > 0 && int(req.From) >= len(idx.Turns) {
		// A read that starts past the end of the snapshot asks for what the program wrote since:
		// "pull in what is new" right after an import. The snapshot, and the listing it was found
		// in, would both answer that nothing is, so the files are looked at again as they are now.
		if e, err = s.find(p, req.ID, false); err != nil {
			return nil, err
		}
		c = s.withChildren(e)
		if idx, err = s.index(p, &c, sidechains, false); err != nil {
			return nil, err
		}
	}
	header := idx.Header
	header.Flags.Live = s.live(p, &c, header)
	reply := ReadReply{Header: header, Turns: []Turn{}, Live: header.Flags.Live, Offset: idx.Consumed, Total: len(idx.Turns)}
	files := map[int]*os.File{}
	defer func() {
		for _, f := range files {
			f.Close()
		}
	}()
	total := 0
	next := int(req.From)
	for ; next < len(idx.Turns); next++ {
		ref := &idx.Turns[next]
		records := make([][]byte, 0, len(ref.Spans))
		for _, sp := range ref.Spans {
			f, ok := files[sp.File]
			if !ok {
				if sp.File >= len(idx.Files) {
					continue
				}
				f, _, err = s.openAny(p, idx.Files[sp.File])
				if err != nil {
					return nil, err
				}
				files[sp.File] = f
			}
			rec, err := readSpan(f, sp)
			if err != nil {
				// The file was rewritten under the index: the next read builds a new one.
				s.forget(&c)
				return nil, fmt.Errorf("%w: the session changed while it was read; read again", ErrBusy)
			}
			records = append(records, rec)
		}
		t := Turn{Seq: next, ExtID: ref.ExtID, Parent: ref.Parent, Role: ref.Role, At: Stamp{ref.At}, Sidechain: ref.Sidechain,
			Model: ref.Model, Usage: ref.Usage, Parts: p.Build(s.env, idx, ref, records)}
		if t.Parts == nil {
			t.Parts = []Part{}
		}
		var red Redactor
		red.Turn(&t)
		size := fit(&t, req.MaxBytes-total)
		if total+size > req.MaxBytes && len(reply.Turns) > 0 {
			break
		}
		total += size
		reply.Turns = append(reply.Turns, t)
		reply.Masked += red.Count
	}
	reply.Next = int64(next)
	reply.Done = next >= len(idx.Turns)
	return reply, nil
}

// openAny opens a file of a session under the program's root that holds it: a Codex sub-agent may
// be archived under another root than its parent.
func (s *Service) openAny(p Parser, path string) (*os.File, os.FileInfo, error) {
	roots := existingRoots(s.env, p)
	for _, root := range roots {
		if s.env.within(NormPath(root), NormPath(path)) {
			return s.env.open(root, path)
		}
	}
	if len(roots) == 0 {
		return nil, nil, fmt.Errorf("%w: %s", ErrNotFound, path)
	}
	return s.env.open(roots[0], path)
}

// forget drops a candidate's cached index.
func (s *Service) forget(c *Candidate) {
	s.mu.Lock()
	defer s.mu.Unlock()
	kept := s.indexes[:0]
	for _, ci := range s.indexes {
		if !strings.HasPrefix(ci.key, c.Path+"\x00") {
			kept = append(kept, ci)
		}
	}
	s.indexes = kept
}

// fit shortens a turn until its JSON fits in room (or as far as shortening goes) and returns its
// size. A single tool output can be megabytes; the turn still goes, with the long texts cut and
// marked, rather than stopping the read.
func fit(t *Turn, room int) int {
	room = max(room, MinPage)
	b, _ := json.Marshal(t)
	for limit := room / 4; len(b) > room && limit >= 256; limit /= 4 {
		for i := range t.Parts {
			shorten(&t.Parts[i], limit)
		}
		b, _ = json.Marshal(t)
	}
	return len(b)
}

const cutMark = "\n…[cut by the import]"

func shorten(p *Part, limit int) {
	cut := func(s string) (string, bool) {
		if len(s) <= limit {
			return s, false
		}
		return clip(s, limit) + cutMark, true
	}
	var c bool
	p.Text, c = cut(p.Text)
	p.Summary, _ = cut(p.Summary)
	if p.Output, c = cut(p.Output); c {
		p.Truncated = true
	}
	_ = c
	if len(p.DataB64) > limit {
		p.DataB64 = ""
	}
	for i := range p.Images {
		if len(p.Images[i].DataB64) > limit {
			p.Images[i].DataB64 = ""
		}
	}
	if len(p.Input) > limit {
		short, _ := json.Marshal(map[string]any{"cut_by_import": true, "text": clip(string(p.Input), limit)})
		p.Input = short
	}
	if p.Data != nil {
		if b, _ := json.Marshal(p.Data); len(b) > limit {
			p.Data = map[string]any{"cut_by_import": true, "text": clip(string(b), limit)}
		}
	}
}

// readRaw pages through one of a session's files as it is on disk, whole records at a time, each
// masked as text: the original kept beside an import is kept masked, never in the clear.
func (s *Service) readRaw(p Parser, c *Candidate, h Header, req ReadRequest) (RawReply, error) {
	paths := append([]string{c.Path}, c.Extra...)
	for _, ch := range c.Children {
		paths = append(paths, ch.Path)
	}
	if req.File < 0 || req.File >= len(paths) {
		return RawReply{}, fmt.Errorf("%w: file is 0..%d", ErrInvalid, len(paths)-1)
	}
	reply := RawReply{Header: h, File: req.File, Files: []RawFile{}, Data: []byte{}}
	for _, path := range paths {
		var size int64
		if st, err := os.Stat(path); err == nil {
			size = st.Size()
		}
		reply.Files = append(reply.Files, RawFile{Name: relName(c.Path, path), Bytes: size})
	}
	f, st, err := s.openAny(p, paths[req.File])
	if err != nil {
		return RawReply{}, err
	}
	defer f.Close()
	if req.From > st.Size() {
		return RawReply{}, fmt.Errorf("%w: from is past the end of the file", ErrInvalid)
	}
	if _, err := f.Seek(req.From, 0); err != nil {
		return RawReply{}, err
	}
	// Base64 grows the data by a third; the page is measured after it.
	room := req.MaxBytes * 3 / 4
	r := bufio.NewReaderSize(f, 64<<10)
	off := req.From
	for {
		// A record longer than the reader's buffer comes in pieces, each masked on its own.
		chunk, err := r.ReadSlice('\n')
		if len(chunk) > 0 {
			var one Redactor
			masked := one.Text(string(chunk))
			if len(reply.Data) > 0 && len(reply.Data)+len(masked) > room {
				break
			}
			reply.Data = append(reply.Data, masked...)
			reply.Masked += one.Count
			off += int64(len(chunk))
		}
		if (err != nil && err != bufio.ErrBufferFull) || len(reply.Data) >= room {
			break
		}
	}
	reply.Next = off
	reply.Done = off >= st.Size()
	reply.Live = s.live(p, c, h)
	return reply, nil
}

// relName is a session file's name relative to the main file's folder, for the list of files.
func relName(main, p string) string {
	dir := main[:max(strings.LastIndexAny(main, `/\`), 0)]
	if strings.HasPrefix(p, dir) {
		return strings.TrimLeft(p[len(dir):], `/\`)
	}
	return p
}
