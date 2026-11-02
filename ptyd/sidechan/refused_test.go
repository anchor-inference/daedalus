//go:build unix

package sidechan

import (
	"os"
	"path/filepath"
	"testing"
)

// The session reader opens files outside the roots, from each program's own store, and leans on
// Refused for the credentials those stores sit beside.
func TestRefusedCoversEveryProgramsLoginAndLinksToOne(t *testing.T) {
	tr := newTree(t)
	for _, rel := range []string{
		".claude/.credentials.json", ".codex/auth.json", ".codex/accounts.json", ".codex/accounts/a.json",
		".grok/auth.json", ".gemini/oauth_creds.json", ".gemini/google_accounts.json", ".qwen/oauth_creds.json",
		".local/share/opencode/auth.json", ".pi/agent/auth.json", ".cursor/chats/x/store.db", ".ssh/id_ed25519",
	} {
		if !tr.fs.Refused(filepath.Join(tr.home, rel)) {
			t.Errorf("%s is not refused", rel)
		}
	}
	for _, rel := range []string{".claude/projects/-p/s.jsonl", ".codex/sessions/2026/01/01/rollout-x.jsonl", ".codex/session_index.jsonl"} {
		if tr.fs.Refused(filepath.Join(tr.home, rel)) {
			t.Errorf("%s is refused", rel)
		}
	}
	if !tr.fs.Refused(filepath.Join(tr.state, "token")) {
		t.Error("the daemon's own state is not refused")
	}
	// A link that looks like a session and resolves to a login.
	link := filepath.Join(tr.transcripts, "looks-like-a-session.jsonl")
	if err := os.Symlink(filepath.Join(tr.home, ".claude", ".credentials.json"), link); err != nil {
		t.Fatal(err)
	}
	if !tr.fs.Refused(link) {
		t.Error("a link to the credentials is not refused")
	}
}
