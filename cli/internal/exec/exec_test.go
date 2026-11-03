package exec

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func newExecutor(t *testing.T) (*Executor, string) {
	t.Helper()
	// The home folder may not be a root, so the test's home is one level above its project.
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	root := filepath.Join(home, "project")
	if err := os.Mkdir(root, 0o755); err != nil {
		t.Fatal(err)
	}
	e, err := New(nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := e.Bind("s1", root); err != nil {
		t.Fatal(err)
	}
	return e, root
}

func call(t *testing.T, e *Executor, method string, params map[string]any) (map[string]any, *Error) {
	t.Helper()
	return callAs(t, e, "s1", method, params)
}

func callAs(t *testing.T, e *Executor, session, method string, params map[string]any) (map[string]any, *Error) {
	t.Helper()
	raw, _ := json.Marshal(params)
	res, err := e.Handle(context.Background(), Request{Method: method, Params: raw, Session: session})
	if err != nil {
		var own *Error
		if !errors.As(err, &own) {
			t.Fatalf("%s: a failure that is not an *Error: %v", method, err)
		}
		return nil, own
	}
	raw, _ = json.Marshal(res)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return out, nil
}

func reasonOf(e *Error) string {
	if d, ok := e.Data.(map[string]string); ok {
		return d["reason"]
	}
	return ""
}

func TestWriteIsWholeOrNothing(t *testing.T) {
	e, root := newExecutor(t)
	target := filepath.Join(root, "dir", "a.txt")
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := call(t, e, "fs.write", map[string]any{"path": target, "offset": 0, "data_b64": []byte("new ")}); err != nil {
		t.Fatal(err)
	}
	// Mid-write the old content is still whole under its name.
	if got, _ := os.ReadFile(target); string(got) != "old" {
		t.Fatalf("a piece reached the target before the last: %q", got)
	}
	res, err := call(t, e, "fs.write", map[string]any{"path": target, "offset": 4, "data_b64": []byte("content"), "final": true})
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(target); string(got) != "new content" {
		t.Fatalf("after the final piece: %q", got)
	}
	if res["created"] != false || res["size"] != float64(11) {
		t.Errorf("result: %v", res)
	}
	if runtime.GOOS != "windows" {
		if st, _ := os.Stat(target); st.Mode().Perm() != 0o755 {
			t.Errorf("the file lost its mode: %v", st.Mode())
		}
	}
	// A piece that does not continue the write is refused.
	if _, err := call(t, e, "fs.write", map[string]any{"path": target, "offset": 99, "data_b64": []byte("x")}); err == nil {
		t.Error("a write continued at the wrong offset")
	}
	// A new file in a new folder.
	fresh := filepath.Join(root, "new", "deep", "b.txt")
	res, err = call(t, e, "fs.write", map[string]any{"path": fresh, "offset": 0, "data_b64": []byte("b"), "final": true})
	if err != nil || res["created"] != true {
		t.Fatalf("%v %v", res, err)
	}
	read, err := call(t, e, "fs.read", map[string]any{"path": fresh})
	if err != nil {
		t.Fatal(err)
	}
	if read["data_b64"] != "Yg==" {
		t.Errorf("read back: %v", read)
	}
}

func TestJail(t *testing.T) {
	e, root := newExecutor(t)
	outside := filepath.Dir(root)
	for _, p := range []string{filepath.Join(outside, "x.txt"), filepath.Join(root, "..", "x.txt")} {
		_, err := call(t, e, "fs.write", map[string]any{"path": p, "offset": 0, "data_b64": []byte("x"), "final": true})
		if err == nil || err.Code != CodeForbidden || reasonOf(err) != ReasonOutsideRoot {
			t.Errorf("%s: %v", p, err)
		}
	}
	if runtime.GOOS != "windows" {
		// A link inside the folder to outside it does not lead out.
		if err := os.Symlink(outside, filepath.Join(root, "out")); err != nil {
			t.Fatal(err)
		}
		_, err := call(t, e, "fs.write", map[string]any{"path": filepath.Join(root, "out", "y.txt"), "offset": 0, "data_b64": []byte("y"), "final": true})
		if err == nil || reasonOf(err) != ReasonOutsideRoot {
			t.Errorf("written through a link: %v", err)
		}
		if _, err := os.Stat(filepath.Join(outside, "y.txt")); err == nil {
			t.Error("the file landed outside the folder")
		}
	}
	// The deny list holds inside the folder too.
	_, err := call(t, e, "fs.read", map[string]any{"path": filepath.Join(root, ".ssh", "id_ed25519")})
	if err == nil || err.Code != CodeForbidden {
		t.Errorf("a key was readable: %v", err)
	}
	// A command's working folder is jailed as well.
	_, err = call(t, e, "exec.run", map[string]any{"argv": []string{"sh", "-c", "pwd"}, "cwd": outside})
	if err == nil || reasonOf(err) != ReasonOutsideRoot {
		t.Errorf("exec outside the folder: %v", err)
	}
	// A session this terminal holds no folder for.
	raw, _ := json.Marshal(map[string]any{"path": filepath.Join(root, "a")})
	_, herr := e.Handle(context.Background(), Request{Method: "fs.stat", Params: raw, Session: "other"})
	var own *Error
	if !errors.As(herr, &own) || reasonOf(own) != ReasonOutsideRoot {
		t.Errorf("an unknown session: %v", herr)
	}
	// The home folder itself cannot be a session's folder.
	if err := e.CheckRoot(outside); err == nil {
		t.Error("the home folder was accepted as a root")
	}
}

func TestExecRunsInTheFolder(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh")
	}
	e, root := newExecutor(t)
	res, err := call(t, e, "exec.run", map[string]any{"argv": []string{"sh", "-c", "pwd; echo err >&2; exit 3"}})
	if err != nil {
		t.Fatal(err)
	}
	real, _ := filepath.EvalSymlinks(root)
	if out := strings.TrimSpace(res["stdout"].(string)); out != root && out != real {
		t.Errorf("ran in %q, want %q", out, root)
	}
	if res["exit_code"] != float64(3) || strings.TrimSpace(res["stderr"].(string)) != "err" {
		t.Errorf("result: %v", res)
	}
	if _, err := call(t, e, "fs.nope", map[string]any{}); err == nil || err.Code != CodeMethodNotFound {
		t.Errorf("an unknown method: %v", err)
	}
	made, err := call(t, e, "fs.mkdir", map[string]any{"path": filepath.Join(root, "m", "n")})
	if err != nil || made["created"] != true {
		t.Errorf("mkdir: %v %v", made, err)
	}
}

func TestFindAndSearch(t *testing.T) {
	e, root := newExecutor(t)
	files := map[string]string{
		"main.go":               "package main\nfunc main() {}\n",
		"pkg/util/util.go":      "package util\n// TODO: tidy\nfunc Tidy() {}\n",
		"pkg/util/util_test.go": "package util\n",
		"README.md":             "todo: write\n",
		"node_modules/x/a.go":   "package x // TODO\n",
		".hidden/h.go":          "package h // TODO\n",
		"bin.dat":               "TODO\x00\x01",
		".ssh/id_ed25519":       "TODO secret\n",
	}
	for name, body := range files {
		p := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	res, err := call(t, e, "fs.find", map[string]any{"path": root, "pattern": "*.go", "limit": 100})
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, p := range res["paths"].([]any) {
		got = append(got, filepath.ToSlash(p.(string)))
	}
	want := []string{".hidden/h.go", "main.go", "pkg/util/util.go", "pkg/util/util_test.go"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("find *.go: %v, want %v", got, want)
	}
	// A pattern on the relative path, where `*` crosses folders as Python's fnmatch does.
	res, _ = call(t, e, "fs.find", map[string]any{"path": root, "pattern": "pkg/*_test.go"})
	if len(res["paths"].([]any)) != 1 {
		t.Errorf("find pkg/*_test.go: %v", res["paths"])
	}
	res, _ = call(t, e, "fs.find", map[string]any{"path": root, "pattern": "*.go", "limit": 2})
	if len(res["paths"].([]any)) != 2 || res["truncated"] != true {
		t.Errorf("a limited find: %v", res)
	}

	res, err = call(t, e, "fs.search", map[string]any{"path": root, "pattern": "TODO", "case_insensitive": true, "limit": 50})
	if err != nil {
		t.Fatal(err)
	}
	var lines []string
	for _, l := range res["lines"].([]any) {
		lines = append(lines, l.(string))
	}
	joined := strings.Join(lines, "\n")
	if !strings.Contains(joined, filepath.Join(root, "pkg", "util", "util.go")+":2:// TODO: tidy") || !strings.Contains(joined, "README.md:1:todo: write") {
		t.Errorf("search: %q", joined)
	}
	for _, skipped := range []string{"node_modules", ".hidden", "bin.dat", "id_ed25519"} {
		if strings.Contains(joined, skipped) {
			t.Errorf("search reached %s: %q", skipped, joined)
		}
	}
	res, _ = call(t, e, "fs.search", map[string]any{"path": root, "pattern": "TODO", "glob": "*.md"})
	if n := len(res["lines"].([]any)); n != 0 {
		t.Errorf("case-sensitive TODO in *.md found %d lines", n)
	}
	if _, err := call(t, e, "fs.search", map[string]any{"path": root, "pattern": "("}); err == nil || err.Code != CodeInvalidParams {
		t.Errorf("a broken pattern: %v", err)
	}
	if _, err := call(t, e, "fs.find", map[string]any{"path": filepath.Dir(root), "pattern": "*"}); err == nil || reasonOf(err) != ReasonOutsideRoot {
		t.Errorf("find outside the folder: %v", err)
	}
}

func TestFnmatchIsPythons(t *testing.T) {
	cases := []struct {
		pattern, name string
		match         bool
	}{
		{"*.go", "a/b/c.go", true},
		{"a?c", "abc", true},
		{"[!a]bc", "xbc", true},
		{"[!a]bc", "abc", false},
		{"[ab]*", "b.txt", true},
		{"a.b", "aXb", false},
		{"[", "[", true},
	}
	for _, c := range cases {
		re, err := fnmatch(c.pattern)
		if err != nil {
			t.Fatalf("%q: %v", c.pattern, err)
		}
		if re.MatchString(c.name) != c.match {
			t.Errorf("fnmatch(%q, %q) = %v", c.name, c.pattern, !c.match)
		}
	}
}
