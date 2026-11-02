package sidechan

import "testing"

// The Windows rules of the side channels, checked on every platform.

func TestWindowsPathsAreComparedWithoutCase(t *testing.T) {
	cases := []struct {
		dir, p string
		want   bool
	}{
		{`C:\Users\someone\proj`, `c:\users\SOMEONE\Proj\src\a.go`, true},
		{`C:\Users\someone\proj`, `C:\Users\someone\project`, false},
		{`C:\`, `C:\Users\someone`, true},
		{`C:\`, `D:\x`, false},
		{`\\host\share\`, `\\HOST\share\x`, true},
	}
	for _, c := range cases {
		if got := underPath(c.dir, c.p, '\\', true); got != c.want {
			t.Errorf("under(%s, %s) = %v", c.dir, c.p, got)
		}
	}
	// Without folding, as on Unix, the case matters.
	if underPath("/home/someone", "/HOME/someone/x", '/', false) {
		t.Error("unix paths compared without case")
	}
	if !underPath("/", "/x", '/', false) {
		t.Error("/ does not hold /x")
	}
}

// A deny rule written for ".ssh" holds for ".SSH" where names have no case.
func TestTheDenyListHoldsWhateverTheCase(t *testing.T) {
	was := foldPaths
	foldPaths = true
	t.Cleanup(func() { foldPaths = was })
	f, err := NewFS(nil, nil, nil, `C:\Users\someone`)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{`C:\Users\someone\.SSH\id_ed25519`, `C:\Users\someone\.ssh\config`, `c:\users\someone\.Aws\credentials`} {
		if !f.denied(p) {
			t.Errorf("%s is not denied", p)
		}
	}
	if f.denied(`C:\Users\someone\proj\ssh.md`) {
		t.Error("an ordinary file is denied")
	}
}

func TestAWindowsProgramIsAllowedByItsName(t *testing.T) {
	cases := map[string]string{
		`C:\Users\someone\AppData\Roaming\npm\claude.cmd`: "claude",
		`C:\Program Files\Git\cmd\Git.EXE`:                "git",
		`C:\tools\node.exe`:                               "node",
		`C:\tools\npx.CMD`:                                "npx",
		`C:\tools\uname`:                                  "uname",
	}
	for p, want := range cases {
		if got := ProgramName(p, "windows"); got != want {
			t.Errorf("%s: %q, want %q", p, got, want)
		}
	}
	if got := ProgramName("/usr/bin/git.exe", "linux"); got != "git.exe" {
		t.Errorf("linux: %q", got)
	}
}

func TestTheFinalPathLosesItsPrefix(t *testing.T) {
	cases := map[string]string{
		`\\?\C:\Users\someone\proj\a.go`: `C:\Users\someone\proj\a.go`,
		`\\?\UNC\host\share\x`:           `\\host\share\x`,
		`C:\plain`:                       `C:\plain`,
	}
	for in, want := range cases {
		if got := trimFinalPath(in); got != want {
			t.Errorf("%s: %s", in, got)
		}
	}
}
