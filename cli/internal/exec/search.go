package exec

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"
)

// fs.find and fs.search: the server's Glob and Grep, served here so they work on a machine without a
// POSIX shell (the server's fallbacks are find, sed, rg and grep) and cost one round trip on one with.
// Their answers are shaped as the server's own file interface returns them (daedalus/host/filesystem.py
// LocalFS.find and .search), so a tool reads the same thing whichever side did the work.

// skippedDirs are never walked into, as the server's own walk skips them.
var skippedDirs = map[string]bool{".git": true, "node_modules": true, ".venv": true, "__pycache__": true}

const (
	defaultFindLimit = 1000
	maxFindLimit     = 10000
	defaultHits      = 100
	maxHits          = 5000
	// walkBudget and walkEntries bound one walk: a folder with a million files must answer, cut,
	// rather than hold the call until the server gives up on it.
	walkBudget  = 20 * time.Second
	walkEntries = 500000
	// searchFileMax is the largest file searched; past it a file is a build product or data, not code.
	searchFileMax = 8 << 20
	// hitChars and hitBytes keep one answer inside a relay message.
	hitChars = 1000
	hitBytes = 512 << 10
)

var errWalkDone = errors.New("walk done")

// fnmatch compiles a pattern as Python's fnmatch reads it: `*` matches anything, `/` included, `?`
// one character, `[...]` a class, `[!...]` its complement; case-blind on Windows as there.
func fnmatch(pattern string) (*regexp.Regexp, error) {
	var b strings.Builder
	if runtime.GOOS == "windows" {
		b.WriteString("(?i)")
	}
	b.WriteString(`(?s)\A`)
	rs := []rune(pattern)
	for i := 0; i < len(rs); i++ {
		switch c := rs[i]; c {
		case '*':
			b.WriteString(".*")
		case '?':
			b.WriteString(".")
		case '[':
			j := i + 1
			if j < len(rs) && rs[j] == '!' {
				j++
			}
			if j < len(rs) && rs[j] == ']' {
				j++
			}
			for j < len(rs) && rs[j] != ']' {
				j++
			}
			if j >= len(rs) {
				b.WriteString(`\[`)
				continue
			}
			class := string(rs[i+1 : j])
			class = strings.ReplaceAll(class, `\`, `\\`)
			if strings.HasPrefix(class, "!") {
				class = "^" + class[1:]
			} else if strings.HasPrefix(class, "^") {
				class = `\` + class
			}
			b.WriteString("[" + class + "]")
			i = j
		default:
			b.WriteString(regexp.QuoteMeta(string(c)))
		}
	}
	b.WriteString(`\z`)
	return regexp.Compile(b.String())
}

// walkFiles calls visit with every file under dir, skipping skippedDirs (and, with hidden false,
// every name starting with a dot), within the walk's budget. It reports whether the walk was cut.
func walkFiles(ctx context.Context, dir string, hidden bool, visit func(path, rel string) bool) bool {
	deadline := time.Now().Add(walkBudget)
	seen := 0
	cut := false
	_ = filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			if d != nil && d.IsDir() && path != dir {
				return fs.SkipDir
			}
			return nil
		}
		seen++
		if seen > walkEntries || time.Now().After(deadline) || ctx.Err() != nil {
			cut = true
			return errWalkDone
		}
		name := d.Name()
		if d.IsDir() {
			if path != dir && (skippedDirs[name] || (!hidden && strings.HasPrefix(name, "."))) {
				return fs.SkipDir
			}
			return nil
		}
		if !hidden && strings.HasPrefix(name, ".") {
			return nil
		}
		rel, err := filepath.Rel(dir, path)
		if err != nil {
			return nil
		}
		if !visit(path, rel) {
			return errWalkDone
		}
		return nil
	})
	return cut
}

// find is fs.find {path, pattern, limit} → {paths: [relative paths], truncated}: the files whose path
// relative to path, or whose name, matches the pattern.
func (e *Executor) find(ctx context.Context, root, dir, pattern string, limit int) (any, error) {
	if err := e.jail(root, dir); err != nil {
		return nil, err
	}
	if limit <= 0 {
		limit = defaultFindLimit
	}
	limit = min(limit, maxFindLimit)
	re, err := fnmatch(pattern)
	if err != nil {
		return nil, &Error{Code: CodeInvalidParams, Message: "pattern: " + err.Error()}
	}
	paths := []string{}
	full := false
	cut := walkFiles(ctx, filepath.Clean(dir), true, func(path, rel string) bool {
		if e.fs.Refused(path) {
			return true
		}
		if re.MatchString(rel) || re.MatchString(filepath.ToSlash(rel)) || re.MatchString(filepath.Base(rel)) {
			paths = append(paths, rel)
			if len(paths) >= limit {
				full = true
				return false
			}
		}
		return true
	})
	sort.Strings(paths)
	return map[string]any{"paths": paths, "truncated": cut || full}, nil
}

// search is fs.search {path, pattern, glob?, case_insensitive, limit} → {lines: ["path:line:text"],
// truncated}: the lines matching a regular expression, at most limit of them, in the form ripgrep
// prints with -n and no heading. Hidden files, binary files and files over searchFileMax are left out,
// as ripgrep leaves the first two out; a file outside the folder by its links or on the deny list is
// never opened.
func (e *Executor) search(ctx context.Context, root, dir, pattern, glob string, caseInsensitive bool, limit int) (any, error) {
	if err := e.jail(root, dir); err != nil {
		return nil, err
	}
	if limit <= 0 {
		limit = defaultHits
	}
	limit = min(limit, maxHits)
	expr := pattern
	if caseInsensitive {
		expr = "(?i)" + expr
	}
	re, err := regexp.Compile(expr)
	if err != nil {
		return nil, &Error{Code: CodeInvalidParams, Message: "pattern: " + err.Error()}
	}
	var only *regexp.Regexp
	if glob != "" {
		if only, err = fnmatch(glob); err != nil {
			return nil, &Error{Code: CodeInvalidParams, Message: "glob: " + err.Error()}
		}
	}
	lines := []string{}
	size := 0
	full := false
	searchOne := func(path string) bool {
		if e.jail(root, path) != nil {
			return true
		}
		st, err := os.Stat(path)
		if err != nil || !st.Mode().IsRegular() || st.Size() > searchFileMax {
			return true
		}
		data, err := os.ReadFile(path)
		if err != nil || bytes.IndexByte(data[:min(len(data), 8192)], 0) >= 0 {
			return true
		}
		scanner := bufio.NewScanner(bytes.NewReader(data))
		scanner.Buffer(make([]byte, 0, 64<<10), searchFileMax)
		n := 0
		for scanner.Scan() {
			n++
			text := scanner.Text()
			if !re.MatchString(text) {
				continue
			}
			if len(text) > hitChars {
				text = strings.ToValidUTF8(text[:hitChars], "") + "…"
			}
			line := path + ":" + strconv.Itoa(n) + ":" + strings.ToValidUTF8(text, "�")
			lines = append(lines, line)
			size += len(line)
			if len(lines) >= limit || size >= hitBytes {
				full = true
				return false
			}
		}
		return true
	}
	clean := filepath.Clean(dir)
	cut := false
	if st, err := os.Stat(clean); err == nil && !st.IsDir() {
		searchOne(clean)
	} else {
		cut = walkFiles(ctx, clean, false, func(path, rel string) bool {
			if only != nil && !only.MatchString(filepath.Base(rel)) && !only.MatchString(filepath.ToSlash(rel)) {
				return true
			}
			return searchOne(path)
		})
	}
	return map[string]any{"lines": lines, "truncated": cut || full}, nil
}
