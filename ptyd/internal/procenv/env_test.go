package procenv

import "testing"

func TestBuildEnv(t *testing.T) {
	inherited := []string{"PATH=/usr/bin", "DAEDALUS_PTYD_TOKEN=x", "CLAUDECODE=1", "CLAUDE_CONFIG_DIR=/c", "TMUX=/t",
		"TERM_PROGRAM=vscode", "VSCODE_PID=1", "KITTY_WINDOW_ID=2", "STY=s", "TERM=dumb", "LC_ALL=C", "LANG=C",
		"SECRET_KEEP=1", "DAEDALUS_TERMINAL_ID=outer"}
	env := BuildEnv(inherited, []string{"SECRET_*"}, map[string]string{"EXTRA": "1"}, "abc")
	got := map[string]string{}
	for _, kv := range env {
		k, v, _ := stringsCut(kv, '=')
		got[k] = v
	}
	for _, gone := range []string{"DAEDALUS_PTYD_TOKEN", "CLAUDECODE", "TMUX", "TERM_PROGRAM",
		"VSCODE_PID", "KITTY_WINDOW_ID", "STY", "LC_ALL", "SECRET_KEEP"} {
		if _, ok := got[gone]; ok {
			t.Errorf("%s survived", gone)
		}
	}
	want := map[string]string{"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "C.UTF-8",
		"DAEDALUS_TERMINAL_ID": "abc", "EXTRA": "1", "PATH": "/usr/bin", "CLAUDE_CODE_NO_FLICKER": "1",
		"CLAUDE_CONFIG_DIR": "/c"}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s = %q, want %q", k, got[k], v)
		}
	}
	// A UTF-8 locale the user chose is kept.
	env = BuildEnv([]string{"LANG=ru_RU.UTF-8"}, nil, nil, "x")
	for _, kv := range env {
		if kv == "LANG=C.UTF-8" {
			t.Error("a UTF-8 LANG was replaced")
		}
	}
}

func stringsCut(s string, c byte) (string, string, bool) {
	for i := 0; i < len(s); i++ {
		if s[i] == c {
			return s[:i], s[i+1:], true
		}
	}
	return s, "", false
}
