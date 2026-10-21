package audit

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRecordsAreAppendedAndRotated(t *testing.T) {
	path := filepath.Join(t.TempDir(), "audit.jsonl")
	l, err := Open(path, 400)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	for i := 0; i < 10; i++ {
		if err := l.Write(Record{Role: "host", Event: "act", Action: "type", TextLength: 12}); err != nil {
			t.Fatal(err)
		}
	}
	st, err := os.Stat(path)
	if err != nil || st.Size() > 400 || st.Mode().Perm() != 0o600 {
		t.Fatalf("current file: %v %v", st, err)
	}
	old, err := os.ReadFile(path + ".1")
	if err != nil || !strings.Contains(string(old), `"text_length":12`) {
		t.Fatalf("rotated file: %q %v", old, err)
	}
	var none *Log
	if err := none.Write(Record{}); err != nil {
		t.Fatal("a nil log refused a write")
	}
}
