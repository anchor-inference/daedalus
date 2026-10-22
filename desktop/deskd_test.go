package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"runtime"
	"slices"
	"sort"
	"strings"
	"testing"
)

// DAEDALUS_DESKD wins; then the daemon beside the launcher, which in a macOS bundle is
// Contents/MacOS; then nothing.
func TestTheDesktopDaemonIsFoundWhereTheReleasePutsIt(t *testing.T) {
	name := "deskd"
	if runtime.GOOS == "windows" {
		name = "deskd.exe"
	}
	exe := filepath.Join("apps", "Daedalus.app", "Contents", "MacOS", "daedalus-desktop")
	beside := filepath.Join(filepath.Dir(exe), name)
	env := func(value string) func(string) string {
		return func(key string) string {
			if key == "DAEDALUS_DESKD" {
				return value
			}
			return ""
		}
	}
	has := func(p string) bool { return p == beside }
	if got := deskdBinary(exe, env("/built/deskd"), has); got != "/built/deskd" {
		t.Fatalf("the environment's daemon: %q", got)
	}
	if got := deskdBinary(exe, env(""), has); got != beside {
		t.Fatalf("the packaged daemon: %q", got)
	}
	if got := deskdBinary(exe, env(""), func(string) bool { return false }); got != "" {
		t.Fatalf("no daemon: %q", got)
	}
}

// The run directory, the state with the operator's token, and the configuration are all in the
// sealed local state folder and none in the data folder an upgrade copies; the command line names
// each of them, and the operator's token is not where the host is pointed.
func TestTheDesktopDaemonRunsFromTheSealedLocalState(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{deskdRunDir(paths), deskdStateDir(paths), deskdConfigPath(paths)} {
		if !within(dir, paths.Local) || within(dir, paths.Data) {
			t.Fatalf("%s is not in the sealed local state folder", dir)
		}
	}
	if within(deskdOperatorToken(paths), deskdRunDir(paths)) {
		t.Fatal("the operator's token is inside the run directory the host reads")
	}
	run := deskdRunDir(paths)
	want := []string{"/opt/deskd", "serve", "--run-dir", run, "--state-dir", deskdStateDir(paths), "--config", deskdConfigPath(paths),
		"--listen", deskdListen(run), "--log-file", filepath.Join(paths.RuntimeLogs, "deskd.log")}
	if got := deskdArgv("/opt/deskd", paths); !slices.Equal(got, want) {
		t.Fatalf("argv %q, want %q", got, want)
	}
	if runtime.GOOS == "windows" && deskdListen(run) != "tcp:127.0.0.1:0" {
		t.Fatalf("Windows listens on %q", deskdListen(run))
	}
	deep := filepath.Join(strings.Repeat("a", 120), "run")
	if got := deskdListen(deep); got != "tcp:127.0.0.1:0" {
		t.Fatalf("a socket path too long for the system listens on %q", got)
	}
}

// The host is told the run directory, always, and nothing that leads to the operator's token.
func TestTheAgentIsToldOnlyTheDesktopRunDirectory(t *testing.T) {
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	env := supervisorEnv(paths, nil, nil)
	if got := envMap(env)["COMPUTER_HOST_DIR"]; got != deskdRunDir(paths) {
		t.Fatalf("COMPUTER_HOST_DIR = %q", got)
	}
	for _, kv := range env {
		_, value, _ := strings.Cut(kv, "=")
		if strings.Contains(value, deskdStateDir(paths)) || strings.Contains(value, "operator.token") {
			t.Fatalf("the agent's environment leads to the operator's token: %s", kv)
		}
	}
}

// Daedalus itself on every system: the application's window by what that system names it by, the
// launcher by its executable and pid, and the shell above it by its pid.
func TestDaedalusIsProtectedOnEverySystem(t *testing.T) {
	cases := []struct {
		goos string
		exe  string
		want []deskdMatcher
	}{
		{"darwin", "/Applications/Daedalus.app/Contents/MacOS/daedalus-desktop", []deskdMatcher{
			{BundleID: "io.github.anchor-inference.daedalus"}, {ExeName: "Daedalus"}, {ExeName: "daedalus-desktop"}, {PID: 41}, {PID: 40}}},
		{"windows", `C:\Users\someone\AppData\Local\Programs\Daedalus\daedalus-desktop.exe`, []deskdMatcher{
			{ExeName: "Daedalus"}, {ExeName: "daedalus-desktop"}, {PID: 41}, {PID: 40}}},
		{"linux", "/opt/Daedalus/daedalus-desktop", []deskdMatcher{
			{ExeName: "daedalus"}, {WMClass: "daedalus"}, {ExeName: "daedalus-desktop"}, {PID: 41}, {PID: 40}}},
	}
	for _, c := range cases {
		if got := deskdProtected(c.goos, c.exe, 41, 40); !reflect.DeepEqual(got, c.want) {
			t.Fatalf("%s: protected %+v, want %+v", c.goos, got, c.want)
		}
	}
	// A launcher with no shell above it protects no parent: that would be the operator's terminal.
	got := deskdProtected("linux", "/opt/Daedalus/daedalus-desktop", 41, 0)
	for _, m := range got {
		if m.PID != 0 && m.PID != 41 {
			t.Fatalf("a parent that is not the shell is protected: %+v", got)
		}
	}
	// No entry may be empty: deskd matches nothing with one, and an empty entry is a mistake here.
	for _, c := range cases {
		for _, m := range deskdProtected(c.goos, c.exe, 41, 40) {
			if m == (deskdMatcher{}) {
				t.Fatalf("%s: an empty protected entry", c.goos)
			}
		}
	}
	// The bundle id is the application's appId.
	builder, err := os.ReadFile(filepath.Join("shell", "electron-builder.yml"))
	if err != nil {
		t.Fatal(err)
	}
	// A Windows checkout ends the lines with CRLF.
	builder = bytes.ReplaceAll(builder, []byte("\r\n"), []byte("\n"))
	if !bytes.Contains(builder, []byte("appId: "+shellAppID+"\n")) {
		t.Fatalf("electron-builder.yml no longer names %s as the appId; the protected bundle id must follow it", shellAppID)
	}
}

// deskd refuses its configuration over a key it does not know, which would leave computer use
// down at every start. What the launcher writes is checked against the daemon's own decoder: the
// json names of its Config and Matcher, read from its source.
func TestTheConfigurationCarriesOnlyKeysDeskdKnows(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "deskd", "config.json")
	config := deskdConfig{Protected: deskdProtected("darwin", "/x/daedalus-desktop", 41, 40)}
	config.Protected = append(config.Protected, deskdMatcher{ExePath: "/x/Daedalus"})
	if err := writeDeskdConfig(path, config); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var top map[string]json.RawMessage
	if err := json.Unmarshal(body, &top); err != nil {
		t.Fatal(err)
	}
	var entries []map[string]any
	if err := json.Unmarshal(top["protected"], &entries); err != nil {
		t.Fatal(err)
	}
	topKeys := keysOf(top)
	entryKeys := map[string]bool{}
	for _, e := range entries {
		for k := range e {
			entryKeys[k] = true
		}
	}
	if !slices.Equal(topKeys, []string{"protected"}) {
		t.Fatalf("top-level keys %v", topKeys)
	}
	if runtime.GOOS != "windows" {
		if info, err := os.Stat(path); err != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("the configuration is not private to its owner: %v %v", info.Mode(), err)
		}
	}

	source, err := os.ReadFile(filepath.Join("..", "deskd", "internal", "desk", "desk.go"))
	if err != nil {
		t.Skipf("deskd's source is not beside this module: %v", err)
	}
	known := func(typeName string) map[string]bool {
		block := regexp.MustCompile(`(?s)type ` + typeName + ` struct \{(.*?)\n\}`).FindSubmatch(source)
		if block == nil {
			t.Fatalf("deskd has no %s struct any more", typeName)
		}
		names := map[string]bool{}
		for _, m := range regexp.MustCompile("json:\"([a-z_]+)").FindAllSubmatch(block[1], -1) {
			names[string(m[1])] = true
		}
		return names
	}
	configKeys, matcherKeys := known("Config"), known("Matcher")
	for _, k := range topKeys {
		if !configKeys[k] {
			t.Fatalf("the launcher writes %q, which deskd's Config does not decode", k)
		}
	}
	for k := range entryKeys {
		if !matcherKeys[k] {
			t.Fatalf("a protected entry carries %q, which deskd's Matcher does not decode", k)
		}
	}
	// Every field the launcher's matcher can set is one deskd has, set or not in this file.
	for _, field := range reflect.VisibleFields(reflect.TypeOf(deskdMatcher{})) {
		name, _, _ := strings.Cut(field.Tag.Get("json"), ",")
		if !matcherKeys[name] {
			t.Fatalf("deskdMatcher.%s is %q, which deskd's Matcher does not decode", field.Name, name)
		}
	}
}

func keysOf(m map[string]json.RawMessage) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// The agent detaches first; then the desktop daemon lets go of the desktop, before the browsers and
// terminals go and the key proxy last.
func TestTheDesktopDaemonStopsRightAfterTheAgent(t *testing.T) {
	var log []string
	n := &Native{supervisor: recorded{"supervisor", &log}, deskd: recorded{"deskd", &log}, browserd: recorded{"browserd", &log},
		ptyd: recorded{"ptyd", &log}, keyproxy: recorded{"keyproxy", &log}}
	n.Stop(context.Background())
	if got := strings.Join(log, ", "); got != "stop supervisor, stop deskd, stop browserd, stop ptyd, stop keyproxy" {
		t.Fatalf("stopped in the order %s", got)
	}
	if n.deskd != nil {
		t.Fatal("the desktop daemon was kept for the next start")
	}
}

// A build without the daemon, and a session it cannot serve, leave the reason where the host and
// `status` read it, and start nothing.
func TestAMissingDesktopDaemonLeavesItsReason(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("DAEDALUS_DESKD", "")
	n := NewNative(paths, func(string, ...any) {})
	exe, _ := os.Executable()
	if deskdBinary(exe, os.Getenv, exists) != "" {
		t.Skip("a deskd lies beside the test binary")
	}
	if c := n.newDeskd(); c != nil {
		t.Fatal("a daemon was made without a binary")
	}
	if got := deskdReason(deskdRunDir(paths)); got != noDeskdReason {
		t.Fatalf("the run directory says %q", got)
	}
	if got := computerStatusLine(deskdRunDir(paths)); !strings.Contains(got, "carries no deskd") {
		t.Fatalf("status says %q", got)
	}

	// With a binary named, the note goes and the configuration is written before the daemon starts.
	t.Setenv("DAEDALUS_DESKD", filepath.Join(t.TempDir(), "deskd"))
	t.Setenv("XDG_SESSION_TYPE", "x11")
	t.Setenv("WAYLAND_DISPLAY", "")
	c := n.newDeskd()
	if c == nil {
		t.Fatal("no daemon with DAEDALUS_DESKD set")
	}
	if deskdReason(deskdRunDir(paths)) != "" {
		t.Fatal("the note was left for a daemon that is starting")
	}
	body, err := os.ReadFile(deskdConfigPath(paths))
	if err != nil {
		t.Fatal(err)
	}
	var config deskdConfig
	if err := json.Unmarshal(body, &config); err != nil || len(config.Protected) == 0 {
		t.Fatalf("the configuration protects nothing: %s", body)
	}
	if !slices.Contains(config.Protected, deskdMatcher{PID: os.Getpid()}) {
		t.Fatalf("the launcher's own pid is not protected: %s", body)
	}
	if got := computerStatusLine(deskdRunDir(paths)); got != "the desktop daemon is not running" {
		t.Fatalf("status says %q", got)
	}
}

// Wayland is told apart by deskd's own test, so the launcher does not start a daemon that would
// refuse the session and be restarted for ever.
func TestAWaylandSessionIsToldApartAsTheDaemonTellsIt(t *testing.T) {
	env := func(pairs map[string]string) func(string) string {
		return func(k string) string { return pairs[k] }
	}
	cases := []struct {
		env  map[string]string
		want bool
	}{
		{map[string]string{"XDG_SESSION_TYPE": "wayland"}, true},
		{map[string]string{"WAYLAND_DISPLAY": "wayland-0"}, true},
		{map[string]string{"WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "x11"}, false},
		{map[string]string{"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, false},
		{map[string]string{}, false},
	}
	for _, c := range cases {
		got := waylandSession(env(c.env))
		if runtime.GOOS != "linux" {
			if got {
				t.Fatalf("%v counted as Wayland off Linux", c.env)
			}
			continue
		}
		if got != c.want {
			t.Fatalf("%v: wayland = %v, want %v", c.env, got, c.want)
		}
	}
}
