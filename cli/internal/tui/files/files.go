// Package files is the index behind `@` mentions: the paths of the working folder, matched fuzzily
// as the person types.
package files

import (
	"bytes"
	"context"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Index is the list of paths under one folder, relative to it, directories ending in "/". It is
// built once and read many times; it is safe for concurrent reads.
type Index struct {
	entries []entry
}

type entry struct {
	path  string
	runes []rune
	lower []rune
	// base is the rune offset where the last path element starts.
	base  int
	dir   bool
	depth int
}

// Match is one ranked path. Positions are the rune offsets in Path that the query matched, for
// highlighting.
type Match struct {
	Path      string
	Score     int
	Positions []int
}

// skipped are directories a walk never enters: dependency trees, build output and caches, where a
// mention is never meant and which can hold more files than the rest of the folder together.
var skipped = map[string]bool{
	".git": true, "node_modules": true, "vendor": true, "dist": true, "build": true,
	".venv": true, "venv": true, "__pycache__": true, "target": true,
}

// Build indexes root. In a git work tree the list comes from git, which already knows what is ignored
// (tracked and untracked files, without the ignored ones); elsewhere the folder is walked, skipping
// hidden and dependency directories. At most limit entries are kept (limit <= 0 means no bound).
func Build(ctx context.Context, root string, limit int) (*Index, error) {
	paths, err := gitFiles(ctx, root)
	if err != nil {
		paths, err = walk(ctx, root, limit)
		if err != nil {
			return nil, err
		}
	}
	return newIndex(paths, limit), nil
}

func gitFiles(ctx context.Context, root string) ([]string, error) {
	cmd := exec.CommandContext(ctx, "git", "ls-files", "-co", "--exclude-standard", "-z")
	cmd.Dir = root
	// No lock files: the index is read while the person may be committing in another window.
	cmd.Env = append(os.Environ(), "GIT_OPTIONAL_LOCKS=0")
	out, err := cmd.Output()
	if err != nil {
		return nil, err
	}
	var paths []string
	for _, p := range bytes.Split(out, []byte{0}) {
		if len(p) > 0 {
			paths = append(paths, filepath.ToSlash(string(p)))
		}
	}
	return paths, nil
}

func walk(ctx context.Context, root string, limit int) ([]string, error) {
	var paths []string
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			// An unreadable folder is skipped, not fatal: one locked directory must not empty the list.
			if d != nil && d.IsDir() && path != root {
				return filepath.SkipDir
			}
			return nil
		}
		if ctxErr := ctx.Err(); ctxErr != nil {
			return ctxErr
		}
		if path == root {
			return nil
		}
		name := d.Name()
		if d.IsDir() && (skipped[name] || strings.HasPrefix(name, ".")) {
			return filepath.SkipDir
		}
		if d.IsDir() {
			return nil
		}
		rel, relErr := filepath.Rel(root, path)
		if relErr != nil {
			return nil
		}
		paths = append(paths, filepath.ToSlash(rel))
		if limit > 0 && len(paths) >= limit {
			return filepath.SkipAll
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return paths, nil
}

// newIndex adds the directories the files sit in, so "@src/" can be mentioned, and orders the entries
// shallow first with directories before files at each depth: the order an empty query lists them in.
func newIndex(files []string, limit int) *Index {
	seen := map[string]bool{}
	var all []string
	for _, f := range files {
		parts := strings.Split(f, "/")
		for i := 1; i < len(parts); i++ {
			dir := strings.Join(parts[:i], "/") + "/"
			if !seen[dir] {
				seen[dir] = true
				all = append(all, dir)
			}
		}
		if !seen[f] {
			seen[f] = true
			all = append(all, f)
		}
	}
	entries := make([]entry, 0, len(all))
	for _, p := range all {
		dir := strings.HasSuffix(p, "/")
		trimmed := strings.TrimSuffix(p, "/")
		runes := []rune(p)
		entries = append(entries, entry{
			path:  p,
			runes: runes,
			lower: []rune(strings.ToLower(p)),
			base:  utf8.RuneCountInString(trimmed[:strings.LastIndex(trimmed, "/")+1]),
			dir:   dir,
			depth: strings.Count(trimmed, "/"),
		})
	}
	sort.SliceStable(entries, func(i, j int) bool {
		a, b := entries[i], entries[j]
		if a.depth != b.depth {
			return a.depth < b.depth
		}
		if a.dir != b.dir {
			return a.dir
		}
		return a.path < b.path
	})
	if limit > 0 && len(entries) > limit {
		entries = entries[:limit]
	}
	return &Index{entries: entries}
}

// Len is the number of entries.
func (ix *Index) Len() int { return len(ix.entries) }

// Match ranks the entries against query, best first, at most n of them (n <= 0 means all). An empty
// query lists the entries in index order. Matching is a case-insensitive subsequence; see score.
func (ix *Index) Match(query string, n int) []Match {
	query = strings.TrimPrefix(strings.ToLower(strings.TrimSpace(query)), "./")
	if query == "" {
		count := len(ix.entries)
		if n > 0 && n < count {
			count = n
		}
		out := make([]Match, count)
		for i := range out {
			out[i] = Match{Path: ix.entries[i].path}
		}
		return out
	}
	q := []rune(query)
	var out []Match
	for i := range ix.entries {
		if score, positions, ok := match(&ix.entries[i], q); ok {
			out = append(out, Match{Path: ix.entries[i].path, Score: score, Positions: positions})
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		if len(out[i].Path) != len(out[j].Path) {
			return len(out[i].Path) < len(out[j].Path)
		}
		return out[i].Path < out[j].Path
	})
	if n > 0 && len(out) > n {
		out = out[:n]
	}
	return out
}

// The weights of the score, in the manner of fzf: every matched character earns a base, a character
// at the start of a word or path element earns a boundary bonus, a run of adjacent matches earns more
// per character, gaps cost a little, and a query that falls entirely in the file name (rather than
// spread over the directories) earns a bonus, because "@model" means model.go far more often than
// m/o/d/e/l scattered through a long path.
const (
	scoreMatch       = 16
	bonusBoundary    = 10
	bonusFirst       = 8
	bonusConsecutive = 6
	bonusBasename    = 24
	bonusWholeName   = 16
	penaltyGap       = 2
	penaltyGapStart  = 3
)

// match finds the query as a subsequence of the entry. The first pass finds the earliest place the
// query ends; the second walks back from there to the latest place it can start, which gives the
// tightest window — the one a person would point at. Within the window each query character takes
// the first occurrence, preferring a boundary when one is at hand.
func match(e *entry, q []rune) (int, []int, bool) {
	text := e.lower
	qi := 0
	end := -1
	for i := 0; i < len(text) && qi < len(q); i++ {
		if text[i] == q[qi] {
			qi++
			if qi == len(q) {
				end = i
			}
		}
	}
	if end < 0 {
		return 0, nil, false
	}
	start := end
	qi = len(q) - 1
	for i := end; i >= 0; i-- {
		if text[i] == q[qi] {
			qi--
			if qi < 0 {
				start = i
				break
			}
		}
	}

	// A query wholly inside the file name is matched there instead when it fits, so the positions
	// (and the bonus) land on the name.
	inBase := false
	if start < e.base {
		if positions, ok := subsequence(text, q, e.base); ok {
			start = positions[0]
			inBase = true
		}
	} else {
		inBase = true
	}

	positions := make([]int, 0, len(q))
	score := 0
	qi = 0
	prev := -2
	for i := start; i < len(text) && qi < len(q); i++ {
		if text[i] != q[qi] {
			continue
		}
		score += scoreMatch
		if boundary(e.runes, i) {
			score += bonusBoundary
			if qi == 0 {
				score += bonusFirst
			}
		}
		if prev == i-1 {
			score += bonusConsecutive
		} else if prev >= 0 {
			gap := i - prev - 1
			score -= penaltyGapStart + penaltyGap*min(gap, 8)
		}
		positions = append(positions, i)
		prev = i
		qi++
	}
	if inBase {
		score += bonusBasename
		if e.base == positions[0] {
			score += bonusFirst
			// The query is the whole name up to its extension: "@model" names model.go, not models/.
			name := e.lower[e.base:]
			if len(name) == len(q) || (len(name) > len(q) && name[len(q)] == '.' && positions[len(positions)-1] == e.base+len(q)-1) {
				score += bonusWholeName
			}
		}
	}
	// Shorter paths win ties of quality: the file near the root is the likelier mention.
	score -= len(text) / 16
	return score, positions, true
}

func subsequence(text, q []rune, from int) ([]int, bool) {
	var positions []int
	qi := 0
	for i := from; i < len(text) && qi < len(q); i++ {
		if text[i] == q[qi] {
			positions = append(positions, i)
			qi++
		}
	}
	return positions, qi == len(q)
}

// boundary reports whether the rune at i starts a word: the first rune, one after a separator, or an
// upper-case letter after a lower-case one.
func boundary(runes []rune, i int) bool {
	if i == 0 {
		return true
	}
	prev, cur := runes[i-1], runes[i]
	switch prev {
	case '/', '_', '-', '.', ' ':
		return true
	}
	return unicode.IsUpper(cur) && unicode.IsLower(prev)
}
