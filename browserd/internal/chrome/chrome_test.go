package chrome

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestArgs(t *testing.T) {
	a := Args(Options{ProfileDir: "/p", Proxy: "127.0.0.1:9", Args: []string{"--lang=ru"}})
	for _, want := range []string{"--headless=new", "--remote-debugging-pipe", "--password-store=basic",
		"--disable-field-trial-config", "--webrtc-ip-handling-policy=disable_non_proxied_udp", "--user-data-dir=/p",
		"--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>", "--lang=ru",
		// The rest of the wall: no name resolved by Chromium itself, the proxy's address excepted.
		"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--disable-quic"} {
		if !slices.Contains(a, want) {
			t.Errorf("missing %s", want)
		}
	}
	if slices.Contains(a, "--no-sandbox") || slices.ContainsFunc(a, func(s string) bool { return strings.HasPrefix(s, "--remote-debugging-port") }) {
		t.Fatalf("a sandbox off or a debugging port: %v", a)
	}
	if a[len(a)-1] != "about:blank" {
		t.Fatalf("the first page: %v", a)
	}
	if !slices.Contains(Args(Options{ProfileDir: "/p", NoSandbox: true}), "--no-sandbox") {
		t.Fatal("no_sandbox from the configuration is not passed")
	}
}

// TestFlagsAPageCannotSee holds the switch list to what it is: the ones that made the browser one
// no person runs stay out, and the two modes differ only in the headless switch and the strip.
func TestFlagsAPageCannotSee(t *testing.T) {
	for _, mode := range []Options{{ProfileDir: "/p"}, {ProfileDir: "/p", Headed: true}} {
		a := Args(mode)
		for _, gone := range []string{"--disable-extensions", "--hide-scrollbars", "--disable-component-update",
			"--disable-default-apps", "--no-pings", "--enable-automation", "--disable-blink-features=AutomationControlled"} {
			if slices.Contains(a, gone) {
				t.Errorf("headed=%v: %s is back", mode.Headed, gone)
			}
		}
	}
	headless := Args(Options{ProfileDir: "/p", ScreenW: 2560, ScreenH: 1440})
	if !slices.Contains(headless, "--headless=new") || !slices.Contains(headless, "--screen-info={2560x1440}") ||
		slices.Contains(headless, "--disable-infobars") {
		t.Fatalf("headless: %v", headless)
	}
	headed := Args(Options{ProfileDir: "/p", Headed: true, ScreenW: 2560, ScreenH: 1440})
	headlessOnly := func(s string) bool {
		return strings.HasPrefix(s, "--headless") || strings.HasPrefix(s, "--screen-info")
	}
	if slices.ContainsFunc(headed, headlessOnly) || !slices.Contains(headed, "--disable-infobars") {
		t.Fatalf("headed: %v", headed)
	}
	// The same list otherwise, so a test of one mode speaks for the other.
	if len(headed) != len(headless)-1 {
		t.Fatalf("the modes differ by more than their own switches:\n%v\n%v", headless, headed)
	}
}

func TestWritePreferencesKeepsWhatChromiumWrote(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "Default"), 0o700); err != nil {
		t.Fatal(err)
	}
	existing := `{"webrtc":{"other":1},"session":{"restore_on_startup":1},"profile":{"name":"x"}}`
	if err := os.WriteFile(filepath.Join(dir, "Default", "Preferences"), []byte(existing), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := WritePreferences(dir); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(filepath.Join(dir, "Default", "Preferences"))
	var p map[string]map[string]any
	var top map[string]any
	if err := json.Unmarshal(data, &top); err != nil {
		t.Fatal(err)
	}
	p = map[string]map[string]any{}
	for k, v := range top {
		if m, ok := v.(map[string]any); ok {
			p[k] = m
		}
	}
	if top["credentials_enable_service"] != false {
		t.Fatalf("the password manager's switch: %v", top["credentials_enable_service"])
	}
	if p["webrtc"]["ip_handling_policy"] != "disable_non_proxied_udp" || p["webrtc"]["other"] != float64(1) {
		t.Fatalf("webrtc: %v", p["webrtc"])
	}
	if p["session"]["restore_on_startup"] != float64(1) || p["profile"]["name"] != "x" || p["profile"]["exit_type"] != "Normal" {
		t.Fatalf("merged: %s", data)
	}
	// A new profile gets the file too.
	if err := WritePreferences(filepath.Join(dir, "new")); err != nil {
		t.Fatal(err)
	}
}

func TestFindHonoursTheConfiguredPath(t *testing.T) {
	dir := t.TempDir()
	exe := filepath.Join(dir, "chromium-1234", "chrome-linux64", "chrome")
	_ = os.MkdirAll(filepath.Dir(exe), 0o700)
	if err := os.WriteFile(exe, []byte("#!/bin/sh\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	f, ok := Find(exe)
	if !ok || f.Path != exe || f.Kind != "bundled" {
		t.Fatalf("configured: %+v %v", f, ok)
	}
	if _, ok := Find(filepath.Join(dir, "missing")); ok {
		t.Fatal("a configured path that does not exist was accepted")
	}
	t.Setenv("BROWSERD_CHROMIUM", "")
	t.Setenv("PLAYWRIGHT_BROWSERS_PATH", dir)
	if got := newestPlaywright(dir); got != exe {
		t.Fatalf("Playwright's: %q", got)
	}
}

func TestSandboxProblem(t *testing.T) {
	if SandboxProblem("FATAL:zygote_host_impl_linux.cc(128)] No usable sandbox! If you are running on Ubuntu") == "" {
		t.Fatal("the sandbox failure was not recognised")
	}
	if SandboxProblem("some other noise") != "" {
		t.Fatal("noise read as a sandbox failure")
	}
}
