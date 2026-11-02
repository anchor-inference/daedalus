package files

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func paths(matches []Match) []string {
	out := make([]string, len(matches))
	for i, m := range matches {
		out[i] = m.Path
	}
	return out
}

func touch(t *testing.T, root string, rel ...string) {
	t.Helper()
	for _, r := range rel {
		p := filepath.Join(root, filepath.FromSlash(r))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestMatchRanking(t *testing.T) {
	ix := newIndex([]string{
		"README.md",
		"cli/internal/tui/model.go",
		"cli/internal/tui/model_test.go",
		"miniapp/src/models/deep/extra/long/path/model.ts",
		"daedalus/stores/database.py",
		"docs/modules/overview.md",
	}, 0)
	got := paths(ix.Match("model", 3))
	if got[0] != "cli/internal/tui/model.go" {
		t.Fatalf("model.go should rank first, got %v", got)
	}
	got = paths(ix.Match("dbpy", 1))
	if len(got) != 1 || got[0] != "daedalus/stores/database.py" {
		t.Fatalf("got %v", got)
	}
	if m := ix.Match("zzz", 5); len(m) != 0 {
		t.Fatalf("no match expected, got %v", paths(m))
	}
	m := ix.Match("README", 1)
	if m[0].Path != "README.md" || len(m[0].Positions) != 6 || m[0].Positions[0] != 0 {
		t.Fatalf("case-insensitive match with positions, got %+v", m[0])
	}
}

func TestMatchPositionsLandOnTheName(t *testing.T) {
	ix := newIndex([]string{"tools/terminal/term.go"}, 0)
	m := ix.Match("term", 1)
	if len(m) != 1 {
		t.Fatalf("no match")
	}
	// The name "term.go" starts at rune 15; the earlier "terminal" directory must not take the match.
	if m[0].Positions[0] != 15 {
		t.Fatalf("positions = %v", m[0].Positions)
	}
}

func TestEmptyQueryListsShallowDirectoriesFirst(t *testing.T) {
	ix := newIndex([]string{"b.go", "a/x.go", "a/b/y.go", "c.go"}, 0)
	got := paths(ix.Match("", 0))
	want := []string{"a/", "b.go", "c.go", "a/b/", "a/x.go", "a/b/y.go"}
	if len(got) != len(want) {
		t.Fatalf("got %v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got %v want %v", got, want)
		}
	}
	if n := len(ix.Match("", 2)); n != 2 {
		t.Fatalf("limit ignored: %d", n)
	}
}

func TestBuildWalksAndSkips(t *testing.T) {
	root := t.TempDir()
	touch(t, root, "main.go", "pkg/util.go", "node_modules/dep/index.js", ".hidden/secret", "__pycache__/x.pyc")
	t.Setenv("GIT_CEILING_DIRECTORIES", filepath.Dir(root))
	ix, err := Build(context.Background(), root, 0)
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]bool{}
	for _, p := range paths(ix.Match("", 0)) {
		got[p] = true
	}
	for _, want := range []string{"main.go", "pkg/", "pkg/util.go"} {
		if !got[want] {
			t.Errorf("missing %s in %v", want, got)
		}
	}
	for _, absent := range []string{"node_modules/dep/index.js", ".hidden/secret", "__pycache__/x.pyc"} {
		if got[absent] {
			t.Errorf("%s should be skipped", absent)
		}
	}
	ix, err = Build(context.Background(), root, 2)
	if err != nil || ix.Len() > 2 {
		t.Fatalf("limit ignored: %d %v", ix.Len(), err)
	}
}

func TestBuildUsesGitIgnores(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	root := t.TempDir()
	run := func(args ...string) {
		cmd := exec.Command("git", args...)
		cmd.Dir = root
		cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null")
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	run("init", "-q")
	touch(t, root, "tracked.go", "untracked.go", "out/ignored.bin", ".github/workflow.yml")
	if err := os.WriteFile(filepath.Join(root, ".gitignore"), []byte("out/\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run("add", "tracked.go")
	ix, err := Build(context.Background(), root, 0)
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]bool{}
	for _, p := range paths(ix.Match("", 0)) {
		got[p] = true
	}
	// Git's own view wins: an untracked file is listed, an ignored one is not, and a hidden folder
	// git tracks (unlike the walk) is offered.
	for _, want := range []string{"tracked.go", "untracked.go", ".gitignore", ".github/workflow.yml"} {
		if !got[want] {
			t.Errorf("missing %s in %v", want, got)
		}
	}
	if got["out/ignored.bin"] {
		t.Errorf("ignored file listed")
	}
}
