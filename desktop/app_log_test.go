package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// What the launcher says is kept on disk as well as on its page: once the window it printed to was
// closed, there was nothing left of what it had seen.
func TestTheLauncherKeepsItsCommentaryInAFile(t *testing.T) {
	p := fixtureData(t)
	app := NewApp(p)
	app.log("waiting for the app to answer")
	app.log("the app is up at %s", "http://127.0.0.1:8765/app/")
	body, err := os.ReadFile(filepath.Join(p.RuntimeLogs, launcherLog))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(body)), "\n")
	if len(lines) != 2 || !strings.HasSuffix(lines[0], "waiting for the app to answer") || !strings.Contains(lines[1], "the app is up at http://127.0.0.1:8765/app/") {
		t.Fatalf("the log holds %q", body)
	}
}
