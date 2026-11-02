package config

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestNormalizeServer(t *testing.T) {
	cases := map[string]string{
		"127.0.0.1:8765":             "http://127.0.0.1:8765",
		"http://127.0.0.1:8765/":     "http://127.0.0.1:8765",
		"https://example.test/app/":  "https://example.test",
		"https://example.test/d/app": "https://example.test/d",
		" http://<host>:1 ":          "",
		"ftp://example.test":         "",
		"http://example.test/?q=1":   "",
		"":                           "",
	}
	for in, want := range cases {
		got, err := NormalizeServer(in)
		if want == "" {
			if err == nil {
				t.Errorf("%q: accepted as %q", in, got)
			}
			continue
		}
		if err != nil || got != want {
			t.Errorf("%q: %q, %v; want %q", in, got, err, want)
		}
	}
}

func TestSelectAndSave(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(EnvConfigDir, dir)
	t.Setenv(EnvProfile, "")
	t.Setenv(EnvServer, "")
	f, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	f.Put("home", Profile{Server: "http://127.0.0.1:1", Guard: "plan"})
	f.Put("work", Profile{Server: "http://127.0.0.1:2"})
	if err := f.Save(); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" {
		st, _ := os.Stat(filepath.Join(dir, "config.json"))
		if st.Mode().Perm() != 0o600 {
			t.Errorf("mode %v", st.Mode())
		}
	}
	f, err = Load()
	if err != nil {
		t.Fatal(err)
	}
	sel, err := f.Select("", "")
	if err != nil || sel.Name != "home" || sel.Profile.Guard != "plan" || !sel.Known {
		t.Fatalf("the first profile is the default: %+v %v", sel, err)
	}
	t.Setenv(EnvProfile, "work")
	if sel, _ := f.Select("", ""); sel.Name != "work" {
		t.Errorf("the environment's profile: %+v", sel)
	}
	if sel, _ := f.Select("home", "127.0.0.1:9"); sel.Name != "home" || sel.Profile.Server != "http://127.0.0.1:9" {
		t.Errorf("the flags win: %+v", sel)
	}
	if _, err := f.Select("bad name!", ""); err == nil {
		t.Error("a profile name a keychain would choke on was accepted")
	}
	if err := os.WriteFile(filepath.Join(dir, "config.json"), []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(); err == nil {
		t.Error("a broken file loaded as empty")
	}
}
