package sessions

import (
	"os"
	"path/filepath"
	"strings"
)

// procsConfirm looks in the process table for a process of the program that has one of the
// session's files open or works in its folder. known is false when the table cannot be read, and
// then the time of the last record decides alone. Claude Code opens its transcript only to append,
// so the folder is the usual evidence; Codex keeps its rollout open.
func procsConfirm(e *Env, names, files []string, cwd string) (confirmed, known bool) {
	if e.ProcRoot == "" {
		return false, false
	}
	entries, err := os.ReadDir(e.ProcRoot)
	if err != nil {
		return false, false
	}
	want := map[string]bool{}
	for _, f := range files {
		want[f] = true
		if r, err := filepath.EvalSymlinks(f); err == nil {
			want[r] = true
		}
	}
	for _, de := range entries {
		pid := de.Name()
		if pid == "" || pid[0] < '0' || pid[0] > '9' {
			continue
		}
		dir := filepath.Join(e.ProcRoot, pid)
		if !isProgram(dir, names) {
			continue
		}
		if cwd != "" {
			if c, err := os.Readlink(filepath.Join(dir, "cwd")); err == nil && NormPath(c) == cwd {
				return true, true
			}
		}
		fds, err := os.ReadDir(filepath.Join(dir, "fd"))
		if err != nil {
			continue
		}
		for _, fd := range fds {
			if target, err := os.Readlink(filepath.Join(dir, "fd", fd.Name())); err == nil && want[target] {
				return true, true
			}
		}
	}
	return false, true
}

// isProgram reports a process whose name, or the program its command line runs (a script run by
// node names itself second), is one of names.
func isProgram(dir string, names []string) bool {
	var candidates []string
	if comm, err := os.ReadFile(filepath.Join(dir, "comm")); err == nil {
		candidates = append(candidates, strings.TrimSpace(string(comm)))
	}
	if cmd, err := os.ReadFile(filepath.Join(dir, "cmdline")); err == nil {
		args := strings.Split(string(cmd), "\x00")
		for i := 0; i < len(args) && i < 2; i++ {
			candidates = append(candidates, filepath.Base(args[i]))
		}
	}
	for _, c := range candidates {
		for _, n := range names {
			if c == n || strings.HasPrefix(c, n+"-") || strings.HasPrefix(c, n+".") {
				return true
			}
		}
	}
	return false
}
