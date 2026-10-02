package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

type fakeCLIAnswer struct {
	stdout string
	exit   int
	err    error
}

// fakeCLIRunner answers per program and counts the calls, because a probe that asks twice would
// double the wait on a CLI that is slow to answer.
func fakeCLIRunner(answers map[string]fakeCLIAnswer) (cliRunner, func(string) int) {
	var mu sync.Mutex
	calls := map[string]int{}
	run := func(_ context.Context, name string, _ ...string) (string, int, error) {
		mu.Lock()
		calls[name]++
		mu.Unlock()
		a, ok := answers[name]
		if !ok {
			return "", -1, errors.New("not found")
		}
		return a.stdout, a.exit, a.err
	}
	return run, func(name string) int { mu.Lock(); defer mu.Unlock(); return calls[name] }
}

func writeCredential(t *testing.T, home string, parts ...string) {
	t.Helper()
	path := filepath.Join(append([]string{home}, parts...)...)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{"token":"x"}`), 0o600); err != nil {
		t.Fatal(err)
	}
}

func byID(t *testing.T, logins []CLILogin, id string) CLILogin {
	t.Helper()
	for _, l := range logins {
		if l.ID == id {
			return l
		}
	}
	t.Fatalf("no entry for %s in %v", id, logins)
	return CLILogin{}
}

func TestCLILoginOrderAndParseBranches(t *testing.T) {
	cases := []struct {
		name    string
		answers map[string]fakeCLIAnswer
		id      string
		want    CLILogin
	}{
		{"codex signed in", map[string]fakeCLIAnswer{"codex": {"Logged in", 0, nil}}, "codex", CLILogin{"codex", true, "cli"}},
		{"codex signed out", map[string]fakeCLIAnswer{"codex": {"Not logged in", 1, nil}}, "codex", CLILogin{"codex", false, "cli"}},
		{"claude yes", map[string]fakeCLIAnswer{"claude": {`{"loggedIn": true}`, 0, nil}}, "claude", CLILogin{"claude", true, "cli"}},
		{"claude no", map[string]fakeCLIAnswer{"claude": {`{"loggedIn": false}`, 1, nil}}, "claude", CLILogin{"claude", false, "cli"}},
		{"grok not authenticated", map[string]fakeCLIAnswer{"grok": {"\nnot authenticated\n", 0, nil}}, "grok", CLILogin{"grok", false, "cli"}},
		{"grok logged in", map[string]fakeCLIAnswer{"grok": {"logged in with SuperGrok.\n", 0, nil}}, "grok", CLILogin{"grok", true, "cli"}},
		{"grok key", map[string]fakeCLIAnswer{"grok": {"using XAI_API_KEY\n", 0, nil}}, "grok", CLILogin{"grok", true, "cli"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			run, _ := fakeCLIRunner(c.answers)
			logins := DetectCLILogins(context.Background(), t.TempDir(), run)
			if len(logins) != 3 || logins[0].ID != "codex" || logins[1].ID != "claude" || logins[2].ID != "grok" {
				t.Fatalf("order: %v", logins)
			}
			if got := byID(t, logins, c.id); got != c.want {
				t.Fatalf("got %+v want %+v", got, c.want)
			}
		})
	}
}

func TestCLILoginFallsBackToFile(t *testing.T) {
	home := t.TempDir()
	writeCredential(t, home, ".codex", "auth.json")
	writeCredential(t, home, ".claude", ".credentials.json")
	writeCredential(t, home, ".grok", "auth.json")
	run, calls := fakeCLIRunner(map[string]fakeCLIAnswer{
		"codex":  {"", -1, errors.New("timed out")},
		"claude": {"not json", 0, nil},
		"grok":   {"something odd", 0, nil},
	})
	for _, l := range DetectCLILogins(context.Background(), home, run) {
		if !l.SignedIn || l.How != "file" {
			t.Errorf("%s: %+v", l.ID, l)
		}
	}
	for _, name := range []string{"codex", "claude", "grok"} {
		if calls(name) != 1 {
			t.Errorf("%s was run %d times", name, calls(name))
		}
	}
}

func TestCLILoginNotFoundWithoutFileIsEmpty(t *testing.T) {
	run, _ := fakeCLIRunner(nil)
	for _, l := range DetectCLILogins(context.Background(), t.TempDir(), run) {
		if l.SignedIn || l.How != "" {
			t.Errorf("%+v", l)
		}
	}
}

func TestCLILoginUnreadableOrEmptyFileIsNo(t *testing.T) {
	home := t.TempDir()
	path := filepath.Join(home, ".codex", "auth.json")
	if err := os.MkdirAll(path, 0o755); err != nil { // a directory where the file should be
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(home, ".claude"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, ".claude", ".credentials.json"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	run, _ := fakeCLIRunner(nil)
	logins := DetectCLILogins(context.Background(), home, run)
	if byID(t, logins, "codex").SignedIn || byID(t, logins, "claude").SignedIn {
		t.Fatalf("%+v", logins)
	}
}

func TestCLILoginDefiniteNoBeatsFile(t *testing.T) {
	home := t.TempDir()
	writeCredential(t, home, ".codex", "auth.json")
	run, _ := fakeCLIRunner(map[string]fakeCLIAnswer{"codex": {"Not logged in", 1, nil}})
	if l := byID(t, DetectCLILogins(context.Background(), home, run), "codex"); l.SignedIn || l.How != "cli" {
		t.Fatalf("%+v", l)
	}
}
