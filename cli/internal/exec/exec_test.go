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
	raw, _ := json.Marshal(params)
	res, err := e.Handle(context.Background(), Request{Method: method, Params: raw, Session: "s1"})
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
