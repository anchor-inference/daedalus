package term

import (
	"slices"
	"testing"
)

// A Claude Code whose configuration lives outside ~/.claude keeps finding it: CLAUDE_CONFIG_DIR is
// the one CLAUDE* name that survives, unless the caller strips it by name.
func TestClaudeConfigDirIsKept(t *testing.T) {
	inherited := []string{"CLAUDE_CONFIG_DIR=/home/someone/.config/claude", "CLAUDECODE=1", "CLAUDE_CODE_ENTRYPOINT=cli"}
	env := BuildEnv(inherited, nil, nil, "t1")
	if !slices.Contains(env, "CLAUDE_CONFIG_DIR=/home/someone/.config/claude") {
		t.Fatalf("CLAUDE_CONFIG_DIR was stripped: %v", env)
	}
	for _, kv := range env {
		if kv == "CLAUDECODE=1" || kv == "CLAUDE_CODE_ENTRYPOINT=cli" {
			t.Fatalf("%s survived", kv)
		}
	}
	for _, strip := range [][]string{{"CLAUDE_CONFIG_DIR"}, {"CLAUDE*"}} {
		for _, kv := range BuildEnv(inherited, strip, nil, "t1") {
			if kv == "CLAUDE_CONFIG_DIR=/home/someone/.config/claude" {
				t.Fatalf("the caller's strip %v did not remove it", strip)
			}
		}
	}
}

func TestSelectedInheritanceExcludesOtherProviderSecrets(t *testing.T) {
	inherited := []string{"PATH=/usr/bin", "HOME=/home/someone", "CLAUDE_CONFIG_DIR=/config/claude", "OPENAI_API_KEY=other", "GH_TOKEN=repo"}
	selected := SelectInheritedEnv(inherited, []string{"PATH", "HOME", "CLAUDE_CONFIG_DIR"})
	env := BuildEnv(selected, nil, nil, "staff")
	for _, item := range []string{"OPENAI_API_KEY=other", "GH_TOKEN=repo"} {
		if slices.Contains(env, item) {
			t.Fatalf("unselected credential survived: %s", item)
		}
	}
	if !slices.Contains(env, "CLAUDE_CONFIG_DIR=/config/claude") {
		t.Fatal("selected CLI configuration was dropped")
	}
	if len(SelectInheritedEnv(inherited, []string{})) != 0 {
		t.Fatal("empty selection inherited daemon variables")
	}
}
