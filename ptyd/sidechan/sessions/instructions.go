package sessions

// sessions.instructions: the standing instructions and memory a program gives its model for one
// session, read from where that program keeps them. Claude Code puts its CLAUDE.md files and its
// auto-memory index into every prompt and again after every compaction; a session imported without
// them made the operator explain again what the other program had kept in memory.
//
// Each program's files are its own convention:
//   - Claude Code: ~/.claude/CLAUDE.md, then CLAUDE.md, .claude/CLAUDE.md and CLAUDE.local.md of
//     every folder from the filesystem root down to the session's folder, then the project's
//     auto-memory: memory/MEMORY.md beside the session file, and the notes in that folder, the ones
//     the index links first.
//   - Codex: $CODEX_HOME/AGENTS.md, then AGENTS.override.md or AGENTS.md of every folder down to
//     the session's, then $CODEX_HOME/memories/memory_summary.md, the part of its memory Codex
//     itself puts into the prompt.
//   - Grok and pi: AGENTS.md and CLAUDE.md of every folder down to the session's.
//
// The files are read as they are now, not as the session last saw them: the memory is what the
// operator keeps current, and what the session saw is in its transcript. Every file is masked as a
// transcript is, and held to the deny list.

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// InstructionFileMax bounds one file; InstructionsMax all of them, under the 640 KiB reply limit
// with room for JSON's escapes.
const (
	InstructionFileMax = 128 << 10
	InstructionsMax    = 384 << 10
	memoryNotesMax     = 200
)

// InstructionsRequest is sessions.instructions' parameters.
type InstructionsRequest struct {
	Harness string `json:"harness"`
	ID      string `json:"id"`
}

// InstructionFile is one file: its path, what it is to the program (user, project, local,
// memory_index, memory), its text, and whether the text was cut or, past the total, left out.
type InstructionFile struct {
	Path      string `json:"path"`
	Kind      string `json:"kind"`
	Content   string `json:"content"`
	Bytes     int64  `json:"bytes"`
	Truncated bool   `json:"truncated"`
	Omitted   bool   `json:"omitted"`
}

// InstructionsReply is sessions.instructions' answer.
type InstructionsReply struct {
	Cwd   string            `json:"cwd"`
	Files []InstructionFile `json:"files"`
}

type instructionSource struct {
	path string
	kind string
}

// Instructions reads the standing instructions and memory of one session.
func (s *Service) Instructions(req InstructionsRequest) (InstructionsReply, error) {
	p, err := s.parser(req.Harness)
	if err != nil {
		return InstructionsReply{}, err
	}
	e, err := s.find(p, req.ID, true)
	if err != nil {
		return InstructionsReply{}, err
	}
	cwd := e.header.Cwd
	reply := InstructionsReply{Cwd: cwd, Files: []InstructionFile{}}
	left := InstructionsMax
	for _, src := range instructionSources(s.env, p.ID(), cwd, e.cand.Path) {
		f, ok := readInstruction(s.env, src, left)
		if !ok {
			continue
		}
		left -= len(f.Content)
		reply.Files = append(reply.Files, f)
	}
	return reply, nil
}

// instructionSources lists the files a program would read for a session in cwd whose file is at
// sessionPath, in the order the program puts them into its prompt: global first, the closest last.
func instructionSources(e *Env, harness, cwd, sessionPath string) []instructionSource {
	var out []instructionSource
	seen := map[string]bool{}
	add := func(path, kind string) {
		// The user's own file is also the home folder's .claude/CLAUDE.md; it counts once, as the user's.
		if key := filepath.Clean(path); !seen[key] {
			seen[key] = true
			out = append(out, instructionSource{path, kind})
		}
	}
	folders := folderChain(cwd)
	switch harness {
	case "claude":
		config := filepath.Join(e.Home, ".claude")
		if dir := e.getenv("CLAUDE_CONFIG_DIR"); dir != "" {
			config = e.expand(dir)
		}
		add(filepath.Join(config, "CLAUDE.md"), "user")
		for _, dir := range folders {
			add(filepath.Join(dir, "CLAUDE.md"), "project")
			add(filepath.Join(dir, ".claude", "CLAUDE.md"), "project")
			add(filepath.Join(dir, "CLAUDE.local.md"), "local")
		}
		if sessionPath != "" {
			memory := filepath.Join(filepath.Dir(sessionPath), "memory")
			index := filepath.Join(memory, "MEMORY.md")
			add(index, "memory_index")
			for _, note := range memoryNotes(memory, index) {
				add(note, "memory")
			}
		}
	case "codex":
		home := codexHome(e)
		add(filepath.Join(home, "AGENTS.md"), "user")
		for _, dir := range folders {
			if _, err := os.Stat(filepath.Join(dir, "AGENTS.override.md")); err == nil {
				add(filepath.Join(dir, "AGENTS.override.md"), "project")
				continue
			}
			add(filepath.Join(dir, "AGENTS.md"), "project")
		}
		add(filepath.Join(home, "memories", "memory_summary.md"), "memory_index")
	default:
		for _, dir := range folders {
			add(filepath.Join(dir, "AGENTS.md"), "project")
			add(filepath.Join(dir, "CLAUDE.md"), "project")
		}
	}
	return out
}

// folderChain is cwd and every folder above it, the root first.
func folderChain(cwd string) []string {
	if cwd == "" || !filepath.IsAbs(filepath.Clean(cwd)) {
		return nil
	}
	var chain []string
	for dir := filepath.Clean(cwd); ; dir = filepath.Dir(dir) {
		chain = append(chain, dir)
		if filepath.Dir(dir) == dir {
			break
		}
	}
	for i, j := 0, len(chain)-1; i < j; i, j = i+1, j-1 {
		chain[i], chain[j] = chain[j], chain[i]
	}
	return chain
}

var markdownLink = regexp.MustCompile(`\]\(([^)\s]+\.md)\)`)

// memoryNotes are the notes of a memory folder: the ones the index links, in its order, then the
// rest by name. The index itself is not among them.
func memoryNotes(folder, index string) []string {
	entries, err := os.ReadDir(folder)
	if err != nil {
		return nil
	}
	present := map[string]bool{}
	var names []string
	for _, d := range entries {
		if d.Type().IsRegular() && strings.HasSuffix(d.Name(), ".md") && d.Name() != filepath.Base(index) {
			present[d.Name()] = true
			names = append(names, d.Name())
		}
	}
	sort.Strings(names)
	var ordered []string
	taken := map[string]bool{}
	if body, err := os.ReadFile(index); err == nil {
		for _, m := range markdownLink.FindAllStringSubmatch(string(body), -1) {
			name := filepath.Base(m[1])
			if present[name] && !taken[name] {
				taken[name] = true
				ordered = append(ordered, name)
			}
		}
	}
	for _, name := range names {
		if !taken[name] {
			ordered = append(ordered, name)
		}
	}
	if len(ordered) > memoryNotesMax {
		ordered = ordered[:memoryNotesMax]
	}
	out := make([]string, len(ordered))
	for i, name := range ordered {
		out[i] = filepath.Join(folder, name)
	}
	return out
}

// readInstruction reads one file within `left` bytes of the reply. A file that is not there is
// not reported; one on the deny list, or not a regular file, neither. One past the total is
// reported as omitted, with its size, so the reader can say it was left out.
func readInstruction(e *Env, src instructionSource, left int) (InstructionFile, bool) {
	clean := filepath.Clean(src.path)
	real, err := filepath.EvalSymlinks(clean)
	if err != nil || e.refused(clean, real) {
		return InstructionFile{}, false
	}
	st, err := os.Stat(real)
	if err != nil || !st.Mode().IsRegular() {
		return InstructionFile{}, false
	}
	out := InstructionFile{Path: NormPath(clean), Kind: src.kind, Bytes: st.Size()}
	if left <= 0 {
		out.Omitted = true
		return out, true
	}
	limit := InstructionFileMax
	if left < limit {
		limit = left
	}
	f, err := os.Open(real)
	if err != nil {
		return InstructionFile{}, false
	}
	defer f.Close()
	buf, err := io.ReadAll(io.LimitReader(f, int64(limit)))
	if err != nil && !errors.Is(err, io.EOF) {
		return InstructionFile{}, false
	}
	out.Truncated = st.Size() > int64(len(buf))
	var red Redactor
	out.Content = red.Text(strings.ToValidUTF8(string(buf), "�"))
	return out, true
}
