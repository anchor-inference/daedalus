package tui

import (
	"fmt"
	"path/filepath"
	"sort"
	"strings"
)

// toolTitle is the call line of a tool: its display name and the one argument that says what it does,
// as Claude Code writes `Bash(npm test)` or `Update(src/app.go)`.
//
// The host's tools have their own names (Exec, Edit, Find); the call line uses the names a person who
// knows Claude Code reads at a glance where the meaning is the same, and the tool's own name
// everywhere else. Paths are shown relative to the working folder, because the absolute path of a
// file in the folder the person is sitting in is noise.
func toolTitle(name string, input map[string]any, cwd string) (display, arg string) {
	str := func(key string) string {
		if v, ok := input[key]; ok {
			switch x := v.(type) {
			case string:
				return x
			case fmt.Stringer:
				return x.String()
			case nil:
				return ""
			default:
				return fmt.Sprint(x)
			}
		}
		return ""
	}
	path := func(key string) string { return relPath(str(key), cwd) }
	switch name {
	case "Exec", "Bash":
		return "Bash", oneLine(str("command"))
	case "Read":
		return "Read", path("path")
	case "Write":
		return "Write", path("path")
	case "Edit", "MultiEdit":
		return "Update", path("path")
	case "Find", "Glob":
		arg := str("pattern")
		if arg == "" {
			arg = str("query")
		}
		if p := path("path"); p != "" && p != "." {
			arg = fmt.Sprintf("pattern: %q, path: %q", arg, p)
		}
		return "Find", arg
	case "Search", "Grep":
		arg := fmt.Sprintf("pattern: %q", str("pattern"))
		if p := path("path"); p != "" && p != "." {
			arg += fmt.Sprintf(", path: %q", p)
		}
		return "Search", arg
	case "WebFetch":
		return "Fetch", str("url")
	case "WebSearch":
		return "Web Search", fmt.Sprintf("%q", str("query"))
	case "SubAgent", "Task":
		arg := str("description")
		if arg == "" {
			arg = str("title")
		}
		if arg == "" {
			arg = oneLine(str("prompt"))
		}
		return "Task", arg
	case "AskUser", "AskUserQuestion":
		return "Ask", ""
	}
	return name, firstArg(input, cwd)
}

// firstArg is the argument worth showing for a tool this file does not know: a path, a command, a
// query or a url if there is one, else the first string argument in name order.
func firstArg(input map[string]any, cwd string) string {
	for _, key := range []string{"path", "file", "command", "query", "url", "name", "title", "id"} {
		if v, ok := input[key].(string); ok && v != "" {
			if key == "path" || key == "file" {
				return relPath(v, cwd)
			}
			return oneLine(v)
		}
	}
	keys := make([]string, 0, len(input))
	for k := range input {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if v, ok := input[k].(string); ok && v != "" {
			return oneLine(v)
		}
	}
	return ""
}

// A path that sits under the folder in slash form is cut textually first. The server writes POSIX
// paths, and filepath on Windows does not call "/work/project/x" absolute, so the call lines kept
// the whole path there and every frame with a tool in it differed on the Windows runner. A native
// Windows path still goes through filepath.Rel.
func relPath(p, cwd string) string {
	if p == "" || cwd == "" {
		return p
	}
	slashCwd := strings.TrimSuffix(filepath.ToSlash(cwd), "/")
	if slashP := filepath.ToSlash(p); strings.HasPrefix(slashP, slashCwd+"/") {
		return slashP[len(slashCwd)+1:]
	}
	if !filepath.IsAbs(p) {
		return p
	}
	if rel, err := filepath.Rel(cwd, p); err == nil && !strings.HasPrefix(rel, "..") {
		return filepath.ToSlash(rel)
	}
	return p
}

// oneLine is the first line of text, marked with an ellipsis when there was more.
func oneLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return strings.TrimSpace(s[:i]) + " …"
	}
	return s
}

// toolSummary is the one-line result Claude Code shows instead of the output for tools whose output
// is the file itself, or "" when the output should be shown (collapsed).
func toolSummary(name string, input map[string]any, output string, change *FileChange) string {
	lines := countLines(output)
	switch name {
	case "Read":
		if lines == 0 {
			return "Read 0 lines"
		}
		return fmt.Sprintf("Read %d %s", lines, plural(lines, "line", "lines"))
	case "Write":
		n := lines
		if change != nil {
			n = countLines(change.After)
		} else if content, ok := input["content"].(string); ok {
			n = countLines(content)
		}
		return fmt.Sprintf("Wrote %d %s to %s", n, plural(n, "line", "lines"), filepath.Base(stringArg(input, "path")))
	case "Find", "Glob":
		n := countLines(strings.TrimSpace(output))
		return fmt.Sprintf("Found %d %s", n, plural(n, "file", "files"))
	case "Search", "Grep":
		n := countLines(strings.TrimSpace(output))
		return fmt.Sprintf("Found %d %s", n, plural(n, "line", "lines"))
	case "WebFetch":
		return fmt.Sprintf("Received %s", formatBytes(len(output)))
	}
	return ""
}

func stringArg(input map[string]any, key string) string {
	if v, ok := input[key].(string); ok {
		return v
	}
	return ""
}

func countLines(s string) int {
	if s == "" {
		return 0
	}
	n := strings.Count(s, "\n")
	if !strings.HasSuffix(s, "\n") {
		n++
	}
	return n
}

func plural(n int, one, many string) string {
	if n == 1 {
		return one
	}
	return many
}

func formatBytes(n int) string {
	switch {
	case n < 1024:
		return fmt.Sprintf("%d B", n)
	case n < 1024*1024:
		return fmt.Sprintf("%.1f KB", float64(n)/1024)
	default:
		return fmt.Sprintf("%.1f MB", float64(n)/(1024*1024))
	}
}
