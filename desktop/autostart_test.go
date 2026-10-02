package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type fakeRunKey struct{ values map[string]string }

func (f *fakeRunKey) Get(name string) (string, bool, error) {
	v, ok := f.values[name]
	return v, ok, nil
}
func (f *fakeRunKey) Set(name, value string) error { f.values[name] = value; return nil }
func (f *fakeRunKey) Delete(name string) error     { delete(f.values, name); return nil }

func autostartRoundTrip(t *testing.T, a Autostart) {
	t.Helper()
	if on, err := a.Enabled(); err != nil || on {
		t.Fatalf("before: %v %v", on, err)
	}
	if err := a.Disable(); err != nil {
		t.Fatalf("disable when absent: %v", err)
	}
	if err := a.Enable(); err != nil {
		t.Fatal(err)
	}
	if on, err := a.Enabled(); err != nil || !on {
		t.Fatalf("after enable: %v %v", on, err)
	}
	if err := a.Enable(); err != nil {
		t.Fatalf("enable twice: %v", err)
	}
	if err := a.Disable(); err != nil {
		t.Fatal(err)
	}
	if on, err := a.Enabled(); err != nil || on {
		t.Fatalf("after disable: %v %v", on, err)
	}
	if err := a.Disable(); err != nil {
		t.Fatalf("disable twice: %v", err)
	}
}

func TestAutostartLinuxRoundTrip(t *testing.T) {
	home := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", "")
	command := []string{"/opt/Some Dir/daedalus", "start"}
	a := NewAutostart("linux", home, command, nil)
	path := filepath.Join(home, ".config", "autostart", "daedalus.desktop")
	if a.Where() != path {
		t.Fatalf("where %s", a.Where())
	}
	if err := a.Enable(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"Type=Application", "Name=Daedalus", `Exec="/opt/Some Dir/daedalus" "start"`, "X-GNOME-Autostart-enabled=true", "NoDisplay=false", autostartMarker} {
		if !strings.Contains(string(data), want) {
			t.Errorf("missing %q in\n%s", want, data)
		}
	}
	if info, _ := os.Stat(path); info.Mode().Perm() != 0o644 {
		t.Errorf("mode %v", info.Mode())
	}
	if err := a.Disable(); err != nil {
		t.Fatal(err)
	}
	autostartRoundTrip(t, NewAutostart("linux", t.TempDir(), command, nil))
}

func TestAutostartLinuxHonoursXDGConfigHome(t *testing.T) {
	xdg := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdg)
	a := NewAutostart("linux", t.TempDir(), []string{"/bin/x"}, nil)
	if a.Where() != filepath.Join(xdg, "autostart", "daedalus.desktop") {
		t.Fatal(a.Where())
	}
}

func TestDesktopExecQuoting(t *testing.T) {
	got := desktopExec([]string{`/a b/c"d$e`, "100%"})
	want := `"/a b/c\\"d\\$e" "100%%"`
	if got != want {
		t.Fatalf("got %s want %s", got, want)
	}
}

func TestAutostartLinuxForeignFileIsLeftAlone(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", "")
	home := t.TempDir()
	a := NewAutostart("linux", home, []string{"/bin/x"}, nil)
	if err := os.MkdirAll(filepath.Dir(a.Where()), 0o755); err != nil {
		t.Fatal(err)
	}
	foreign := "[Desktop Entry]\nName=Mine\nExec=/bin/mine\n"
	if err := os.WriteFile(a.Where(), []byte(foreign), 0o644); err != nil {
		t.Fatal(err)
	}
	if on, err := a.Enabled(); err != nil || on {
		t.Fatalf("%v %v", on, err)
	}
	if err := a.Disable(); err != nil {
		t.Fatal(err)
	}
	if err := a.Enable(); err == nil {
		t.Fatal("enable overwrote a foreign file")
	}
	if data, _ := os.ReadFile(a.Where()); string(data) != foreign {
		t.Fatalf("foreign file changed: %s", data)
	}
}

func TestAutostartDarwinRoundTrip(t *testing.T) {
	home := t.TempDir()
	command := []string{"/Applications/Daedalus & Co.app/Contents/MacOS/Daedalus"}
	a := NewAutostart("darwin", home, command, nil)
	if err := a.Enable(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(home, "Library", "LaunchAgents", autostartLabel+".plist"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"<key>Label</key>", "<string>" + autostartLabel + "</string>", "/Daedalus &amp; Co.app/", "<key>RunAtLoad</key>", "<true/>"} {
		if !strings.Contains(string(data), want) {
			t.Errorf("missing %q in\n%s", want, data)
		}
	}
	autostartRoundTrip(t, NewAutostart("darwin", t.TempDir(), command, nil))
}

func TestAutostartWindowsRoundTrip(t *testing.T) {
	reg := &fakeRunKey{values: map[string]string{"Other": "keep"}}
	a := NewAutostart("windows", "", []string{`C:\Program Files\Daedalus\Daedalus.exe`, "start"}, reg)
	if err := a.Enable(); err != nil {
		t.Fatal(err)
	}
	if got := reg.values["Daedalus"]; got != `"C:\Program Files\Daedalus\Daedalus.exe" "start"` {
		t.Fatalf("%s", got)
	}
	autostartRoundTrip(t, NewAutostart("windows", "", []string{`C:\x.exe`}, &fakeRunKey{values: map[string]string{}}))
	if reg.values["Other"] != "keep" {
		t.Fatal("touched another value")
	}
	if err := a.Disable(); err != nil || reg.values["Other"] != "keep" {
		t.Fatal(err)
	}
}

func TestAutostartUnsupportedAndEmptyCommand(t *testing.T) {
	if err := NewAutostart("plan9", "", []string{"x"}, nil).Enable(); err == nil {
		t.Fatal("plan9 enabled")
	}
	t.Setenv("XDG_CONFIG_HOME", "")
	if err := NewAutostart("linux", t.TempDir(), nil, nil).Enable(); err == nil {
		t.Fatal("empty command accepted")
	}
}

func TestAutostartCommandIsNotEmpty(t *testing.T) {
	if len(AutostartCommand()) == 0 {
		t.Fatal("no command")
	}
}
