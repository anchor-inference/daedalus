package procenv

import (
	"slices"
	"strings"
	"testing"
)

// The Windows rules for a terminal's environment and its program, checked on every platform: the
// daemon's Windows build is cross-compiled, and these are the parts of it that can be proven here.

func withFoldedEnv(t *testing.T) {
	t.Helper()
	was := foldEnv
	foldEnv = true
	t.Cleanup(func() { foldEnv = was })
}

// A launch's PATH replaces the inherited Path instead of standing beside it, and the name keeps the
// spelling Windows gave it.
func TestWindowsVariableNamesAreOneWhateverTheirCase(t *testing.T) {
	withFoldedEnv(t)
	inherited := []string{`Path=C:\Windows\system32`, `SystemRoot=C:\Windows`, "claude_code_entrypoint=cli", "Tmux_Pane=%1"}
	env := BuildEnv(inherited, []string{"systemroot"}, map[string]string{"PATH": `C:\tools;C:\Windows\system32`}, "t1")
	var paths []string
	for _, kv := range env {
		k, _, _ := strings.Cut(kv, "=")
		if strings.EqualFold(k, "PATH") {
			paths = append(paths, kv)
		}
		if strings.EqualFold(k, "SYSTEMROOT") || strings.EqualFold(k, "CLAUDE_CODE_ENTRYPOINT") || strings.EqualFold(k, "TMUX_PANE") {
			t.Errorf("%s survived", kv)
		}
	}
	if !slices.Equal(paths, []string{`Path=C:\tools;C:\Windows\system32`}) {
		t.Errorf("PATH entries: %v", paths)
	}
	if !slices.Contains(env, "DAEDALUS_TERMINAL_ID=t1") {
		t.Errorf("no terminal id: %v", env)
	}
}

// On Unix the names stay case-sensitive: "Path" and "PATH" are two variables there.
func TestUnixVariableNamesKeepTheirCase(t *testing.T) {
	was := foldEnv
	foldEnv = false
	t.Cleanup(func() { foldEnv = was })
	env := BuildEnv([]string{"Path=/a"}, nil, map[string]string{"PATH": "/b"}, "t1")
	if !slices.Contains(env, "Path=/a") || !slices.Contains(env, "PATH=/b") {
		t.Errorf("env: %v", env)
	}
}

func TestAWindowsProgramIsFoundAsCmdDoes(t *testing.T) {
	files := map[string]bool{
		`C:\Program Files\PowerShell\7\pwsh.exe`:                    true,
		`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`: true,
		`C:\Users\someone\AppData\Roaming\npm\claude.cmd`:           true,
		`C:\Users\someone\AppData\Roaming\npm\claude`:               true, // npm's shell script beside the .cmd
		`C:\work\tools\run.bat`:                                     true,
	}
	exists := func(p string) bool { return files[p] }
	env := []string{`Path=C:\Program Files\PowerShell\7\;C:\Windows\System32\WindowsPowerShell\v1.0;"C:\Users\someone\AppData\Roaming\npm"`, "PATHEXT=.COM;.EXE;.BAT;.CMD"}
	cases := []struct {
		name, want string
		ok         bool
	}{
		{"pwsh.exe", `C:\Program Files\PowerShell\7\pwsh.exe`, true},
		{"powershell.exe", `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`, true},
		// The script without an extension is not what Windows can run; the .cmd beside it is.
		{"claude", `C:\Users\someone\AppData\Roaming\npm\claude.cmd`, true},
		{`tools\run`, `C:\work\tools\run.bat`, true},
		{`C:\work\tools\run.bat`, `C:\work\tools\run.bat`, true},
		{"missing", "", false},
	}
	for _, c := range cases {
		got, ok := lookPathWindows(c.name, env, `C:\work`, exists)
		if ok != c.ok || got != c.want {
			t.Errorf("%s: got %q %v, want %q %v", c.name, got, ok, c.want, c.ok)
		}
	}
	// Without PATHEXT in the environment, the default list applies.
	if got, ok := lookPathWindows("claude", []string{`PATH=C:\Users\someone\AppData\Roaming\npm`}, `C:\`, exists); !ok || !strings.HasSuffix(got, "claude.cmd") {
		t.Errorf("default PATHEXT: %q %v", got, ok)
	}
	// The current directory is not searched.
	if _, ok := lookPathWindows("run.bat", []string{"PATH="}, `C:\work\tools`, exists); ok {
		t.Error("found a program in the current directory without a path")
	}
}
