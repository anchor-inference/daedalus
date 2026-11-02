package sessions

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// testNow is the clock every test runs at, so "live" and the times in fixtures are fixed.
var testNow = time.Date(2026, 3, 4, 12, 0, 0, 0, time.UTC)

// testEnv is a home folder of its own, an environment of its own, and the default deny list's
// credential files refused, as the daemon refuses them.
func testEnv(t *testing.T, vars map[string]string) *Env {
	t.Helper()
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	home := filepath.Join(base, "home")
	if err := os.MkdirAll(home, 0o700); err != nil {
		t.Fatal(err)
	}
	return &Env{
		Home:   home,
		Getenv: func(k string) string { return vars[k] },
		GOOS:   "linux",
		Now:    func() time.Time { return testNow },
		Refused: func(p string) bool {
			p = filepath.ToSlash(p)
			for _, deny := range []string{"/.claude/.credentials.json", "/.codex/auth.json", "/.ssh/"} {
				if strings.Contains(p, deny) {
					return true
				}
			}
			return false
		},
	}
}

// writeFile writes a fixture, making its folders.
func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

// jsonl joins records, each a Go value, into JSONL with a final newline.
func jsonl(t *testing.T, records ...any) string {
	t.Helper()
	var b strings.Builder
	for _, r := range records {
		line, err := json.Marshal(r)
		if err != nil {
			t.Fatal(err)
		}
		b.Write(line)
		b.WriteByte('\n')
	}
	return b.String()
}

// readAll pages through a whole session and returns its turns and the last reply.
func readAll(t *testing.T, s *Service, harness, id string, sidechains bool, maxBytes int) ([]Turn, ReadReply) {
	t.Helper()
	var turns []Turn
	var last ReadReply
	for from := int64(0); ; {
		r, err := s.Read(ReadRequest{Harness: harness, ID: id, From: from, MaxBytes: maxBytes, Sidechains: &sidechains})
		if err != nil {
			t.Fatal(err)
		}
		last = r.(ReadReply)
		turns = append(turns, last.Turns...)
		if last.Done {
			return turns, last
		}
		if last.Next <= from {
			t.Fatalf("the read did not advance from %d", from)
		}
		from = last.Next
	}
}

// kinds lists the kinds of a turn's parts, for compact assertions.
func kinds(t Turn) string {
	var out []string
	for _, p := range t.Parts {
		k := p.Kind
		if p.Kind == KindMeta {
			k += ":" + p.MetaType
		}
		out = append(out, k)
	}
	return strings.Join(out, ",")
}

// wire marshals a value and decodes it back into plain JSON, to assert on the shapes the host sees.
func wire(t *testing.T, v any) map[string]any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	return out
}
